"""
Platform username-availability checkers for the Multi-Sniper Discord bot.

Each checker maps an HTTP response to one normalized status:

    AVAILABLE  the platform confirms that the name is free
    TAKEN      an existing profile/account was found
    INVALID    the name cannot be used on that platform
    BLOCKED    rate limit, challenge page, or anti-bot wall; result is unknown
    SKIPPED    checker deliberately disabled by configuration
    ERROR      timeout, network, or unexpected response failure

The bot calls ``run_all_checks`` with one shared deadline so Minecraft,
guns.lol, and the optional Discord probe run concurrently rather than adding
their latencies together.

Important: Discord's public bot API does not expose username search. The
optional ``account`` mode uses the first-party account-flow eligibility route
(or an authorized compatible gateway) and is still off by default. Its JSON
answer is parsed strictly; malformed or blocked responses remain unknown.
``dnsrobot`` mirrors the fast browser-side request used by
https://dnsrobot.net/username-checker; it does not forward account/probe
credentials. ``probe`` remains available for an explicit external checker URL
and is never silently pointed at Discord's homepage.

Run a one-off report without starting Discord:

    python checkers.py Notch
    python checkers.py vortex --mode account
    python checkers.py zxqw99182vlt --mode probe \
        --discord-probe-url 'https://checker.example/{username}'
"""

from __future__ import annotations

import argparse
import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import aiohttp

# ---------------------------------------------------------------------------
# Normalized result statuses and platform identities
# ---------------------------------------------------------------------------

AVAILABLE = "available"
TAKEN = "taken"
INVALID = "invalid"
BLOCKED = "blocked"      # anti-bot wall / rate limit - availability unknown
SKIPPED = "skipped"      # checker disabled in config
ERROR = "error"          # timeout / network failure

MINECRAFT_EMOJI = "\U0001F579\uFE0F"
GUNSLOL_EMOJI = "\U0001F52B"
DISCORD_EMOJI = "\U0001F408\u200D\u2B1B"

# Kept in reaction order as well as timeout/error result order.
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("Minecraft", MINECRAFT_EMOJI),
    ("guns.lol", GUNSLOL_EMOJI),
    ("Discord", DISCORD_EMOJI),
)

# Realistic browser headers help regular profile pages return their ordinary
# HTML instead of a simplistic bot response. They do not try to bypass a
# challenge; challenge pages are reported as BLOCKED/unknown.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
}


@dataclass
class Result:
    """Outcome of one platform check."""

    platform: str
    emoji: str
    status: str
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.status == AVAILABLE


# ---------------------------------------------------------------------------
# Input validation (do not make network requests for impossible names)
# ---------------------------------------------------------------------------

# Minecraft names: 3-16 chars, letters/digits/underscore only.
MINECRAFT_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
# guns.lol profiles observed in the wild use dots too (for example id.search).
# The public service does not document an exhaustive registration rule, so this
# is deliberately only a transport-safe, conservative input rule.
GUNSLOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{2,24}$")
# Discord's new-style usernames: 2-32 chars, lowercase a-z 0-9 . _
DISCORD_PATTERN = re.compile(r"^[a-z0-9._]{2,32}$")

# A Discord message must look like one bare username token before the bot
# spends any request budget on it.
USERNAME_MESSAGE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


# ---------------------------------------------------------------------------
# Status-code / page interpreters (pure functions - easy to unit test)
# ---------------------------------------------------------------------------

# guns.lol currently serves some unclaimed names with an HTTP 200 page rather
# than an HTTP 404. These are intentionally narrow markers: a generic "User
# Not Found" can appear in a *claimed* profile's Discord-presence widget, so
# it must not be treated as an availability signal.
GUNSLOL_UNCLAIMED_MARKERS = (
    "username not found",
    "this user is not claimed",
    # The ordinary unclaimed page has used this title. Keep the HTML context
    # so a profile bio containing the same phrase is not enough to mark free.
    "<title>everything you want",
)
GUNSLOL_CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required",
    "cf-chl-",
    "/cdn-cgi/challenge-platform",
)


