"""
Platform username-availability checkers for the Multi-Sniper Discord bot.

Each checker maps an HTTP response to one normalized status:

    AVAILABLE  the platform confirms that the name is free
    TAKEN      an existing profile/account was found
    INVALID    the name cannot be used on that platform
    BLOCKED    rate limit, challenge page, or anti-bot wall; result is unknown
    SKIPPED    checker deliberately disabled by configuration
    ERROR      timeout, network, or unexpected response failure

The bot calls ``run_all_checks`` with one shared deadline so all platform
checks run concurrently rather than adding their latencies together.

Supported platforms:
    Minecraft     Mojang profile API (with fallback endpoint)
    guns.lol      Profile page with unclaimed/challenge detection
    Discord       off | dnsrobot | instantusername | combined |
                  account | account_api | probe
    GitHub        Public user API (200/404 contract)
    Steam         Community profile page
    Reddit        JSON user-about endpoint
    Instagram     Web profile info API
    Twitter/X     Profile page status

Run a one-off report without starting Discord:

    python checkers.py Notch
    python checkers.py vortex --mode account
    python checkers.py zxqw99182vlt --mode probe \\
        --discord-probe-url 'https://checker.example/{username}'
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Collection, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlencode, urlsplit

import aiohttp
import logging
log = logging.getLogger(__name__)

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
GITHUB_EMOJI = "\U0001F4BB"      # 💻
STEAM_EMOJI = "\U0001F3AE"       # 🎮
REDDIT_EMOJI = "\U0001F440"      # 👀
INSTAGRAM_EMOJI = "\U0001F4F8"   # 📸
TWITTER_EMOJI = "\U0001F426"     # 🐦

# Kept in reaction order as well as timeout/error result order.
# The first three are core; the rest are optional fast checks.
PLATFORMS: tuple[tuple[str, str], ...] = (
    ("Minecraft", MINECRAFT_EMOJI),
    ("guns.lol", GUNSLOL_EMOJI),
    ("Discord", DISCORD_EMOJI),
    ("GitHub", GITHUB_EMOJI),
    ("Steam", STEAM_EMOJI),
    ("Reddit", REDDIT_EMOJI),
    ("Instagram", INSTAGRAM_EMOJI),
    ("Twitter/X", TWITTER_EMOJI),
)

# Core platforms that always run (Discord is SKIPPED when mode=off but still counted).
CORE_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("Minecraft", MINECRAFT_EMOJI),
    ("guns.lol", GUNSLOL_EMOJI),
    ("Discord", DISCORD_EMOJI),
)

# Fast optional platforms (checked in parallel with core, very quick endpoints).
FAST_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("GitHub", GITHUB_EMOJI),
    ("Steam", STEAM_EMOJI),
    ("Reddit", REDDIT_EMOJI),
    ("Instagram", INSTAGRAM_EMOJI),
    ("Twitter/X", TWITTER_EMOJI),
)


# Per-platform opt-out list. On datacenter-grade hosting some platforms
# (Reddit, Twitter/X) wall automated traffic so hard that they mostly return
# Unknown; a deployment can skip them instead of showing noise.
def _platform_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


PLATFORM_LOOKUP: dict[str, str] = {_platform_key(p): p for p, _ in PLATFORMS}
# Common informal spellings; keep short, they resolve to canonical names.
PLATFORM_LOOKUP.update({"twitter": "Twitter/X", "x": "Twitter/X"})


def parse_disabled_platforms(raw: str) -> tuple[frozenset[str], list[str]]:
    """Map a comma list ("Reddit, Twitter") to canonical platform names.

    Returns (disabled, unknown_tokens): unknown tokens fail config validation
    loudly rather than being silently ignored, because a typo'd entry that
    does nothing is exactly how a user ends up believing Reddit is off when
    it is not.
    """

    disabled: set[str] = set()
    unknown: list[str] = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        match = PLATFORM_LOOKUP.get(_platform_key(token))
        if match is None:
            unknown.append(token)
        else:
            disabled.add(match)
    return frozenset(disabled), unknown


def active_platforms(
    include_extra: bool = True,
    disabled: Collection[str] = frozenset(),
) -> tuple[tuple[str, str], ...]:
    """The platform list honouring the extra-platform toggle and opt-outs."""

    base = PLATFORMS if include_extra else CORE_PLATFORMS
    if not disabled:
        return base
    return tuple((p, e) for p, e in base if p not in disabled)

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
    # Only advertise encodings aiohttp can always decode. Advertising "br"
    # without the optional Brotli package makes real sites answer with a body
    # aiohttp cannot read, turning good checks into ERROR results.
    "Accept-Encoding": "gzip, deflate",
}

# API-specific headers for JSON endpoints
API_HEADERS = {
    "User-Agent": BROWSER_HEADERS["User-Agent"],
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
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
GUNSLOL_PATTERN = re.compile(r"^[A-Za-z0-9._-]{2,24}$")
# Discord's new-style usernames: 2-32 chars, lowercase a-z 0-9 . _
DISCORD_PATTERN = re.compile(r"^[a-z0-9._]{2,32}$")
# GitHub usernames: 1-39 chars, alphanumeric and hyphens, cannot start/end with hyphen.
GITHUB_PATTERN = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$")
# Steam vanity URLs: alphanumeric, underscores, hyphens, 2-32 chars.
STEAM_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
# Reddit usernames: 3-20 chars, alphanumeric, underscore, hyphen.
REDDIT_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,20}$")
# Instagram usernames: 1-30 chars, alphanumeric, underscores, periods.
INSTAGRAM_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Twitter/X usernames: 1-15 chars, alphanumeric and underscore.
TWITTER_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")

# A Discord message must look like one bare username token before the bot
# spends any request budget on it.
USERNAME_MESSAGE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


# ---------------------------------------------------------------------------
# Status-code / page interpreters (pure functions - easy to unit test)
# ---------------------------------------------------------------------------

# guns.lol currently serves some unclaimed names with an HTTP 200 page rather
# than an HTTP 404. These are intentionally narrow markers.
GUNSLOL_UNCLAIMED_MARKERS = (
    "username not found",
    "this user is not claimed",
    "<title>everything you want",
)
_MISSING_PAYLOAD = object()


def _normalize_page(page: str) -> str:
    """Casefold page text and fold typographic quotes to ASCII.

    Instagram and X render "doesn\u2019t" with a typographic apostrophe, so a
    literal "doesn't" marker would never match the page they actually serve.
    """

    return (page.replace("\u2019", "'").replace("\u2018", "'")
                .replace("\u201c", '"').replace("\u201d", '"')
                .casefold())


GUNSLOL_CHALLENGE_MARKERS = (
    "just a moment...",
    "attention required",
    "cf-chl-",
    "/cdn-cgi/challenge-platform",
)


# Instagram and X are matched against narrow, page-specific phrases. Broad
# words such as "challenge" or "captcha" appear inside ordinary profile HTML
# (bios, scripts, CSS class names) and used to mark live profiles as BLOCKED.
INSTAGRAM_MISSING_MARKERS = (
    "sorry, this page isn't available",
    "the link you followed may be broken",
    "page not found \u2022 instagram",
)
INSTAGRAM_BLOCKED_MARKERS = (
    "login \u2022 instagram",
    "log in to instagram",
    "login to instagram",
    "checkpoint_required",
    "/challenge/",
    "please wait a few minutes before you try again",
)
TWITTER_MISSING_MARKERS = (
    "this account doesn't exist",
    "this user doesn't exist",
    "hmm...this page doesn't exist",
    "this page doesn't exist",
)
TWITTER_BLOCKED_MARKERS = (
    "rate limit exceeded",
    "solve this captcha",
    "confirm you're not a robot",
    "arkose",
    "/i/flow/login",
    "something went wrong, but don't fret",
)


def interpret_minecraft(
    status: int,
    payload: object = _MISSING_PAYLOAD,
) -> str:
    if status == 200:
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
    """Interpret guns.lol's status plus its small semantic error page."""

    if status == 200:
        if page is None:
            return TAKEN
        if not isinstance(page, str) or not page.strip():
            return BLOCKED
        content = _normalize_page(page)
        if any(marker in content for marker in GUNSLOL_CHALLENGE_MARKERS):
            return BLOCKED
        if any(marker in content for marker in GUNSLOL_UNCLAIMED_MARKERS):
            return AVAILABLE
        return TAKEN
    if status in (404, 410):
        if isinstance(page, str) and any(
                marker in _normalize_page(page)
                for marker in GUNSLOL_CHALLENGE_MARKERS):
            return BLOCKED
        return AVAILABLE
    if status == 400:
        return INVALID
    if status in (403, 429, 503):
        return BLOCKED         # Cloudflare challenge / rate limit
    return ERROR


