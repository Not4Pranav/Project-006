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
guns.lol, and the selected optional Discord mode run concurrently rather than
adding their latencies together.

Important: Discord's public bot API does not expose username search. The
optional ``account`` mode uses the first-party account-flow eligibility route
(or an authorized compatible gateway) and is still off by default. Its JSON
answer is parsed strictly; malformed or blocked responses remain unknown.
``dnsrobot`` literally loads the DNS Robot username-checker page in an
isolated Playwright/Chromium context and reads its rendered Discord result; it
does not forward account/probe credentials. ``probe`` remains available for an
explicit external checker URL and is never silently pointed at Discord's
homepage.

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
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urlencode, urlsplit

import aiohttp

try:  # Playwright is optional until the DNS Robot mode is enabled.
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # Keep off/account/probe modes usable without a browser install.
    async_playwright = None

    class PlaywrightTimeoutError(Exception):
        """Fallback type used when the optional Playwright package is absent."""

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
_MISSING_PAYLOAD = object()


GUNSLOL_CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required",
    "cf-chl-",
    "/cdn-cgi/challenge-platform",
)


def interpret_minecraft(
    status: int,
    payload: object = _MISSING_PAYLOAD,
) -> str:
    if status == 200:
        # The status-only form is retained for small integrations that already
        # validate Mojang responses elsewhere. The live checker passes the
        # decoded body and requires the profile shape so a 200 HTML challenge
        # cannot be mistaken for a claimed name.
        if payload is _MISSING_PAYLOAD:
            return TAKEN
        if (
            isinstance(payload, Mapping)
            and isinstance(payload.get("id"), str)
            and bool(payload.get("id").strip())
            and isinstance(payload.get("name"), str)
            and bool(payload.get("name").strip())
        ):
            return TAKEN           # valid profile JSON -> name claimed
        return BLOCKED              # 200, but not a Mojang profile response
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
        # ``None`` is retained as the status-only compatibility form. The live
        # checker always supplies the response body, where an empty/non-text
        # body is malformed and therefore unknown.
        if page is None:
            return TAKEN
        if not isinstance(page, str) or not page.strip():
            return BLOCKED
        content = page.casefold()
        if any(marker in content for marker in GUNSLOL_CHALLENGE_MARKERS):
            return BLOCKED
        if any(marker in content for marker in GUNSLOL_UNCLAIMED_MARKERS):
            return AVAILABLE
        return TAKEN
    if status in (404, 410):
        if isinstance(page, str) and any(
                marker in page.casefold() for marker in GUNSLOL_CHALLENGE_MARKERS):
            return BLOCKED
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
    """Interpret a recorded JSON response from DNS Robot's browser flow.

    The live ``dnsrobot`` mode does not call this endpoint from Python: it
    loads DNS Robot in a browser and reads the rendered Discord card. Keeping
    this helper preserves the strict network-response contract for diagnostics
    and integrations that already capture that response.
    """

    return interpret_discord_account_api(status, payload)


def interpret_discord_dnsrobot_page(status: object | None) -> str:
    """Map the visible Discord card on DNS Robot's page to a safe status.

    Only the two positive labels are definitive. Pending, rate-limited,
    unknown, missing, or unfamiliar labels never become AVAILABLE.
    """

    if not isinstance(status, str):
        return ERROR
    normalized = status.strip().casefold()
    if normalized == "available":
        return AVAILABLE
    if normalized == "taken":
        return TAKEN
    if normalized in {"pending", "unknown", "rate limited", "rate-limited"}:
        return BLOCKED
    return ERROR


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


def _has_http_control_chars(value: object) -> bool:
    """Whether a URL/header value contains a C0 or DEL control character."""

    return isinstance(value, str) and any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _decoded_url_component_has_control_chars(value: str | None) -> bool:
    """Check percent-decoded URL user-info before a client sends it."""

    return value is not None and _has_http_control_chars(unquote(value))