def interpret_minecraft(status: int) -> str:
    if status == 200:
        return TAKEN           # profile JSON came back -> name claimed
    if status in (204, 404):   # no profile exists -> free
        return AVAILABLE
    if status == 400:
        return INVALID         # name rejected by Mojang's own validation
    if status in (403, 405, 429):
        return BLOCKED         # rate limiting / auth wall
    return ERROR


def interpret_gunslol(status: int, page: str | None = None) -> str:
    """Interpret guns.lol's status plus its small semantic error page.

    A 200 status by itself normally means a claimed profile, but the service
    may render an unclaimed page with a 200 status. Conversely, a Cloudflare
    challenge can also be 200, so those known challenge markers are unknown,
    not falsely reported as taken.
    """

    if status == 200:
        content = (page or "").casefold()
        if any(marker in content for marker in GUNSLOL_CHALLENGE_MARKERS):
            return BLOCKED
        if any(marker in content for marker in GUNSLOL_UNCLAIMED_MARKERS):
            return AVAILABLE
        return TAKEN
    if status in (404, 410):
        return AVAILABLE
    if status == 400:
        return INVALID
    if status in (403, 429, 503):
        return BLOCKED         # Cloudflare challenge / rate limit
    return ERROR


def interpret_discord_probe(status: int) -> str:
    """Interpret the documented contract for an authorized external checker.

    The custom endpoint must use 200 for a claimed username and 404 for a
    free one. Authentication and rate-limit responses are *not* availability
    answers, so they remain unknown instead of being misreported as TAKEN.
    """

    if status == 200:
        return TAKEN
    if status == 404:
        return AVAILABLE
    if status in (401, 403, 429):
        return BLOCKED
    return ERROR


def interpret_discord_account_api(
    status: int,
    payload: object | None,
) -> str:
    """Interpret a JSON response from Discord's account username check.

    The account eligibility endpoint reports its answer in JSON (normally as
    ``{"taken": true|false}``) while the HTTP status describes transport or
    authorization failures. A successful status without a strict boolean is
    deliberately treated as ERROR; guessing from a missing/malformed field can
    turn an outage into a false availability result.

    A few authorized account-api gateways expose the equivalent ``available``
    field or wrap the result in ``data``. Supporting those shapes keeps the
    adapter useful without weakening the boolean-only contract. The optional
    numeric ``data.check.status`` shape is the compatibility format used by
    some Discord username account services: 2 means available, 3–6 means
    taken, 0 invalid, and 1 unknown.
    """

    if status in (401, 403, 429):
        return BLOCKED
    if status == 400:
        return INVALID
    if status != 200 or not isinstance(payload, Mapping):
        return ERROR

    data = payload.get("data")
    mappings = [payload]
    if isinstance(data, Mapping):
        mappings.append(data)

    boolean_outcomes: list[str] = []
    for mapping in mappings:
        for key in ("taken", "available"):
            if key not in mapping:
                continue
            value = mapping[key]
            if type(value) is not bool:
                # A recognized field with a string/number/null is malformed;
                # do not let a second compatibility field hide that problem.
                return ERROR
            # ``available`` has the opposite meaning to ``taken``.
            boolean_outcomes.append(
                (TAKEN if value else AVAILABLE) if key == "taken" else (
                    AVAILABLE if value else TAKEN))

    boolean_outcome: str | None = None
    if boolean_outcomes:
        if len(set(boolean_outcomes)) != 1:
            # Contradictory fields are an unknown response, not an answer.
            return ERROR
        boolean_outcome = boolean_outcomes[0]

    numeric_outcome: str | None = None
    if isinstance(data, Mapping):
        check = data.get("check")
        if isinstance(check, Mapping) and "status" in check:
            account_status = check["status"]
            # JSON booleans and floating-point lookalikes must not be accepted
            # as the compatibility service's integer status codes.
            if type(account_status) is not int:
                return ERROR
            numeric_outcome = {
                0: INVALID,
                2: AVAILABLE,
                3: TAKEN,
                4: TAKEN,
                5: TAKEN,
                6: TAKEN,
            }.get(account_status, ERROR)
            if numeric_outcome == ERROR:
                return ERROR

    if numeric_outcome is not None:
        if boolean_outcome is not None and boolean_outcome != numeric_outcome:
            return ERROR
        return numeric_outcome
    return boolean_outcome or ERROR