def interpret_discord_probe(status: int) -> str:
    """Interpret the documented contract for an authorized external checker."""

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
    """Interpret a JSON response from Discord's account username check."""

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
                return ERROR
            boolean_outcomes.append(
                (TAKEN if value else AVAILABLE) if key == "taken" else (
                    AVAILABLE if value else TAKEN))

    boolean_outcome: str | None = None
    if boolean_outcomes:
        if len(set(boolean_outcomes)) != 1:
            return ERROR
        boolean_outcome = boolean_outcomes[0]

    numeric_outcome: str | None = None
    if isinstance(data, Mapping):
        check = data.get("check")
        if isinstance(check, Mapping) and "status" in check:
            account_status = check["status"]
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
    """Interpret a recorded JSON response from DNS Robot's browser flow."""
    return interpret_discord_account_api(status, payload)


def interpret_discord_dnsrobot_page(status: object | None) -> str:
    """Map the visible Discord card on DNS Robot's page to a safe status."""

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


# Backwards-friendly short name
interpret_discord_account = interpret_discord_account_api


# ---------------------------------------------------------------------------
# New platform interpreters
# ---------------------------------------------------------------------------

def interpret_github(status: int, payload: object | None = None) -> str:
    """Interpret GitHub's public user API response.

    200 with a login field = taken. 404 = available.
    403/429 = rate limited (BLOCKED). Other = error.
    """
    if status == 200:
        if payload is None:
            return TAKEN
        if isinstance(payload, Mapping) and isinstance(payload.get("login"), str):
            return TAKEN
        return BLOCKED  # 200 but not a valid user response
    if status == 404:
        return AVAILABLE
    if status in (403, 429):
        return BLOCKED
    return ERROR


def interpret_steam(status: int, page: str | None = None) -> str:
    """Interpret Steam community profile page.

    Steam returns 200 for existing profiles and redirects or special pages
    for non-existing ones. The page body can contain "The specified profile
    could not be found" for missing profiles.
    """
    if status == 200:
        if not isinstance(page, str) or not page.strip():
            return BLOCKED
        content = _normalize_page(page)
        # Steam serves a "profile not found" page with 200 for missing profiles
        if "the specified profile could not be found" in content:
            return AVAILABLE
        # Cloudflare/challenge detection
        if any(marker in content for marker in ("just a moment...", "cf-chl-")):
            return BLOCKED
        return TAKEN
    if status == 404:
        return AVAILABLE
    if status in (403, 429, 503):
        return BLOCKED
    return ERROR


def interpret_reddit(status: int, payload: object | None = None) -> str:
    """Interpret Reddit's JSON user-about endpoint.

    200 with user data = taken. 404 = available.
    Reddit returns 404 JSON for missing users.
    """
    if status == 200:
        if payload is None:
            return BLOCKED
        if isinstance(payload, Mapping):
            # Reddit returns {"data": {...}} for existing users
            data = payload.get("data")
            if isinstance(data, Mapping) and data.get("name"):
                return TAKEN
        return BLOCKED  # 200 but not a valid user response
    if status == 404:
        return AVAILABLE
    if status in (403, 429, 503):
        return BLOCKED
    return ERROR


def interpret_instagram(status: int, page: str | None = None) -> str:
    """Interpret Instagram profile page status.

    Instagram returns 200 for existing profiles and may redirect or return
    login pages. This is a best-effort check since Instagram aggressively
    blocks non-authenticated requests.
    """
    if status == 200:
        if not isinstance(page, str) or not page.strip():
            return BLOCKED
        content = _normalize_page(page)
        # A missing profile is the strongest signal, so test it first: a login
        # wall page can also mention "log in", and the old ordering made every
        # free name look BLOCKED.
        if any(marker in content for marker in INSTAGRAM_MISSING_MARKERS):
            return AVAILABLE
        if any(marker in content for marker in INSTAGRAM_BLOCKED_MARKERS):
            return BLOCKED
        return TAKEN
    if status == 404:
        return AVAILABLE
    if status in (302, 301):
        # Redirects may indicate a login wall
        return BLOCKED
    if status in (401, 403, 429):
        return BLOCKED
    return ERROR


def interpret_twitter(status: int, page: str | None = None) -> str:
    """Interpret Twitter/X profile page status.

    Twitter returns 200 for existing profiles. For non-existing profiles,
    the page may still return 200 but with "This account doesn't exist"
    or 404. Due to heavy JS requirements, this is best-effort.
    """
    if status == 200:
        if not isinstance(page, str) or not page.strip():
            return BLOCKED
        content = _normalize_page(page)
        if any(marker in content for marker in TWITTER_MISSING_MARKERS):
            return AVAILABLE
        if any(marker in content for marker in TWITTER_BLOCKED_MARKERS):
            return BLOCKED
        return TAKEN
    if status == 404:
        return AVAILABLE
    if status in (403, 429):
        return BLOCKED
    return ERROR


# ---------------------------------------------------------------------------
# Low-level fetch helpers
# ---------------------------------------------------------------------------

try:
    _AIOHTTP_VERSION = tuple(
        int(part) for part in aiohttp.__version__.split(".")[:2])
except (ValueError, AttributeError):  # pragma: no cover - odd dev builds
    _AIOHTTP_VERSION = (3, 9)