_SENSITIVE_QUERY_PATTERN = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|authorization|password|secret|token)=)[^&#\s]+")
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(access[_-]?token|api[_-]?key|authorization|password|secret|token)="
    r"([^\s,&]+)")
_SENSITIVE_HEADER_PATTERN = re.compile(
    r"(?i)\b((?:proxy-)?authorization|(?:x-)?api[_-]?key|token):\s*"
    r"(?:bearer|token)?\s*[^\s,]+")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")


async def _fetch_status(
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> int:
    """GET one endpoint and return its status without following redirects.

    Probe endpoints can receive a credential-bearing header. Disabling
    redirects prevents aiohttp from carrying that header to an unrelated host
    selected by a 3xx response.
    """

    async with session.get(
        url,
        proxy=proxy,
        headers=headers,
        allow_redirects=False,
    ) as response:
        return response.status


async def _fetch_json_get(
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None = None,
) -> tuple[int, object | None]:
    """GET JSON and keep malformed successful bodies distinguishable."""

    async with session.get(url, proxy=proxy) as response:
        try:
            try:
                body = await response.json(content_type=None)
            except TypeError:
                body = await response.json()
        except (TypeError, ValueError, aiohttp.ContentTypeError):
            body = None
        return response.status, body


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
        url,
        json=dict(payload),
        proxy=proxy,
        headers=headers,
        allow_redirects=False,
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
    """GET a URL and return its status and decoded HTML without redirects.

    guns.lol's availability response is not always represented by the status
    code alone, so that checker needs a small amount of response text. Keeping
    redirects disabled prevents a profile URL redirecting to a generic page
    from becoming a false TAKEN result. The shared aiohttp timeout still
    bounds both download and decoding.
    """

    async with session.get(url, proxy=proxy, allow_redirects=False) as response:
        return response.status, await response.text(errors="replace")


def _safe_url(template: str, username: str) -> str:
    """Substitute only the literal ``{username}`` field in a URL template.

    ``str.format`` also permits attribute/index traversal and conversion
    specifiers. Configuration is trusted more than message content, but there
    is no reason to expose that formatting surface for a URL setting.
    """

    if not isinstance(template, str) or not isinstance(username, str):
        raise TypeError("URL template and username must be strings")
    if "{username}" not in template:
        raise ValueError("URL template must include {username}")
    remainder = template.replace("{username}", "")
    if "{" in remainder or "}" in remainder:
        raise ValueError("URL template has invalid braces; use {username}")
    return template.replace("{username}", username)


def validate_http_url(url: str, label: str = "URL") -> str | None:
    """Return an actionable error for a non-HTTP(S) URL, otherwise ``None``."""

    if not isinstance(url, str):
        return f"{label} is not a valid URL"
    if _has_http_control_chars(url):
        return f"{label} must not contain control characters"
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
    if (_decoded_url_component_has_control_chars(parsed.username)
            or _decoded_url_component_has_control_chars(parsed.password)):
        return f"{label} credentials must not contain control characters"
    return None


def validate_proxy_url(url: str) -> str | None:
    """Validate a proxy URL accepted by both aiohttp and Playwright.

    Playwright needs the proxy server without a path, query, or fragment. Keep
    the stricter validation in one place so the HTTP checkers and the optional
    browser use the same configuration instead of silently taking different
    routes.
    """

    error = validate_http_url(url, "PROXY_URL")
    if error:
        return error
    parsed = urlsplit(url)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return "PROXY_URL must not contain a path, query string, or fragment"
    return None


def validate_account_api_url(url: str) -> str | None:
    """Validate the endpoint used by the JSON account-eligibility request."""

    if not isinstance(url, str) or not url:
        return "DISCORD_ACCOUNT_API_URL is blank"
    if "{" in url or "}" in url:
        return (
            "DISCORD_ACCOUNT_API_URL must not contain a username placeholder; "
            "the username is sent in the JSON body"
        )
    error = validate_http_url(url, "DISCORD_ACCOUNT_API_URL")
    if error:
        return error
    if urlsplit(url).scheme.lower() != "https":
        return "DISCORD_ACCOUNT_API_URL must use an https:// URL"
    return None


def validate_probe_url_template(template: str) -> str | None:
    """Validate the user-supplied external-checker URL template.

    The checker must receive the submitted username, so a static URL is not a
    valid template. Only HTTPS endpoints are accepted so an optional probe
    credential cannot be sent in cleartext; this also avoids accidental
    ``file:``/other schemes when configuration is supplied by a user.
    """

    if not isinstance(template, str) or not template:
        return "DISCORD_PROBE_URL is blank"
    if "{username}" not in template:
        return "DISCORD_PROBE_URL must include a {username} placeholder"
    try:
        rendered = _safe_url(template, "example")
    except (TypeError, ValueError) as exc:
        return str(exc)
    error = validate_http_url(rendered, "DISCORD_PROBE_URL")
    if error:
        return error
    if urlsplit(rendered).scheme.lower() != "https":
        return "DISCORD_PROBE_URL must use an https:// URL"
    return None


def is_valid_header_name(value: str) -> bool:
    """Whether ``value`` is safe to use as an HTTP header name."""

    return isinstance(value, str) and bool(_HEADER_NAME_PATTERN.fullmatch(value))


def validate_request_headers(
    headers: Mapping[str, str] | None,
    label: str = "headers",
) -> str | None:
    """Validate optional per-request headers before handing them to aiohttp."""

    if headers is None:
        return None
    try:
        items = headers.items()
    except AttributeError:
        return f"{label} must be a mapping"
    try:
        for name, value in items:
            if not is_valid_header_name(name):
                return f"{label} contains an invalid header name"
            if not isinstance(value, str) or _has_http_control_chars(value):
                return f"{label} contains an invalid header value"
    except (AttributeError, TypeError, ValueError):
        return f"{label} must be a mapping of string headers"
    return None


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

# DNS Robot's username-checker page is a browser UI. Its client-side code
# performs the Discord request from the page, so the literal integration below
# launches a real browser, navigates to the page with ``?u=``, and reads the
# rendered Discord card. These network constants are retained for diagnostics
# and for ``interpret_discord_dnsrobot`` compatibility; the live dnsrobot mode
# never sends them with aiohttp and never forwards account/probe credentials.
DEFAULT_DISCORD_DNSROBOT_URL = "https://dnsrobot.net/username-checker"
# Backwards-compatible diagnostic constants. The live browser adapter below
# does not call this URL from aiohttp; DNS Robot's page calls it in the browser.
DEFAULT_DISCORD_DNSROBOT_API_URL = (
    "https://discord.com/api/v9/unique-username/"
    "username-attempt-unauthed"
)
DNSROBOT_BROWSER_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://dnsrobot.net",
    "Referer": DEFAULT_DISCORD_DNSROBOT_URL,
}
DNSROBOT_USERNAME_CHECKER_URL = DEFAULT_DISCORD_DNSROBOT_URL
DNSROBOT_ALLOWED_HOSTS = {"dnsrobot.net", "www.dnsrobot.net"}