def interpret_discord_dnsrobot(status: int, payload: object | None) -> str:
    """Interpret the JSON returned by DNS Robot's browser-side Discord check.

    DNS Robot's published username-checker page does not expose a DNS Robot
    server API for Discord. Its browser code sends the candidate to Discord's
    first-party ``username-attempt-unauthed`` route and reads the same strict
    ``{"taken": true|false}`` shape as the account adapter. Keep this small
    named wrapper so the mode has an explicit, auditable contract rather than
    silently sharing account-mode configuration or credentials.
    """

    return interpret_discord_account_api(status, payload)


# Backwards-friendly short name for callers that do not need to distinguish
# the account API transport from the Discord platform it serves.
interpret_discord_account = interpret_discord_account_api


# ---------------------------------------------------------------------------
# Low-level fetch helpers
# ---------------------------------------------------------------------------

_REQUEST_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, ValueError)
_HTTP_SCHEMES = {"http", "https"}
_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_URL_USERINFO_PATTERN = re.compile(r"(?i)(https?://)[^/\s@]+@")
_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|authorization|password|secret|token)=)[^&#\s]+")
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret|token)="
    r"([^\s,&]+)")
_SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)\b((?:proxy-)?authorization:\s*(?:bearer|token)?\s*)[^\s,]+")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")


async def _fetch_status(
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> int:
    """GET a URL and return its final HTTP status (following redirects)."""

    async with session.get(url, proxy=proxy, headers=headers) as response:
        return response.status


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: Mapping[str, object],
    proxy: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, object | None]:
    """POST JSON and return the status plus a decoded response, if any.

    Discord's account username eligibility endpoint returns a JSON object such
    as ``{"taken": false}`` rather than using 404 for an available name. Keep
    JSON decoding here so the account checker can reject an HTML/error response
    without guessing at its meaning.
    """

    async with session.post(
        url, json=dict(payload), proxy=proxy, headers=headers,
    ) as response:
        try:
            # ``content_type=None`` accepts JSON returned with a vendor or
            # missing Content-Type. The fallback keeps small test doubles and
            # compatible aiohttp-like clients that do not accept that keyword
            # usable as well.
            try:
                body = await response.json(content_type=None)
            except TypeError:
                body = await response.json()
        except (TypeError, ValueError, aiohttp.ContentTypeError):
            body = None
        return response.status, body


async def _fetch_page(
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None = None,
) -> tuple[int, str]:
    """GET a URL and return its final status and decoded HTML.

    guns.lol's availability response is not always represented by the status
    code alone, so that checker needs a small amount of response text. The
    shared aiohttp timeout still bounds both download and decoding.
    """

    async with session.get(url, proxy=proxy) as response:
        return response.status, await response.text(errors="replace")


def _safe_url(template: str, username: str) -> str:
    """Substitute ``{username}`` without allowing a bad template to crash bot."""

    try:
        return template.format(username=username)
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError("URL template has invalid braces; use {username}") from exc


def validate_http_url(url: str, label: str = "URL") -> str | None:
    """Return an actionable error for a non-HTTP(S) URL, otherwise ``None``."""

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname  # may raise for malformed bracketed IPv6
    except (TypeError, ValueError):
        return f"{label} is not a valid URL"
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not hostname:
        return f"{label} must be an absolute http:// or https:// URL"
    try:
        _ = parsed.port  # force validation of a malformed numeric port
    except ValueError:
        return f"{label} has an invalid port"
    if any(char.isspace() for char in url):
        return f"{label} must not contain whitespace"
    return None


def validate_account_api_url(url: str) -> str | None:
    """Validate the endpoint used by the JSON account-eligibility request."""

    if not url:
        return "DISCORD_ACCOUNT_API_URL is blank"
    if "{" in url or "}" in url:
        return (
            "DISCORD_ACCOUNT_API_URL must not contain a username placeholder; "
            "the username is sent in the JSON body"
        )
    return validate_http_url(url, "DISCORD_ACCOUNT_API_URL")