def make_fast_connector(
    limit: int,
    limit_per_host: int,
    *,
    force_close: bool = False,
    keepalive_timeout: float = 30.0,
) -> aiohttp.TCPConnector:
    """Build the fastest pooled connector the installed aiohttp supports.

    - DNS results are cached (5 min) so repeat checks skip re-resolution.
    - Happy Eyeballs (aiohttp >= 3.10) races IPv4/IPv6 and connects to
      whichever answers first, shaving setup latency on dual-stack hosts.
    - Keep-alive connections stay pooled between lookups.
    - ``enable_cleanup_closed`` is only passed on aiohttp versions that want
      it; newer ones deprecate it.
    """

    kwargs: dict[str, object] = {
        "limit": limit,
        "limit_per_host": limit_per_host,
        "ttl_dns_cache": 300,
        "use_dns_cache": True,
        "force_close": force_close,
        "keepalive_timeout": keepalive_timeout,
    }
    params = inspect.signature(aiohttp.TCPConnector.__init__).parameters
    if "happy_eyeballs_delay" in params:
        kwargs["happy_eyeballs_delay"] = 0.25
    if "enable_cleanup_closed" in params and _AIOHTTP_VERSION < (3, 12):
        kwargs["enable_cleanup_closed"] = True
    return aiohttp.TCPConnector(**kwargs)


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
    r"(?i)\b((?:proxy-)?authorization|(?:x-)?api[_-]?key|token):\s*"
    r"(?:bearer|token)?\s*[^\s,]+")
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")