def dnsrobot_username_checker_url(username: str) -> str:
    """Build the page URL used for one DNS Robot browser lookup."""

    return f"{DEFAULT_DISCORD_DNSROBOT_URL}?{urlencode({'u': username})}"


DNSROBOT_PAGE_STATUS_SCRIPT = r"""
() => {
  const statuses = new Set([
    "Available", "Taken", "Unknown", "Rate limited", "Pending",
    "Not supported"
  ]);
  const platformNames = Array.from(document.querySelectorAll("*"))
    .filter((element) => element.children.length === 0
      && element.textContent?.trim() === "Discord");
  for (const name of platformNames) {
    let node = name;
    for (let depth = 0; node && depth < 12; depth += 1, node = node.parentElement) {
      const text = node.innerText || "";
      // The category and latency may be adjacent text nodes (for example,
      // "Messaging205ms"), so do not require a whitespace-separated word.
      if (!text.includes("Messaging")) continue;

      const visibleLabels = [node, ...node.querySelectorAll("*")]
        .map((element) => element.textContent?.trim() || "")
        .filter((label) => statuses.has(label));
      const label = visibleLabels.find((value) => value !== "Pending")
        || visibleLabels[0]
        || null;
      if (label && label !== "Pending") return label;
    }
  }
  // Pending or an absent Discord card is deliberately falsy so Playwright
  // keeps waiting for the actual rendered result.
  return null;
}
"""