def validate_probe_url_template(template: str) -> str | None:
    """Validate the user-supplied external-checker URL template.

    The checker must receive the submitted username, so a static URL is not a
    valid template. Only HTTP(S) endpoints are accepted; this avoids accidental
    ``file:``/other schemes when configuration is supplied by a user.
    """

    if not template:
        return "DISCORD_PROBE_URL is blank"
    if "{username}" not in template:
        return "DISCORD_PROBE_URL must include a {username} placeholder"
    try:
        rendered = _safe_url(template, "example")
    except ValueError as exc:
        return str(exc)
    return validate_http_url(rendered, "DISCORD_PROBE_URL")


def is_valid_header_name(value: str) -> bool:
    """Whether ``value`` is safe to use as an HTTP header name."""

    return bool(_HEADER_NAME_PATTERN.fullmatch(value))


def _redact_sensitive_text(value: object) -> str:
    """Make exception details useful without leaking proxy/API credentials."""

    text = str(value).strip()
    text = _URL_USERINFO_PATTERN.sub(r"\1***@", text)
    text = _SENSITIVE_QUERY_PATTERN.sub(r"\1***", text)
    text = _SENSITIVE_ASSIGNMENT_PATTERN.sub(r"\1=***", text)
    text = _SENSITIVE_HEADER_PATTERN.sub(r"\1***", text)
    text = _BEARER_PATTERN.sub(r"\1***", text)
    # Keep exception details to one log line even if a remote service returns
    # control characters in an error message.
    text = " ".join(text.split())
    return text[:120] or type(value).__name__


def _request_error(platform: str, emoji: str, exc: Exception) -> Result:
    return Result(platform, emoji, ERROR, _redact_sensitive_text(exc))


# ---------------------------------------------------------------------------
# Async platform checkers
# ---------------------------------------------------------------------------

# Mojang occasionally returns random 403s at api.mojang.com, so retry the
# equivalent minecraftservices lookup once. The outer fan-out deadline prevents
# this fallback from turning a single Discord message into a long wait.
MINECRAFT_ENDPOINTS: Sequence[str] = (
    "https://api.mojang.com/users/profiles/minecraft/{username}",
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/{username}",
)

# This is Discord's first-party username eligibility route used by the account
# registration flow. It is intentionally opt-in: it is not part of the public
# bot API, can be restricted by Discord, and must not be confused with a bot
# account token or a request to discord.com/<username>.
DEFAULT_DISCORD_ACCOUNT_API_URL = (
    "https://discord.com/api/v10/unique-username/"
    "username-attempt-unauthed"
)
# A descriptive alias for integrations that call this an account endpoint.
DISCORD_ACCOUNT_API_URL = DEFAULT_DISCORD_ACCOUNT_API_URL

# DNS Robot's username-checker page is a browser UI, not a server-side Discord
# API. Its published client performs one direct, unauthenticated POST to the
# v9 route below after loading https://dnsrobot.net/username-checker?u=<name>.
# Keeping these as separate constants makes the provenance explicit: this
# mode mirrors the page's documented browser flow for low latency, while the
# ordinary ``account`` mode remains independently configurable.
DEFAULT_DISCORD_DNSROBOT_URL = "https://dnsrobot.net/username-checker"
DEFAULT_DISCORD_DNSROBOT_API_URL = (
    "https://discord.com/api/v9/unique-username/"
    "username-attempt-unauthed"
)
DNSROBOT_USERNAME_CHECKER_URL = DEFAULT_DISCORD_DNSROBOT_URL
DNSROBOT_BROWSER_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://dnsrobot.net",
    "Referer": DEFAULT_DISCORD_DNSROBOT_URL,
}


def dnsrobot_username_checker_url(username: str) -> str:
    """Build the same query URL used by DNS Robot's username checker page."""

    return f"{DEFAULT_DISCORD_DNSROBOT_URL}?{urlencode({'u': username})}"