def _has_http_control_chars(value: object) -> bool:
    return isinstance(value, str) and any(
        ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _decoded_url_component_has_control_chars(value: str | None) -> bool:
    return value is not None and _has_http_control_chars(unquote(value))


# ---------------------------------------------------------------------------
# Request layer: proxy resolution, health reporting, and transient retries
# ---------------------------------------------------------------------------

# Failures worth retrying once: connection resets, proxy hiccups, timeouts.
# A definitive HTTP status is never retried, and neither is a ValueError from
# a malformed URL, which would fail identically every time.
_TRANSIENT_ERRORS = (
    aiohttp.ClientConnectionError,
    aiohttp.ClientPayloadError,
    aiohttp.ServerTimeoutError,
    asyncio.TimeoutError,
)
# Failures that say something about the proxy rather than the target site.
_PROXY_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

DEFAULT_MAX_RETRIES = 1
DEFAULT_RETRY_DELAY = 0.05


def _resolve_proxy(proxy: object) -> str | None:
    """Resolve a proxy: accept a string, a callable factory, or ``None``."""

    if proxy is None:
        return None
    if callable(proxy):
        return proxy()
    return proxy if isinstance(proxy, str) else None


def _report_proxy(proxy: object, url: str | None, *, ok: bool) -> None:
    """Feed one request outcome back to a pool that wants to hear about it.

    Pools expose ``report_success`` / ``report_failure``; plain strings and
    ``None`` do not, so this is a no-op for them. Reporting must never be able
    to break a check, hence the broad guard.
    """

    if not url:
        return
    reporter = getattr(proxy, "report_success" if ok else "report_failure", None)
    if not callable(reporter):
        return
    try:
        reporter(url)
    except Exception as exc:  # noqa: BLE001
        log.debug("Proxy health reporting failed: %s", exc)


async def _with_proxy(
    proxy: object,
    operation,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_RETRY_DELAY,
):
    """Run ``operation(proxy_url)`` with rotation, reporting, and one retry.

    The proxy is resolved *per attempt*, so a retry automatically lands on the
    next proxy in the rotation instead of hammering the one that just failed.
    """

    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        resolved = _resolve_proxy(proxy)
        try:
            result = await operation(resolved)
        except _PROXY_ERRORS as exc:
            _report_proxy(proxy, resolved, ok=False)
            last_exc = exc
            if attempt < max_retries and isinstance(exc, _TRANSIENT_ERRORS):
                delay = base_delay * (2 ** attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
                continue
            raise
        else:
            _report_proxy(proxy, resolved, ok=True)
            return result
    raise last_exc  # pragma: no cover - loop always returns or raises


async def _read_json_body(response: aiohttp.ClientResponse) -> object | None:
    """Decode a JSON body, keeping malformed successful bodies distinguishable."""

    try:
        try:
            return await response.json(content_type=None)
        except TypeError:
            return await response.json()
    except (TypeError, ValueError, aiohttp.ContentTypeError):
        return None


# ---------------------------------------------------------------------------
# Fetch functions (with proxy pool support)
# ---------------------------------------------------------------------------

async def _fetch_status(
    session: aiohttp.ClientSession,
    url: str,
    proxy: object = None,
    headers: Mapping[str, str] | None = None,
) -> int:
    """GET one endpoint and return its status without following redirects."""

    async def attempt(resolved_proxy: str | None) -> int:
        async with session.get(
            url,
            proxy=resolved_proxy,
            headers=headers,
            allow_redirects=False,
        ) as response:
            return response.status

    return await _with_proxy(proxy, attempt)


async def _fetch_json_get(
    session: aiohttp.ClientSession,
    url: str,
    proxy: object = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, object | None]:
    """GET JSON and keep malformed successful bodies distinguishable."""

    async def attempt(resolved_proxy: str | None) -> tuple[int, object | None]:
        async with session.get(
            url, proxy=resolved_proxy, headers=headers,
        ) as response:
            return response.status, await _read_json_body(response)

    return await _with_proxy(proxy, attempt)


async def _fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    payload: Mapping[str, object],
    proxy: object = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, object | None]:
    """POST JSON and return the status plus a decoded response, if any."""

    body = dict(payload)

    async def attempt(resolved_proxy: str | None) -> tuple[int, object | None]:
        async with session.post(
            url,
            json=body,
            proxy=resolved_proxy,
            headers=headers,
            allow_redirects=False,
        ) as response:
            return response.status, await _read_json_body(response)

    return await _with_proxy(proxy, attempt)


# Profile pages can be megabytes of JS. Every marker this bot looks for lives
# in the first screenful of markup (title, meta, error banner), so the body is
# read only up to this cap and the rest of the transfer is abandoned.
MAX_PAGE_BYTES = 96 * 1024


async def _fetch_page(
    session: aiohttp.ClientSession,
    url: str,
    proxy: object = None,
    headers: Mapping[str, str] | None = None,
    max_bytes: int = MAX_PAGE_BYTES,
) -> tuple[int, str]:
    """GET a URL and return its status and a bounded prefix of its HTML.

    Reading a capped prefix instead of the whole body is a large latency win
    on the page-scraped platforms (Steam, Instagram, X) and cannot change a
    verdict: the markers are always near the top of the document.
    """

    async def attempt(resolved_proxy: str | None) -> tuple[int, str]:
        async with session.get(
            url, proxy=resolved_proxy, headers=headers, allow_redirects=False,
        ) as response:
            if max_bytes and max_bytes > 0:
                # StreamReader.read(n) returns only what is buffered *right
                # now* - a single call can stop mid-way through the first
                # network chunk, which on a slow proxy could truncate the
                # prefix before the markers. Accumulate up to the cap (or
                # EOF) so the marker search always sees the full prefix.
                chunks: list[bytes] = []
                remaining = max_bytes
                while remaining > 0:
                    chunk = await response.content.read(remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                encoding = response.charset or "utf-8"
                try:
                    text = raw.decode(encoding, errors="replace")
                except LookupError:  # server advertised a charset Python lacks
                    text = raw.decode("utf-8", errors="replace")
            else:
                text = await response.text(errors="replace")
            return response.status, text

    return await _with_proxy(proxy, attempt)


def _safe_url(template: str, username: str) -> str:
    """Substitute only the literal ``{username}`` field in a URL template."""

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
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return f"{label} is not a valid URL"
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not hostname:
        return f"{label} must be an absolute http:// or https:// URL"
    try:
        _ = parsed.port
    except ValueError:
        return f"{label} has an invalid port"
    if any(char.isspace() for char in url):
        return f"{label} must not contain whitespace"
    if (_decoded_url_component_has_control_chars(parsed.username)
            or _decoded_url_component_has_control_chars(parsed.password)):
        return f"{label} credentials must not contain control characters"
    return None


def validate_proxy_url(url: str) -> str | None:
    """Validate a proxy URL accepted by both aiohttp and Playwright."""

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
    """Validate the user-supplied external-checker URL template."""

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
    text = " ".join(text.split())
    return text[:120] or type(value).__name__


def _request_error(platform: str, emoji: str, exc: Exception) -> Result:
    return Result(platform, emoji, ERROR, _redact_sensitive_text(exc))


# ---------------------------------------------------------------------------
# Async platform checkers
# ---------------------------------------------------------------------------

# Mojang occasionally returns random 403s at api.mojang.com, so retry the
# equivalent minecraftservices lookup once.
# How long the primary Mojang endpoint gets before the backup is hedged in.
MINECRAFT_HEDGE_DELAY = 0.15

MINECRAFT_ENDPOINTS: Sequence[str] = (
    "https://api.mojang.com/users/profiles/minecraft/{username}",
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/{username}",
)

# Hosts contacted by the checkers, used to pre-open pooled TLS connections.
# Keyed by platform so deployments that disable a platform can also skip
# warming its socket.
PREWARM_HOSTS: tuple[tuple[str, str], ...] = (
    ("Minecraft", "https://api.mojang.com/"),
    ("Minecraft", "https://api.minecraftservices.com/"),
    ("guns.lol", "https://guns.lol/"),
    ("GitHub", "https://github.com/"),
    ("Steam", "https://steamcommunity.com/"),
    ("Reddit", "https://www.reddit.com/"),
    ("Instagram", "https://www.instagram.com/"),
    ("Twitter/X", "https://x.com/"),
)
PREWARM_URLS: tuple[str, ...] = tuple(url for _, url in PREWARM_HOSTS)


def prewarm_urls(
    include_extra: bool = True,
    disabled: Collection[str] = frozenset(),
) -> tuple[str, ...]:
    """The warm-up host list honouring the same toggles as the fan-out."""

    active = {name for name, _ in active_platforms(include_extra, disabled)}
    return tuple(url for name, url in PREWARM_HOSTS if name in active)

DEFAULT_DISCORD_ACCOUNT_API_URL = (
    "https://discord.com/api/v10/unique-username/"
    "username-attempt-unauthed"
)
DISCORD_ACCOUNT_API_URL = DEFAULT_DISCORD_ACCOUNT_API_URL

DEFAULT_DISCORD_DNSROBOT_URL = "https://dnsrobot.net/username-checker"
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
    if not hostname:
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
    """Start the optional Chromium runtime used by the literal DNS mode."""

    if async_playwright is None:
        raise RuntimeError(
            "Playwright is not installed; run "
            "\"python -m pip install 'playwright>=1.48,<2'\" and then "
            "'python -m playwright install chromium'")

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
        return True
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


# ---------------------------------------------------------------------------
# Platform checkers (async)
# ---------------------------------------------------------------------------

async def _minecraft_lookup(session, template: str, username: str, proxy) -> Result:
    """One Mojang endpoint lookup, normalized into a Result."""

    try:
        status, payload = await _fetch_json_get(
            session, _safe_url(template, username), proxy)
    except _REQUEST_ERRORS as exc:
        return _request_error("Minecraft", MINECRAFT_EMOJI, exc)
    outcome_status = interpret_minecraft(status, payload)
    detail = f"HTTP {status}"
    if status == 200 and outcome_status == BLOCKED:
        detail += " (unexpected profile response)"
    return Result("Minecraft", MINECRAFT_EMOJI, outcome_status, detail)


async def check_minecraft(session, username: str, proxy=None) -> Result:
    """Check Minecraft/Mojang availability (emoji 🕹️).

    Mojang hands out sporadic 403s at api.mojang.com, so a second endpoint
    exists as a fallback. It is issued as a *hedged* request: the backup only
    starts if the primary has not answered within MINECRAFT_HEDGE_DELAY, and
    the first definitive answer wins with the other cancelled. A healthy
    primary therefore still costs exactly one request, while a slow or blocked
    one no longer doubles this check's latency.
    """

    if not MINECRAFT_PATTERN.fullmatch(username):
        return Result(
            "Minecraft", MINECRAFT_EMOJI, INVALID,
            "name must be 3-16 chars of A-Z a-z 0-9 _",
        )

    async def hedged(template: str, delay: float):
        if delay > 0:
            await asyncio.sleep(delay)
        return await _minecraft_lookup(session, template, username, proxy)

    tasks = [
        asyncio.ensure_future(hedged(template, index * MINECRAFT_HEDGE_DELAY))
        for index, template in enumerate(MINECRAFT_ENDPOINTS)
    ]
    fallback: Result | None = None
    try:
        for completed in asyncio.as_completed(tasks):
            try:
                outcome = await completed
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                fallback = fallback or _request_error(
                    "Minecraft", MINECRAFT_EMOJI, exc)
                continue
            if outcome.status not in (BLOCKED, ERROR):
                return outcome        # definitive: stop waiting for the rest
            fallback = fallback or outcome
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    return fallback or Result(
        "Minecraft", MINECRAFT_EMOJI, ERROR, "no endpoint attempted")


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
    """Check a username through an explicitly enabled account API."""

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


# Short alias
check_discord_account = check_discord_account_api


async def check_discord_dnsrobot(
    session,
    username: str,
    proxy=None,
    browser=None,
    browser_semaphore: asyncio.Semaphore | None = None,
    timeout: float | None = None,
) -> Result:
    """Check Discord by literally loading DNS Robot's username-checker page."""

    del session, proxy
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
    except Exception as exc:  # noqa: BLE001
        return _request_error("Discord", DISCORD_EMOJI, exc)
    finally:
        if context is not None:
            try:
                await context.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _redact_sensitive_text(exc)


async def check_discord_instantusername(
    session,
    username: str,
    proxy: object = None,
) -> Result:
    """Check Discord via instantusername.com's Discord service (no browser).

    Plain HTTP, no credentials: the same API contract as every other
    platform's instantusername check, aimed at their ``discord`` service.
    """

    normalized_username = username.lower()
    if not DISCORD_PATTERN.fullmatch(normalized_username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )
    return await check_instantusername(
        session, "Discord", DISCORD_EMOJI, normalized_username, proxy)


async def check_discord_combined(
    session,
    username: str,
    proxy=None,
    browser=None,
    browser_semaphore: asyncio.Semaphore | None = None,
    timeout: float | None = None,
) -> Result:
    """Race instantusername.com and the DNS Robot page; first agreement wins.

    instantusername.com answers in plain HTTP tenths of a second; the DNS
    Robot page drives Discord's own eligibility check in a real browser, so
    its verdict outranks the aggregator when they disagree. Whichever source
    is definitive first is held for a short window so the other can confirm
    or overrule it; if only one source produces an answer at all, that
    answer is used. On hosts without Chromium the browser leg is skipped
    and instantusername.com carries the check alone.
    """

    normalized_username = username.lower()
    if not DISCORD_PATTERN.fullmatch(normalized_username):
        return Result(
            "Discord", DISCORD_EMOJI, INVALID,
            "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _",
        )

    instant_task = asyncio.ensure_future(check_instantusername(
        session, "Discord", DISCORD_EMOJI, normalized_username, proxy))
    browser_task = (
        asyncio.ensure_future(check_discord_dnsrobot(
            session, normalized_username,
            browser=browser, browser_semaphore=browser_semaphore,
            timeout=timeout))
        if browser is not None else None
    )

    web_result: Result | None = None
    browser_result: Result | None = None
    try:
        pending = {instant_task} | ({browser_task} if browser_task else set())
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                result = await _task_result(task, "Discord", DISCORD_EMOJI)
                if task is browser_task:
                    browser_result = result
                else:
                    web_result = result
            if browser_result is not None and browser_result.status in (
                    AVAILABLE, TAKEN):
                return browser_result      # the site's own verdict wins
            if web_result is not None and web_result.status in (
                    AVAILABLE, TAKEN):
                if browser_task is None:
                    return web_result      # no second source: trust it
                # Give the browser a beat to confirm or contradict before
                # publishing the aggregator's word alone.
                if browser_task.done():
                    browser_result = await _task_result(
                        browser_task, "Discord", DISCORD_EMOJI)
                    if browser_result.status in (AVAILABLE, TAKEN):
                        return browser_result
                    return web_result      # browser was inconclusive
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(browser_task), COMBINED_BROWSER_GRACE)
                    browser_result = result
                    if result.status in (AVAILABLE, TAKEN):
                        return result      # browser confirmed/overruled
                    return web_result
                except asyncio.TimeoutError:
                    log.info(
                        "Discord: instantusername.com says %s; DNS Robot "
                        "still loading, publishing web verdict",
                        web_result.status)
                    return web_result

        # Everything finished without a definitive answer: prefer the
        # first-party-ish browser result, then the aggregator's.
        if browser_result is not None:
            return browser_result
        if web_result is not None:
            return web_result
        return Result("Discord", DISCORD_EMOJI, ERROR, "no answer received")
    finally:
        for task in (instant_task, browser_task):
            if task is not None and not task.done():
                task.cancel()
                task.add_done_callback(_consume_late_task)


# Grace period the aggregator's definitive Discord answer waits for the DNS
# Robot page to confirm or overrule it before being published on its own.
COMBINED_BROWSER_GRACE = 0.5


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
    """Check Discord in off, DNS Robot, instantusername, combined, account, or probe mode."""

    mode = (mode or "off").strip().lower()
    if mode == "off":
        return Result(
            "Discord", DISCORD_EMOJI, SKIPPED,
            "check disabled (DISCORD_CHECK_MODE=off)",
        )
    if mode == "instantusername":
        return await check_discord_instantusername(
            session, username, proxy)
    if mode == "combined":
        return await check_discord_combined(
            session, username, proxy,
            browser=dnsrobot_browser,
            browser_semaphore=dnsrobot_semaphore,
            timeout=dnsrobot_timeout,
        )
    if mode == "dnsrobot":
        return await check_discord_dnsrobot(
            session, username, proxy,
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
            "DISCORD_CHECK_MODE must be off, dnsrobot, instantusername, "
            "combined, account, account_api, or probe",
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
# New platform checkers
# ---------------------------------------------------------------------------

async def check_github(session, username: str, proxy=None) -> Result:
    """Check GitHub username availability (💻).

    Uses the profile page's status code (200 = taken, 404 = free) rather
    than the JSON API: the same contract, without the unauthenticated API's
    60-requests-per-hour-per-IP limit that a busy channel exhausts in
    minutes. Status-only, so no response body is parsed.
    """

    if not GITHUB_PATTERN.fullmatch(username):
        return Result(
            "GitHub", GITHUB_EMOJI, INVALID,
            "name must be 1-39 chars of A-Z a-z 0-9 - (no leading/trailing -)",
        )

    try:
        status = await _fetch_status(
            session,
            f"https://github.com/{quote(username, safe='')}",
            proxy,
        )
        outcome = interpret_github(status)
        return Result("GitHub", GITHUB_EMOJI, outcome, f"HTTP {status}")
    except _REQUEST_ERRORS as exc:
        return _request_error("GitHub", GITHUB_EMOJI, exc)


async def check_steam(session, username: str, proxy=None) -> Result:
    """Check Steam community username availability (🎮).

    Uses the community profile page. Steam may return 200 with a
    'profile not found' page for missing profiles.
    """

    if not STEAM_PATTERN.fullmatch(username):
        return Result(
            "Steam", STEAM_EMOJI, INVALID,
            "name must be 2-32 chars of A-Z a-z 0-9 _ -",
        )

    try:
        status, page = await _fetch_page(
            session,
            f"https://steamcommunity.com/id/{username}/",
            proxy,
        )
        outcome = interpret_steam(status, page)
        detail = f"HTTP {status}"
        if status == 200 and outcome == AVAILABLE:
            detail += " (profile not found page)"
        return Result("Steam", STEAM_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("Steam", STEAM_EMOJI, exc)


async def check_reddit(session, username: str, proxy=None) -> Result:
    """Check Reddit username availability (👀).

    Uses Reddit's JSON user-about endpoint: 200 = taken, 404 = free.
    """

    if not REDDIT_PATTERN.fullmatch(username):
        return Result(
            "Reddit", REDDIT_EMOJI, INVALID,
            "name must be 3-20 chars of A-Z a-z 0-9 _ -",
        )

    try:
        status, payload = await _fetch_json_get(
            session,
            f"https://www.reddit.com/user/{username}/about.json",
            proxy,
            headers={
                **API_HEADERS,
                "User-Agent": "MultiSniper/2.0 (username checker bot)",
            },
        )
        outcome = interpret_reddit(status, payload)
        detail = f"HTTP {status}"
        if status == 200 and isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                detail += f" (name={data.get('name', '?')})"
        return Result("Reddit", REDDIT_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("Reddit", REDDIT_EMOJI, exc)


async def check_instagram(session, username: str, proxy=None) -> Result:
    """Check Instagram username availability (📸).

    Instagram aggressively blocks non-authenticated and non-browser requests.
    This is a best-effort check using the web profile page.
    """

    if not INSTAGRAM_PATTERN.fullmatch(username):
        return Result(
            "Instagram", INSTAGRAM_EMOJI, INVALID,
            "name must be 1-30 chars of A-Z a-z 0-9 . _",
        )

    try:
        status, page = await _fetch_page(
            session,
            f"https://www.instagram.com/{username}/",
            proxy,
        )
        outcome = interpret_instagram(status, page)
        detail = f"HTTP {status}"
        if outcome == BLOCKED:
            detail += " (likely login wall)"
        return Result("Instagram", INSTAGRAM_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("Instagram", INSTAGRAM_EMOJI, exc)


async def check_twitter(session, username: str, proxy=None) -> Result:
    """Check Twitter/X username availability (🐦).

    Twitter/X is heavily JS-dependent and blocks most non-browser requests.
    This is a best-effort check using the profile page status.
    """

    if not TWITTER_PATTERN.fullmatch(username):
        return Result(
            "Twitter/X", TWITTER_EMOJI, INVALID,
            "name must be 1-15 chars of A-Z a-z 0-9 _",
        )

    try:
        status, page = await _fetch_page(
            session,
            f"https://x.com/{username}",
            proxy,
        )
        outcome = interpret_twitter(status, page)
        detail = f"HTTP {status}"
        if outcome == BLOCKED:
            detail += " (rate limit or challenge)"
        return Result("Twitter/X", TWITTER_EMOJI, outcome, detail)
    except _REQUEST_ERRORS as exc:
        return _request_error("Twitter/X", TWITTER_EMOJI, exc)


# ---------------------------------------------------------------------------
# instantusername.com fallback provider
# ---------------------------------------------------------------------------
#
# When a platform's own endpoint stops answering usefully - Cloudflare wall,
# rate limit, login gate, network error - the check would otherwise report
# "Unknown". instantusername.com exposes a small credential-free JSON API that
# answers the same question, so it is used as a second opinion *only* for the
# platforms that came back inconclusive.
#
#   GET https://api.instantusername.com/services.json
#       -> {"services": [{"service": "GitHub",
#                         "endpoint": "/check/github/{username}"}, ...]}
#   GET https://api.instantusername.com/check/<service>/<username>
#       -> {"available": true|false, "url": "..."}

INSTANTUSERNAME_BASE_URL = "https://api.instantusername.com"
INSTANTUSERNAME_SERVICES_URL = f"{INSTANTUSERNAME_BASE_URL}/services.json"

# Our platform name -> their service slug. Seeded with the slugs the service
# has shipped for years; refresh_instantusername_services() can extend this at
# runtime from their live catalogue.
INSTANTUSERNAME_SERVICES: dict[str, str] = {
    "Discord": "discord",
    "Minecraft": "mc-java",
    "GitHub": "github",
    "Steam": "steam",
    "Reddit": "reddit",
    "Instagram": "instagram",
    "Twitter/X": "twitter",
}

# Their service names, normalized, mapped onto our platform names. Used when
# refreshing from the live catalogue so newly added services (for example
# Discord or Minecraft) are picked up without a code change.
_INSTANTUSERNAME_ALIASES: dict[str, str] = {
    "github": "GitHub",
    "steam": "Steam",
    "reddit": "Reddit",
    "instagram": "Instagram",
    "twitter": "Twitter/X",
    "x": "Twitter/X",
    "xtwitter": "Twitter/X",
    "discord": "Discord",
    "minecraft": "Minecraft",
}


def _normalize_service_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).casefold())


def interpret_instantusername(status: int, payload: object | None) -> str:
    """Map one instantusername.com check response to a normalized status."""

    if status == 404:
        # Unknown service slug: not an availability answer.
        return ERROR
    if status in (401, 403, 429):
        return BLOCKED
    if status != 200 or not isinstance(payload, Mapping):
        return ERROR
    available = payload.get("available")
    if type(available) is not bool:
        return ERROR
    return AVAILABLE if available else TAKEN


async def refresh_instantusername_services(
    session: aiohttp.ClientSession,
    proxy: object = None,
    timeout: float = 6.0,
) -> int:
    """Learn the live service catalogue so new platforms map automatically.

    Failure is not an error: the seeded map keeps working. Returns how many
    of our platforms are currently covered.
    """

    request_timeout = aiohttp.ClientTimeout(total=max(0.5, timeout))
    try:
        async with session.get(
            INSTANTUSERNAME_SERVICES_URL,
            proxy=_resolve_proxy(proxy),
            timeout=request_timeout,
            headers=API_HEADERS,
        ) as response:
            if response.status != 200:
                return len(INSTANTUSERNAME_SERVICES)
            payload = await _read_json_body(response)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.debug("instantusername catalogue unavailable: %s",
                  _redact_sensitive_text(exc))
        return len(INSTANTUSERNAME_SERVICES)

    services = payload.get("services") if isinstance(payload, Mapping) else None
    if not isinstance(services, Sequence):
        return len(INSTANTUSERNAME_SERVICES)

    known = {name for name, _emoji in PLATFORMS}
    for entry in services:
        if not isinstance(entry, Mapping):
            continue
        raw_name = entry.get("service")
        endpoint = entry.get("endpoint")
        if not isinstance(raw_name, str) or not isinstance(endpoint, str):
            continue
        platform = _INSTANTUSERNAME_ALIASES.get(_normalize_service_name(raw_name))
        if platform not in known:
            continue
        # "/check/github/{username}" -> "github"
        parts = [part for part in endpoint.split("/") if part]
        if len(parts) >= 2 and parts[0] == "check":
            INSTANTUSERNAME_SERVICES[platform] = parts[1]

    return len(INSTANTUSERNAME_SERVICES)


async def check_instantusername(
    session: aiohttp.ClientSession,
    platform: str,
    emoji: str,
    username: str,
    proxy: object = None,
) -> Result:
    """Ask instantusername.com about one platform. Never raises."""

    service = INSTANTUSERNAME_SERVICES.get(platform)
    if not service:
        return Result(platform, emoji, ERROR, "no instantusername service")
    try:
        url = (f"{INSTANTUSERNAME_BASE_URL}/check/"
               f"{quote(service, safe='')}/{quote(username, safe='')}")
        status, payload = await _fetch_json_get(
            session, url, proxy, headers=API_HEADERS)
    except _REQUEST_ERRORS as exc:
        return Result(platform, emoji, ERROR,
                      f"instantusername: {_redact_sensitive_text(exc)}")
    outcome = interpret_instantusername(status, payload)
    return Result(platform, emoji, outcome,
                  f"instantusername HTTP {status}")


# How long a platform's own endpoint gets to answer before a second-opinion
# request to instantusername.com starts *in parallel*. A healthy endpoint
# answers well inside this window and costs exactly one request; a hanging or
# walled one no longer makes the lookup wait its full timeout before the
# fallback is even asked. The first definitive answer wins.
FALLBACK_HEDGE_DELAY = 1.0
# When the hedged fallback answers definitively FIRST, the platform's own
# endpoint gets this brief grace window to overrule it - the first-party
# verdict is ground truth, the aggregator is a second opinion. A hanging
# primary still costs only this grace, not its full timeout.
FALLBACK_PRIMARY_GRACE = 0.25


async def _task_result(task: asyncio.Task, platform: str, emoji: str) -> Result:
    """Await a finished checker task, normalizing exceptions into a Result."""

    try:
        result = await task
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        return _request_error(platform, emoji, exc)
    if not isinstance(result, Result):
        return Result(platform, emoji, ERROR, "checker returned an invalid result")
    return result


def _consume_late_task(task: asyncio.Task) -> None:
    """Consume a cancelled loser of a race so it never logs a warning."""

    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001
        log.debug("Late checker task exited after the race: %s",
                  _redact_sensitive_text(exc))


async def _with_fallback(
    primary,
    session: aiohttp.ClientSession,
    platform: str,
    emoji: str,
    username: str,
    proxy: object = None,
) -> Result:
    """Run a platform's own check, with instantusername.com as second opinion.

    Fast path: the primary answers inside FALLBACK_HEDGE_DELAY and is
    definitive - the fallback is never contacted. If the primary is
    inconclusive (BLOCKED/ERROR) the fallback runs, and only replaces the
    result when it is itself definitive. If the primary is simply slow, the
    fallback is hedged in parallel and the first definitive answer wins, so a
    hanging endpoint can no longer add its whole timeout on top of the
    fallback's latency.
    """

    if platform not in INSTANTUSERNAME_SERVICES:
        return await primary

    primary_task = asyncio.ensure_future(primary)
    fallback_task: asyncio.Task | None = None
    try:
        # Give the platform's own endpoint its head start.
        await asyncio.wait({primary_task}, timeout=FALLBACK_HEDGE_DELAY)

        if primary_task.done():
            primary_result = await _task_result(primary_task, platform, emoji)
            if primary_result.status not in (BLOCKED, ERROR):
                return primary_result
            fallback_result = await check_instantusername(
                session, platform, emoji, username, proxy)
            if fallback_result.status in (AVAILABLE, TAKEN):
                log.info("%s: primary was %s, instantusername says %s",
                         platform, primary_result.status, fallback_result.status)
                return fallback_result
            return primary_result

        # Primary is still running after the hedge window: it is likely on
        # its way to a timeout. Race a second opinion instead of waiting.
        fallback_task = asyncio.ensure_future(check_instantusername(
            session, platform, emoji, username, proxy))
        primary_result: Result | None = None
        fallback_result: Result | None = None
        pending = {primary_task, fallback_task}
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is fallback_task:
                    fallback_result = await _task_result(task, platform, emoji)
                    if fallback_result.status in (AVAILABLE, TAKEN):
                        if primary_task.done():
                            # The primary already finished: its verdict rules.
                            primary_result = await _task_result(
                                primary_task, platform, emoji)
                            if primary_result.status not in (BLOCKED, ERROR):
                                return primary_result
                            return fallback_result
                        # Fallback answered first: give the platform's own
                        # endpoint a brief grace window to overrule it.
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(primary_task),
                                timeout=FALLBACK_PRIMARY_GRACE)
                        except asyncio.TimeoutError:
                            log.info("%s: hedged fallback answered first (%s)",
                                     platform, fallback_result.status)
                            return fallback_result
                        primary_result = await _task_result(
                            primary_task, platform, emoji)
                        if primary_result.status not in (BLOCKED, ERROR):
                            return primary_result
                        return fallback_result
                else:
                    primary_result = await _task_result(task, platform, emoji)
                    if primary_result.status not in (BLOCKED, ERROR):
                        return primary_result

        # Both finished and neither was definitive: keep the platform's own
        # verdict when there is one, then the fallback's, then a safe ERROR.
        if fallback_result is not None and fallback_result.status in (
                AVAILABLE, TAKEN):
            return fallback_result
        if primary_result is not None:
            return primary_result
        if fallback_result is not None:
            return fallback_result
        return Result(platform, emoji, ERROR, "no answer received")
    finally:
        for task in (primary_task, fallback_task):
            if task is not None and not task.done():
                task.cancel()
                task.add_done_callback(_consume_late_task)


# ---------------------------------------------------------------------------
# Parallel fan-out and deadline support
# ---------------------------------------------------------------------------

def timeout_results(
    detail: str = "check deadline reached",
    include_extra: bool = True,
    disabled: Collection[str] = frozenset(),
) -> list[Result]:
    """Return one honest unknown/error result per platform that would run.

    ``include_extra`` mirrors ``run_all_checks(enable_extra_platforms=...)`` and
    ``disabled`` mirrors ``disabled_platforms``, so a timed-out lookup reports
    exactly the platforms that were configured, instead of inventing errors for
    checks that are switched off.
    """

    return [
        Result(platform, emoji, ERROR, detail)
        for platform, emoji in active_platforms(include_extra, disabled)
    ]


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
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected checker exception: %s", exc)
        return _request_error(fallback.platform, fallback.emoji, exc)


def build_check_workers(
    session: aiohttp.ClientSession,
    username: str,
    proxy: object = None,
    discord_mode: str = "off",
    discord_probe_url: str | None = None,
    discord_probe_headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    discord_account_api_url: str | None = None,
    discord_account_api_headers: Mapping[str, str] | None = None,
    dnsrobot_browser=None,
    dnsrobot_semaphore: asyncio.Semaphore | None = None,
    enable_extra_platforms: bool = True,
    instantusername_fallback: bool = True,
    disabled_platforms: Collection[str] = frozenset(),
) -> list[Awaitable[Result]]:
    """Build one bounded coroutine per configured platform, in PLATFORMS order.

    Nothing runs until the caller awaits or schedules them, which lets a caller
    either gather them (ordered results) or consume them as they complete.

    Each worker is independent and holds no shared mutable state, so any number
    of lookups (different users, different usernames) can run at the same time.

    Platforms in ``disabled_platforms`` are not born at all: a disabled
    platform costs zero sockets, zero pool slots, and zero columns.
    """

    def bounded(coro, deadline_result: Result) -> Awaitable[Result]:
        """Attach the instantusername second opinion, then the deadline."""

        # Skip the hedge when the primary already *is* the instantusername
        # check (Discord in instantusername/combined mode): a failed primary
        # would otherwise re-hit the identical same-service URL as its own
        # "second opinion", which is a wasted duplicate, not a hedge.
        already_instantusername = (
            deadline_result.platform == "Discord"
            and discord_mode in ("instantusername", "combined"))
        if (instantusername_fallback
                and not already_instantusername
                and deadline_result.platform in INSTANTUSERNAME_SERVICES):
            coro = _with_fallback(
                coro, session, deadline_result.platform,
                deadline_result.emoji, username, proxy)
        return _run_bounded(coro, deadline_result, timeout)

    entries: list[tuple[str, str, Awaitable[Result]]] = [
        ("Minecraft", MINECRAFT_EMOJI, check_minecraft(session, username, proxy)),
        ("guns.lol", GUNSLOL_EMOJI, check_gunslol(session, username, proxy)),
        ("Discord", DISCORD_EMOJI, check_discord(
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
        )),
    ]
    if enable_extra_platforms:
        entries.extend([
            ("GitHub", GITHUB_EMOJI, check_github(session, username, proxy)),
            ("Steam", STEAM_EMOJI, check_steam(session, username, proxy)),
            ("Reddit", REDDIT_EMOJI, check_reddit(session, username, proxy)),
            ("Instagram", INSTAGRAM_EMOJI,
             check_instagram(session, username, proxy)),
            ("Twitter/X", TWITTER_EMOJI, check_twitter(session, username, proxy)),
        ])

    workers: list[Awaitable[Result]] = []
    for platform, emoji, coro in entries:
        if platform in disabled_platforms:
            # Un-awaited coroutine would log a warning; close it cleanly.
            if inspect.iscoroutine(coro):
                coro.close()
            continue
        workers.append(bounded(
            coro, Result(platform, emoji, ERROR, "check deadline reached")))

    return workers


async def run_all_checks(
    session: aiohttp.ClientSession,
    username: str,
    proxy: object = None,
    discord_mode: str = "off",
    discord_probe_url: str | None = None,
    discord_probe_headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    discord_account_api_url: str | None = None,
    discord_account_api_headers: Mapping[str, str] | None = None,
    dnsrobot_browser=None,
    dnsrobot_semaphore: asyncio.Semaphore | None = None,
    enable_extra_platforms: bool = True,
    instantusername_fallback: bool = True,
    disabled_platforms: Collection[str] = frozenset(),
) -> list[Result]:
    """Fan out every platform check in parallel and return ordered results.

    ``timeout`` is a *shared wall-clock budget* for the fan-out. Every worker
    begins at the same time, so the total duration is at most the one timeout,
    not the sum of platform timeouts. Timed-out workers return ERROR results.

    Results come back in ``PLATFORMS`` order minus ``disabled_platforms``.
    Use ``stream_all_checks`` when you would rather act on each platform the
    moment it answers.
    """

    workers = build_check_workers(
        session, username, proxy,
        discord_mode=discord_mode,
        discord_probe_url=discord_probe_url,
        discord_probe_headers=discord_probe_headers,
        timeout=timeout,
        discord_account_api_url=discord_account_api_url,
        discord_account_api_headers=discord_account_api_headers,
        dnsrobot_browser=dnsrobot_browser,
        dnsrobot_semaphore=dnsrobot_semaphore,
        enable_extra_platforms=enable_extra_platforms,
        instantusername_fallback=instantusername_fallback,
        disabled_platforms=disabled_platforms,
    )
    return list(await asyncio.gather(*workers))


async def stream_all_checks(
    session: aiohttp.ClientSession,
    username: str,
    proxy: object = None,
    discord_mode: str = "off",
    discord_probe_url: str | None = None,
    discord_probe_headers: Mapping[str, str] | None = None,
    timeout: float | None = None,
    discord_account_api_url: str | None = None,
    discord_account_api_headers: Mapping[str, str] | None = None,
    dnsrobot_browser=None,
    dnsrobot_semaphore: asyncio.Semaphore | None = None,
    enable_extra_platforms: bool = True,
    instantusername_fallback: bool = True,
    disabled_platforms: Collection[str] = frozenset(),
) -> AsyncIterator[Result]:
    """Yield each platform result the moment that platform answers.

    Same parallel fan-out and same shared deadline as ``run_all_checks``, but
    the caller does not have to wait for the slowest platform before acting on
    the fastest one. Results arrive in completion order, not PLATFORMS order.
    """

    workers = build_check_workers(
        session, username, proxy,
        discord_mode=discord_mode,
        discord_probe_url=discord_probe_url,
        discord_probe_headers=discord_probe_headers,
        timeout=timeout,
        discord_account_api_url=discord_account_api_url,
        discord_account_api_headers=discord_account_api_headers,
        dnsrobot_browser=dnsrobot_browser,
        dnsrobot_semaphore=dnsrobot_semaphore,
        enable_extra_platforms=enable_extra_platforms,
        instantusername_fallback=instantusername_fallback,
        disabled_platforms=disabled_platforms,
    )
    tasks = [asyncio.ensure_future(worker) for worker in workers]
    try:
        for completed in asyncio.as_completed(tasks):
            try:
                yield await completed
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("Checker task failed: %s", _redact_sensitive_text(exc))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def prewarm_connections(
    session: aiohttp.ClientSession,
    proxy: object = None,
    timeout: float = 4.0,
    urls: Sequence[str] | None = None,
) -> int:
    """Open a pooled TLS connection to every platform host up front.

    The first request to a host pays DNS + TCP + TLS (often 100-300 ms). Doing
    that once at startup, in the background, means the first real lookup reuses
    a warm keep-alive connection instead. Failures are ignored on purpose: this
    is an optimisation, never a startup requirement.

    ``urls`` overrides the default host list (e.g. to add the fallback
    provider's host when it is enabled). Returns how many hosts were warmed.
    """

    targets = PREWARM_URLS if urls is None else tuple(urls)
    request_timeout = aiohttp.ClientTimeout(total=max(0.5, timeout))

    async def warm(url: str) -> bool:
        try:
            async with session.head(
                url,
                proxy=_resolve_proxy(proxy),
                timeout=request_timeout,
                allow_redirects=False,
            ) as response:
                response.release()
                return True
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            return False

    warmed = await asyncio.gather(*(warm(url) for url in targets))
    return sum(1 for ok in warmed if ok)


# ---------------------------------------------------------------------------
# CLI self-test: python checkers.py <username>
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
    parser.add_argument("--no-extra", action="store_true",
                        help="disable extra platforms (GitHub, Steam, etc.)")
    ns = parser.parse_args(argv)

    deadline = max(0.1, ns.timeout)
    # A socket that cannot even connect inside the connect cap should rotate
    # to the hedged/fallback path rather than eat the whole check budget.
    request_timeout = aiohttp.ClientTimeout(
        total=deadline, sock_connect=min(2.0, deadline))

    # Optimized TCP connector for connection pooling
    connector = make_fast_connector(limit=20, limit_per_host=10)

    browser_runtime = None
    dnsrobot_browser = None
    dnsrobot_semaphore = None
    if ns.mode == "dnsrobot":
        try:
            browser_runtime, dnsrobot_browser = await start_dnsrobot_browser(ns.proxy)
            dnsrobot_semaphore = asyncio.Semaphore(2)
        except Exception as exc:  # noqa: BLE001
            print(
                "DNS Robot browser unavailable; its result will be ERROR: "
                f"{_redact_sensitive_text(exc)}",
                file=sys.stderr,
            )

    try:
        async with aiohttp.ClientSession(
                headers=BROWSER_HEADERS,
                timeout=request_timeout,
                connector=connector) as session:
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
                enable_extra_platforms=not ns.no_extra,
            )
    finally:
        if dnsrobot_browser is not None:
            try:
                await dnsrobot_browser.close()
            except Exception as exc:  # noqa: BLE001
                print(
                    f"DNS Robot browser cleanup failed: {_redact_sensitive_text(exc)}",
                    file=sys.stderr,
                )
        if browser_runtime is not None:
            try:
                await browser_runtime.stop()
            except Exception as exc:  # noqa: BLE001
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
        print(f"  {result.emoji} {result.platform:<12} "
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