def playwright_proxy_config(proxy: str | None) -> dict[str, str] | None:
    """Convert a validated HTTP(S) proxy URL to Playwright's proxy shape."""

    if not proxy:
        return None
    error = validate_proxy_url(proxy)
    if error:
        raise ValueError(error)
    parsed = urlsplit(proxy)
    hostname = parsed.hostname
    if not hostname:  # Defensive: validate_proxy_url already checked this.
        raise ValueError("PROXY_URL must contain a hostname")
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    server = f"{parsed.scheme.lower()}://{host}"
    if parsed.port is not None:
        server += f":{parsed.port}"
    config: dict[str, str] = {"server": server}
    if parsed.username is not None:
        config["username"] = unquote(parsed.username)
    if parsed.password is not None:
        config["password"] = unquote(parsed.password)
    return config


async def start_dnsrobot_browser(proxy: str | None = None):
    """Start the optional Chromium runtime used by the literal DNS mode.

    The caller owns both returned objects and must close the browser followed
    by the Playwright runtime. Browser installation is intentionally separate:
    ``python -m playwright install chromium`` is a setup/deployment step, not a
    runtime network action.
    """

    if async_playwright is None:
        raise RuntimeError(
            "Playwright is not installed; run 'python -m pip install -r requirements.txt'")

    runtime = await async_playwright().start()
    try:
        browser = await runtime.chromium.launch(
            headless=True,
            proxy=playwright_proxy_config(proxy),
            args=["--disable-dev-shm-usage"],
        )
    except Exception:
        await runtime.stop()
        raise
    return runtime, browser


def dnsrobot_page_timeout_ms(deadline: float) -> int:
    """Convert a remaining seconds budget to Playwright's integer milliseconds."""

    return max(1, int(max(0.001, deadline - time.monotonic()) * 1000))


def _dnsrobot_page_is_on_site(page_url: object) -> bool:
    """Reject redirects away from the exact DNS Robot checker page."""

    if page_url is None:
        return True  # Small browser test doubles may not expose ``url``.
    if not isinstance(page_url, str) or not page_url:
        return False
    try:
        parsed = urlsplit(page_url)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/") or "/"
    return (
        parsed.scheme.lower() == "https"
        and hostname in DNSROBOT_ALLOWED_HOSTS
        and port in (None, 443)
        and path == "/username-checker"
    )


def dnsrobot_page_detail(raw_status: object | None, outcome: str) -> str:
    """Create a non-sensitive detail string for a rendered page result."""

    if isinstance(raw_status, str) and raw_status.strip():
        label = raw_status.strip()
        return f"DNS Robot page: {label}"
    if outcome == BLOCKED:
        return "DNS Robot page did not provide a definitive result"
    return "DNS Robot page returned an unrecognized result"


def dnsrobot_page_timeout_detail() -> str:
    return "DNS Robot page did not render a definitive Discord result before the deadline"