async def check_minecraft(session, username: str, proxy=None) -> Result:
    """Check Minecraft/Mojang availability (emoji 🕹️)."""

    if not MINECRAFT_PATTERN.fullmatch(username):
        return Result(
            "Minecraft", MINECRAFT_EMOJI, INVALID,
            "name must be 3-16 chars of A-Z a-z 0-9 _",
        )

    outcome: Result | None = None
    for template in MINECRAFT_ENDPOINTS:
        try:
            status = await _fetch_status(session, _safe_url(template, username), proxy)
            outcome = Result(
                "Minecraft", MINECRAFT_EMOJI,
                interpret_minecraft(status), f"HTTP {status}",
            )
            # A block or transient endpoint error deserves the fallback. A
            # definitive AVAILABLE/TAKEN/INVALID answer does not.
            if outcome.status not in (BLOCKED, ERROR):
                return outcome
        except _REQUEST_ERRORS as exc:
            outcome = _request_error("Minecraft", MINECRAFT_EMOJI, exc)

    return outcome or Result("Minecraft", MINECRAFT_EMOJI, ERROR, "no endpoint attempted")


async def check_gunslol(session, username: str, proxy=None) -> Result:
    """Check guns.lol availability (emoji 🔫)."""

    if not GUNSLOL_PATTERN.fullmatch(username):
        return Result(
            "guns.lol", GUNSLOL_EMOJI, INVALID,
            "name must be 2-24 chars of A-Z a-z 0-9 . - _",
        )

    try:
        status, page = await _fetch_page(
            session, _safe_url("https://guns.lol/{username}", username), proxy)
        outcome = interpret_gunslol(status, page)
        detail = f"HTTP {status}"
        if status == 200 and outcome == AVAILABLE:
            detail += " (unclaimed page)"
        elif status == 200 and outcome == BLOCKED:
            detail += " (challenge page)"
        return Result("guns.lol", GUNSLOL_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("guns.lol", GUNSLOL_EMOJI, exc)


async def check_discord_account_api(
    session,
    username: str,
    proxy=None,
    api_url: str | None = None,
    api_headers: Mapping[str, str] | None = None,
) -> Result:
    """Check a username through an explicitly enabled account API.

    Discord's account eligibility route accepts ``POST`` JSON of the form
    ``{"username": "name"}`` and returns ``{"taken": true|false}``. The
    request is an eligibility check only; this function never calls the
    username-claim endpoint and never sends the bot token. ``api_url`` defaults
    to Discord's first-party account-flow route, but remains overrideable for
    an authorized gateway that exposes the same JSON contract.
    """

    if not DISCORD_PATTERN.fullmatch(username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )

    url = api_url or DISCORD_ACCOUNT_API_URL
    url_error = validate_account_api_url(url)
    if url_error:
        return Result("Discord", DISCORD_EMOJI, ERROR, url_error)

    try:
        status, payload = await _fetch_json(
            session, url, {"username": username}, proxy, api_headers)
        outcome = interpret_discord_account_api(status, payload)
        detail = f"HTTP {status}"
        if status == 200 and outcome == ERROR:
            detail += " (invalid account API response)"
        elif status == 200 and isinstance(payload, Mapping):
            if isinstance(payload.get("taken"), bool):
                detail += f" (taken={str(payload['taken']).lower()})"
            elif isinstance(payload.get("available"), bool):
                detail += f" (available={str(payload['available']).lower()})"
        return Result("Discord", DISCORD_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("Discord", DISCORD_EMOJI, exc)


# Short alias for integrations that refer to the adapter as the account check.
check_discord_account = check_discord_account_api


async def check_discord_dnsrobot(
    session,
    username: str,
    proxy=None,
) -> Result:
    """Mirror DNS Robot's fast browser-side Discord availability request.

    The website itself loads at :func:`dnsrobot_username_checker_url`, then its
    JavaScript sends a JSON POST directly to Discord. There is no DNS Robot
    server API to scrape, so this adapter mirrors that public page flow instead
    of starting a browser or guessing an undocumented proxy. It deliberately
    has no credential parameters: neither account-API nor probe credentials
    are ever forwarded to the DNS Robot flow.
    """

    normalized_username = username.lower()
    if not DISCORD_PATTERN.fullmatch(normalized_username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )

    try:
        # The page flow is tied to the submitted candidate. Copy the public
        # headers per request so this function cannot mutate the shared
        # constant or inherit account/probe credentials.
        browser_headers = {
            **DNSROBOT_BROWSER_HEADERS,
            "Referer": dnsrobot_username_checker_url(username),
        }
        status, payload = await _fetch_json(
            session,
            DEFAULT_DISCORD_DNSROBOT_API_URL,
            {"username": normalized_username},
            proxy,
            browser_headers,
        )
        outcome = interpret_discord_dnsrobot(status, payload)
        detail = f"HTTP {status} (DNS Robot browser flow)"
        if status == 200 and outcome == ERROR:
            detail += " (invalid account API response)"
        elif status == 200 and isinstance(payload, Mapping):
            if isinstance(payload.get("taken"), bool):
                detail += f" (taken={str(payload['taken']).lower()})"
            elif isinstance(payload.get("available"), bool):
                detail += f" (available={str(payload['available']).lower()})"
        return Result("Discord", DISCORD_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("Discord", DISCORD_EMOJI, exc)


async def check_discord(
    session,
    username: str,
    proxy=None,
    mode: str = "off",
    probe_url: str | None = None,
    probe_headers: Mapping[str, str] | None = None,
    account_api_url: str | None = None,
    account_api_headers: Mapping[str, str] | None = None,
) -> Result:
    """Check Discord in off, DNS Robot, account, or probe mode.

    ``off`` is the safe default. ``dnsrobot`` mirrors the fast browser request
    made by ``https://dnsrobot.net/username-checker?u=...`` and never accepts
    credentials. ``account`` (also accepted as ``account_api``) sends a JSON
    eligibility request to the configured account API and interprets its strict
    boolean response. ``probe`` remains available for an external GET checker
    using the 200/404 contract. No mode treats ``discord.com/<username>`` as an
    availability endpoint.
    """

    mode = (mode or "off").strip().lower()
    if mode == "off":
        return Result(
            "Discord", DISCORD_EMOJI, SKIPPED,
            "check disabled (DISCORD_CHECK_MODE=off)",
        )
    if mode == "dnsrobot":
        return await check_discord_dnsrobot(session, username, proxy)
    if mode in ("account", "account_api"):
        return await check_discord_account_api(
            session, username, proxy, account_api_url, account_api_headers)
    if mode != "probe":
        return Result(
            "Discord", DISCORD_EMOJI, ERROR,
            "DISCORD_CHECK_MODE must be off, dnsrobot, account, account_api, or probe",
        )
    if not probe_url:
        return Result(
            "Discord", DISCORD_EMOJI, SKIPPED,
            "probe requires an explicit DISCORD_PROBE_URL",
        )
    template_error = validate_probe_url_template(probe_url)
    if template_error:
        return Result("Discord", DISCORD_EMOJI, ERROR, template_error)
    if not DISCORD_PATTERN.fullmatch(username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )

    try:
        url = _safe_url(probe_url, username)
        status = await _fetch_status(session, url, proxy, headers=probe_headers)
        return Result(
            "Discord", DISCORD_EMOJI,
            interpret_discord_probe(status), f"HTTP {status}",
        )
    except _REQUEST_ERRORS as exc:
        return _request_error("Discord", DISCORD_EMOJI, exc)


# ---------------------------------------------------------------------------
# Parallel fan-out and deadline support
# ---------------------------------------------------------------------------

def timeout_results(detail: str = "check deadline reached") -> list[Result]:
    """Return one honest unknown/error result per platform.

    This lets the bot react with ⚠️ when a whole fan-out hits its response
    budget instead of pretending that a name was taken.
    """

    return [Result(platform, emoji, ERROR, detail) for platform, emoji in PLATFORMS]


async def _run_bounded(
    checker,
    fallback: Result,
    timeout: float | None,
) -> Result:
    """Run one checker without allowing it to break or outlive the fan-out."""

    try:
        if timeout is None:
            return await checker
        return await asyncio.wait_for(checker, timeout=max(0.0, timeout))
    except asyncio.TimeoutError:
        return Result(fallback.platform, fallback.emoji, ERROR, "check deadline reached")
    except Exception as exc:  # noqa: BLE001 - one checker must not cancel all results
        # Checkers already handle normal network failures. This final guard
        # isolates malformed optional configuration or unforeseen errors so one
        # platform can never cancel every result/reaction.
        return _request_error(fallback.platform, fallback.emoji, exc)


async def run_all_checks(
    session: aiohttp.ClientSession,
    username: str,
    proxy: str | None = None,
    discord_mode: str = "off",
    discord_probe_url: str | None = None,
    discord_probe_headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    discord_account_api_url: str | None = None,
    discord_account_api_headers: Mapping[str, str] | None = None,
) -> list[Result]:
    """Fan out every platform check in parallel.

    ``timeout`` is a *shared wall-clock budget* for the fan-out. Every worker
    begins at the same time, so the total duration is at most the one timeout,
    not the sum of platform timeouts. Timed-out workers return ERROR results.
    """

    fallbacks = timeout_results()
    workers = (
        _run_bounded(check_minecraft(session, username, proxy), fallbacks[0], timeout),
        _run_bounded(check_gunslol(session, username, proxy), fallbacks[1], timeout),
        _run_bounded(
            check_discord(
                session, username, proxy, discord_mode, discord_probe_url,
                discord_probe_headers, discord_account_api_url,
                discord_account_api_headers),
            fallbacks[2], timeout,
        ),
    )
    return list(await asyncio.gather(*workers))


# ---------------------------------------------------------------------------
# CLI self-test: python checkers.py <username>
#   [--mode off|dnsrobot|account|account_api|probe]
# ---------------------------------------------------------------------------

async def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Test the platform checkers without running the bot.")
    parser.add_argument("username", help="name to check, e.g. Notch")
    parser.add_argument(
        "--mode", choices=("off", "dnsrobot", "account", "account_api", "probe"),
        default="off",
        help=("Discord check mode (default: off; dnsrobot mirrors DNS Robot's "
              "browser flow; account_api is a compatibility alias)"))
    parser.add_argument("--discord-probe-url", default=None,
                        help="explicit authorized checker URL template for probe mode")
    parser.add_argument(
        "--discord-account-api-url", default=None,
        help="optional account API URL (default: Discord username eligibility route)")
    parser.add_argument("--proxy", default=None,
                        help="optional http(s) proxy URL")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="shared check deadline in seconds (default: 8)")
    ns = parser.parse_args(argv)

    deadline = max(0.1, ns.timeout)
    request_timeout = aiohttp.ClientTimeout(total=deadline)
    async with aiohttp.ClientSession(headers=BROWSER_HEADERS,
                                     timeout=request_timeout) as session:
        results = await run_all_checks(
            session, ns.username, ns.proxy,
            discord_mode=ns.mode,
            discord_probe_url=ns.discord_probe_url,
            discord_account_api_url=ns.discord_account_api_url,
            timeout=deadline,
        )

    icon = {
        AVAILABLE: "[FREE]  ", TAKEN: "[TAKEN]  ", INVALID: "[INVALID]",
        BLOCKED: "[BLOCKED]", SKIPPED: "[SKIP]  ", ERROR: "[ERROR] ",
    }
    print(f"\nAvailability report for '{ns.username}':")
    print("-" * 62)
    for result in results:
        print(f"  {result.emoji} {result.platform:<10} "
              f"{icon[result.status]} {result.detail}")
    print("-" * 62)

    emojis = [result.emoji for result in results if result.available]
    statuses = {result.status for result in results}
    if emojis:
        verdict = " ".join(emojis)
    elif not statuses or statuses & {ERROR, BLOCKED} or statuses <= {SKIPPED}:
        verdict = "⚠️  (availability could not be confirmed on every platform)"
    else:
        verdict = "❌"
    print(f"  Bot would react: {verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli()))