def _remaining_page_seconds(deadline: float) -> float:
    return max(0.001, deadline - time.monotonic())


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
            status, payload = await _fetch_json_get(
                session, _safe_url(template, username), proxy)
            outcome_status = interpret_minecraft(status, payload)
            detail = f"HTTP {status}"
            if status == 200 and outcome_status == BLOCKED:
                detail += " (unexpected profile response)"
            outcome = Result(
                "Minecraft", MINECRAFT_EMOJI,
                outcome_status, detail,
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

    normalized_username = username.lower()
    if not DISCORD_PATTERN.fullmatch(normalized_username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )

    url = api_url or DISCORD_ACCOUNT_API_URL
    url_error = validate_account_api_url(url)
    if url_error:
        return Result("Discord", DISCORD_EMOJI, ERROR, url_error)
    header_error = validate_request_headers(api_headers, "account API headers")
    if header_error:
        return Result("Discord", DISCORD_EMOJI, ERROR, header_error)

    try:
        status, payload = await _fetch_json(
            session, url, {"username": normalized_username}, proxy, api_headers)
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
    browser=None,
    browser_semaphore: asyncio.Semaphore | None = None,
    timeout: float | None = None,
) -> Result:
    """Check Discord by literally loading DNS Robot's username-checker page.

    A real Playwright browser navigates to
    ``https://dnsrobot.net/username-checker?u=<name>``. DNS Robot's own
    JavaScript then performs its browser-side check, and this adapter reads the
    rendered Discord card. Python never sends an account/probe header or token
    to either the page or its browser requests.

    ``session`` and ``proxy`` remain in the signature for checker compatibility.
    The proxy is applied when the browser is launched, not by adding headers to
    the page. The caller owns the browser lifecycle.
    """

    del session, proxy  # The browser, not aiohttp, performs this mode's request.
    normalized_username = username.lower()
    if not DISCORD_PATTERN.fullmatch(normalized_username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )
    if browser is None:
        return Result(
            "Discord", DISCORD_EMOJI, ERROR,
            "DNS Robot browser is unavailable; install Chromium and start Playwright",
        )

    page_budget = max(0.05, timeout if timeout is not None else 3.0)
    deadline = time.monotonic() + page_budget
    context = None

    async def read_page() -> Result:
        nonlocal context
        context = await browser.new_context(
            user_agent=BROWSER_HEADERS["User-Agent"],
            locale="en-US",
        )
        page = await context.new_page()
        page_url = dnsrobot_username_checker_url(username)
        await page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=dnsrobot_page_timeout_ms(deadline),
        )
        if not _dnsrobot_page_is_on_site(getattr(page, "url", None)):
            return Result(
                "Discord", DISCORD_EMOJI, BLOCKED,
                "DNS Robot redirected away from its username-checker page",
            )
        await page.wait_for_function(
            DNSROBOT_PAGE_STATUS_SCRIPT,
            timeout=dnsrobot_page_timeout_ms(deadline),
        )
        raw_status = await page.evaluate(DNSROBOT_PAGE_STATUS_SCRIPT)
        outcome = interpret_discord_dnsrobot_page(raw_status)
        return Result(
            "Discord", DISCORD_EMOJI, outcome,
            dnsrobot_page_detail(raw_status, outcome),
        )

    try:
        if browser_semaphore is None:
            return await read_page()
        async with browser_semaphore:
            return await read_page()
    except asyncio.CancelledError:
        raise
    except PlaywrightTimeoutError:
        return Result("Discord", DISCORD_EMOJI, BLOCKED, dnsrobot_page_timeout_detail())
    except Exception as exc:  # noqa: BLE001 - browser failures remain unknown
        return _request_error("Discord", DISCORD_EMOJI, exc)
    finally:
        if context is not None:
            try:
                await context.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - cleanup must not leak a task
                # The primary result is already safe; only log the sanitized
                # cleanup failure through the returned error path when needed.
                _redact_sensitive_text(exc)


async def check_discord(
    session,
    username: str,
    proxy=None,
    mode: str = "off",
    probe_url: str | None = None,
    probe_headers: Mapping[str, str] | None = None,
    account_api_url: str | None = None,
    account_api_headers: Mapping[str, str] | None = None,
    dnsrobot_browser=None,
    dnsrobot_semaphore: asyncio.Semaphore | None = None,
    dnsrobot_timeout: float | None = None,
) -> Result:
    """Check Discord in off, DNS Robot, account, or probe mode.

    ``off`` is the safe default. ``dnsrobot`` literally loads
    ``https://dnsrobot.net/username-checker?u=...`` in the supplied Playwright
    browser and reads its rendered Discord result. ``account`` (also accepted as
    ``account_api``) sends a JSON eligibility request to the configured account
    API and interprets its strict boolean response. ``probe`` remains available
    for an external GET checker using the 200/404 contract. No mode treats
    ``discord.com/<username>`` as an availability endpoint.
    """

    mode = (mode or "off").strip().lower()
    if mode == "off":
        return Result(
            "Discord", DISCORD_EMOJI, SKIPPED,
            "check disabled (DISCORD_CHECK_MODE=off)",
        )
    if mode == "dnsrobot":
        return await check_discord_dnsrobot(
            session,
            username,
            proxy,
            browser=dnsrobot_browser,
            browser_semaphore=dnsrobot_semaphore,
            timeout=dnsrobot_timeout,
        )
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
    header_error = validate_request_headers(probe_headers, "probe headers")
    if header_error:
        return Result("Discord", DISCORD_EMOJI, ERROR, header_error)
    normalized_username = username.lower()
    if not DISCORD_PATTERN.fullmatch(normalized_username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )

    try:
        url = _safe_url(probe_url, normalized_username)
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
            result = await checker
        else:
            result = await asyncio.wait_for(checker, timeout=max(0.0, timeout))
        if not isinstance(result, Result):
            raise TypeError("checker returned an invalid result")
        return result
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
    dnsrobot_browser=None,
    dnsrobot_semaphore: asyncio.Semaphore | None = None,
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
                session,
                username,
                proxy=proxy,
                mode=discord_mode,
                probe_url=discord_probe_url,
                probe_headers=discord_probe_headers,
                account_api_url=discord_account_api_url,
                account_api_headers=discord_account_api_headers,
                dnsrobot_browser=dnsrobot_browser,
                dnsrobot_semaphore=dnsrobot_semaphore,
                dnsrobot_timeout=timeout,
            ),
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
        help=("Discord check mode (default: off; dnsrobot loads the DNS Robot page "
              "in Chromium; account_api is a compatibility alias)"))
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
    browser_runtime = None
    dnsrobot_browser = None
    dnsrobot_semaphore = None
    if ns.mode == "dnsrobot":
        try:
            browser_runtime, dnsrobot_browser = await start_dnsrobot_browser(ns.proxy)
            dnsrobot_semaphore = asyncio.Semaphore(2)
        except Exception as exc:  # noqa: BLE001 - report a safe unknown result
            print(
                "DNS Robot browser unavailable; its result will be ERROR: "
                f"{_redact_sensitive_text(exc)}",
                file=sys.stderr,
            )

    try:
        async with aiohttp.ClientSession(
                headers=BROWSER_HEADERS, timeout=request_timeout) as session:
            results = await run_all_checks(
                session,
                ns.username,
                ns.proxy,
                discord_mode=ns.mode,
                discord_probe_url=ns.discord_probe_url,
                discord_account_api_url=ns.discord_account_api_url,
                timeout=deadline,
                dnsrobot_browser=dnsrobot_browser,
                dnsrobot_semaphore=dnsrobot_semaphore,
            )
    finally:
        if dnsrobot_browser is not None:
            try:
                await dnsrobot_browser.close()
            except Exception as exc:  # noqa: BLE001 - do not hide the report
                print(
                    f"DNS Robot browser cleanup failed: {_redact_sensitive_text(exc)}",
                    file=sys.stderr,
                )
        if browser_runtime is not None:
            try:
                await browser_runtime.stop()
            except Exception as exc:  # noqa: BLE001 - do not hide the report
                print(
                    f"Playwright cleanup failed: {_redact_sensitive_text(exc)}",
                    file=sys.stderr,
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
