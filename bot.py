"""
Multi-Sniper - a Discord username availability checker.

A member posts one bare username in the watched channel. The bot validates it,
runs all platform checks in parallel (Minecraft, guns.lol, Discord, GitHub,
Steam, Reddit, Instagram, Twitter/X), receives the normalized checker results,
and reacts to the *same* Discord message:

    🕹️  Minecraft free
    🔫  guns.lol free
    🐈‍⬛ Discord free (DNS Robot, account, or authorized probe; disabled by default)
    💻  GitHub free
    🎮  Steam free
    👀  Reddit free
    📸  Instagram free
    🐦  Twitter/X free
    ❌  nothing checked was free
    ⚠️  no free result can be confirmed (all or a required check was inconclusive)
    ⏳  member exceeded the per-user cooldown

The valid-message path has a response budget (4.5 seconds by default). Checks
share the budget and reactions run concurrently, leaving time for the Discord
REST calls instead of letting sequential retries push the reaction past five
seconds.

Configuration lives in .env (see .env.example):
    DISCORD_TOKEN             bot token from the Discord Developer Portal
    TARGET_CHANNEL_ID         channel to watch (blank = every channel)
    LOG_CHANNEL_ID            optional channel to log available hits
    PROXY_URL                 single HTTP(S) proxy for outbound checks
    PROXY_URLS                comma-separated proxy pool for rotation + failover
    DISCORD_CHECK_MODE        off (default) | dnsrobot | account | account_api | probe
    DISCORD_ACCOUNT_API_URL   optional account eligibility endpoint override
    DISCORD_ACCOUNT_API_TOKEN optional credential for an authorized account API
    DISCORD_PROBE_URL         authorized external checker URL template (optional)
    DISCORD_PROBE_TOKEN       optional token sent only to that checker endpoint
    ENABLE_EXTRA_PLATFORMS    true (default) | false — check GitHub/Steam/Reddit/...
    PROXY_ALLOW_DIRECT_FALLBACK  false (default) | true — go direct if all proxies die

Proxy pool features:
    - Round-robin rotation resolved per request, so the platform checks in one
      lookup spread across the pool instead of hammering a single IP
    - Live health reporting from real traffic: a proxy that fails a check is
      benched at once and the retry lands on the next proxy
    - Concurrent health checks every 30s, with a 60s recovery cooldown
    - When every proxy is down it keeps using proxies by default rather than
      leaking the host IP; set PROXY_ALLOW_DIRECT_FALLBACK=true to go direct

Run locally: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import time
from collections import defaultdict, deque

import aiohttp
import discord
from dotenv import load_dotenv

import checkers
from proxies import (
    DEFAULT_PROXY_CACHE, DEFAULT_PROXY_FILE, ProxyPool, ProxyProvider,
    drop_socks_ports, fetch_proxy_list, load_proxy_file, parse_proxy_list,
    probe_proxies, read_proxy_cache, sample_proxies, short_proxy_url,
    usable_concurrency, write_proxy_cache,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()


def _opt_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else None


def _opt_float(name: str, default: float) -> float:
    """Read a finite float, falling back safely for malformed env values."""

    try:
        value = float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read a float config value without allowing it to defeat latency bounds."""

    return min(max(_opt_float(name, default), minimum), maximum)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """Read a bounded integer without crashing on ``nan``/``inf`` input."""

    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _has_http_control_chars(value: str) -> bool:
    """Reject characters that can corrupt an HTTP header or log line."""

    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _token_header(
    token: str,
    header_name: str,
    scheme: str,
) -> dict[str, str] | None:
    """Build one optional auth header without ever logging its token value."""

    if not token:
        return None
    value = f"{scheme} {token}".strip() if scheme else token
    return {header_name: value}


def _discord_probe_headers() -> dict[str, str] | None:
    """Build the optional probe auth header without exposing its value."""

    return _token_header(
        DISCORD_PROBE_TOKEN,
        DISCORD_PROBE_TOKEN_HEADER,
        DISCORD_PROBE_TOKEN_SCHEME,
    )


def _discord_account_api_headers() -> dict[str, str] | None:
    """Build account-API auth headers; never reuse the bot token implicitly."""

    return _token_header(
        DISCORD_ACCOUNT_API_TOKEN,
        DISCORD_ACCOUNT_API_TOKEN_HEADER,
        DISCORD_ACCOUNT_API_TOKEN_SCHEME,
    )


TARGET_CHANNEL_ID = _opt_int("TARGET_CHANNEL_ID")
LOG_CHANNEL_ID = _opt_int("LOG_CHANNEL_ID")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
DISCORD_CHECK_MODE = os.getenv("DISCORD_CHECK_MODE", "off").strip().lower()
DISCORD_ACCOUNT_API_URL = (
    os.getenv("DISCORD_ACCOUNT_API_URL", "").strip()
    or checkers.DEFAULT_DISCORD_ACCOUNT_API_URL
)
DISCORD_ACCOUNT_API_TOKEN = os.getenv("DISCORD_ACCOUNT_API_TOKEN", "").strip()
DISCORD_ACCOUNT_API_TOKEN_HEADER = os.getenv(
    "DISCORD_ACCOUNT_API_TOKEN_HEADER", "Authorization").strip() or "Authorization"
DISCORD_ACCOUNT_API_TOKEN_SCHEME = os.getenv(
    "DISCORD_ACCOUNT_API_TOKEN_SCHEME", "Bearer").strip()
DISCORD_PROBE_URL = os.getenv("DISCORD_PROBE_URL", "").strip() or None
DISCORD_PROBE_TOKEN = os.getenv("DISCORD_PROBE_TOKEN", "").strip()
DISCORD_PROBE_TOKEN_HEADER = os.getenv(
    "DISCORD_PROBE_TOKEN_HEADER", "Authorization").strip() or "Authorization"
DISCORD_PROBE_TOKEN_SCHEME = os.getenv("DISCORD_PROBE_TOKEN_SCHEME", "Bearer").strip()
DISCORD_ACCOUNT_API_HEADERS = _discord_account_api_headers()
DISCORD_PROBE_HEADERS = _discord_probe_headers()

# Proxy pool: comma-separated list of proxies for rotation + failover
PROXY_URLS_RAW = os.getenv("PROXY_URLS", "").strip()
# Default proxy list file. Drop a vendor list into proxies.txt (one per line,
# any common format) and it is used automatically - no environment variable
# needed. PROXY_FILE points somewhere else; PROXY_FILE= (blank) disables it.
PROXY_FILE = os.getenv("PROXY_FILE", DEFAULT_PROXY_FILE).strip()

# Remote proxy list, downloaded at startup. Public lists are far too large to
# keep in the repository (this one is ~169,000 entries) and go stale within
# hours, so the bot fetches it, caches it, samples it down and verifies it.
# Set PROXY_LIST_URL= (blank) to switch remote loading off entirely.
DEFAULT_PROXY_LIST_URL = (
    "https://drive.google.com/file/d/"
    "1-Go3mD7uZ-2j5-YoytjsHJAKbPA7RDsN/view?usp=drivesdk"
)
PROXY_LIST_URL = os.getenv("PROXY_LIST_URL", DEFAULT_PROXY_LIST_URL).strip()
PROXY_CACHE_FILE = os.getenv("PROXY_CACHE_FILE", DEFAULT_PROXY_CACHE).strip()
# How long a cached copy is reused before the list is downloaded again.
PROXY_LIST_TTL = _bounded_float(
    "PROXY_LIST_TTL", 6 * 3600, minimum=0.0, maximum=30 * 86400)
PROXY_LIST_TIMEOUT = _bounded_float(
    "PROXY_LIST_TIMEOUT", 20.0, minimum=1.0, maximum=120.0)
# Upper bound on how many proxies enter the rotation. Every entry costs a
# little memory and a slice of each health sweep, so this is a ceiling, not a
# goal - but a big pool is genuinely useful against per-IP rate limits, and a
# scraped list is cheap, so the ceiling sits well above the floor.
PROXY_MAX_POOL = _bounded_int(
    "PROXY_MAX_POOL", 2_000, minimum=1, maximum=50_000)
# How many *working* proxies the bot tries to end up with. Verification keeps
# pulling fresh candidates from the list until it has this many, because a
# public list is mostly dead and one sample of it will not be enough. At the
# ~1% hit rate a free list gives, reaching 1,000 means probing most of a
# 169,000-entry list - which is why the search is wide, chunked and runs in
# the background while the bot is already answering.
PROXY_MIN_POOL = _bounded_int(
    "PROXY_MIN_POOL", 1_000, minimum=0, maximum=50_000)
# Wall-clock ceiling for that search. It runs in the background, so the bot is
# already answering while it works; this only stops it hunting forever.
PROXY_VERIFY_MAX_SECONDS = _bounded_float(
    "PROXY_VERIFY_MAX_SECONDS", 900.0, minimum=1.0, maximum=21_600.0)
if PROXY_MAX_POOL < PROXY_MIN_POOL:
    # A maximum below the minimum is a typo, not an instruction to keep fewer.
    PROXY_MAX_POOL = PROXY_MIN_POOL
# Probe every proxy once at startup and keep only the ones that answer.
# Essential for public lists, where the large majority are already dead.
PROXY_VERIFY_ON_START = os.getenv(
    "PROXY_VERIFY_ON_START", "true").strip().lower() in (
        "true", "1", "yes", "on", "")
# Verification runs on its own connector, so these do not compete with live
# lookups. Free proxies that work answer in a couple of seconds; the rest are
# timeouts, so a wide-and-short probe finds working ones far faster than a
# narrow-and-patient one.
PROXY_VERIFY_CONCURRENCY = _bounded_int(
    "PROXY_VERIFY_CONCURRENCY", 1_000, minimum=1, maximum=10_000)
PROXY_VERIFY_TIMEOUT = _bounded_float(
    "PROXY_VERIFY_TIMEOUT", 5.0, minimum=0.5, maximum=60.0)
# What a proxy must be able to fetch to count as working. It is deliberately
# an HTTPS URL: every real check the bot makes is HTTPS, so a proxy that
# cannot CONNECT is no use even if it serves plain HTTP happily.
PROXY_PROBE_URL = os.getenv(
    "PROXY_PROBE_URL", "https://api.mojang.com").strip() or "https://api.mojang.com"
# The periodic sweep of the already-verified live pool. It has to get through
# a pool that can now hold thousands, so it is wider than it was - but still
# far gentler than startup verification, because these proxies already work.
PROXY_HEALTH_CONCURRENCY = _bounded_int(
    "PROXY_HEALTH_CONCURRENCY", 200, minimum=1, maximum=5_000)
# Skip entries on well-known SOCKS ports: aiohttp cannot speak SOCKS, so on a
# scraped list they are just wasted probes.
PROXY_SKIP_SOCKS_PORTS = os.getenv(
    "PROXY_SKIP_SOCKS_PORTS", "true").strip().lower() in (
        "true", "1", "yes", "on", "")


# The proxy file is read once per path: startup validation, the pool, and the
# banner all ask for it, and one log line is enough.
_PROXY_FILE_CACHE: dict[str, list[str]] = {}


def _proxy_file_entries() -> list[str]:
    if PROXY_FILE not in _PROXY_FILE_CACHE:
        _PROXY_FILE_CACHE[PROXY_FILE] = load_proxy_file(PROXY_FILE)
    return list(_PROXY_FILE_CACHE[PROXY_FILE])


def _proxy_source_summary() -> str:
    """Name the places the proxy list was actually loaded from."""

    sources = []
    if PROXY_URL:
        sources.append("PROXY_URL")
    if PROXY_URLS_RAW:
        sources.append("PROXY_URLS")
    if PROXY_FILE and _proxy_file_entries():
        sources.append(PROXY_FILE)
    if PROXY_LIST_URL:
        sources.append("remote list")
    return " + ".join(sources) if sources else "none"


def configured_proxies() -> list[str]:
    """Every proxy this run should rotate through, in priority order.

    PROXY_URL first (explicit single proxy), then PROXY_URLS, then the
    proxy file. Duplicates are dropped, so listing a proxy twice is safe.
    """

    ordered: list[str] = []
    seen: set[str] = set()
    for proxy in (parse_proxy_list(PROXY_URL or "")
                  + parse_proxy_list(PROXY_URLS_RAW)
                  + _proxy_file_entries()):
        if proxy not in seen:
            seen.add(proxy)
            ordered.append(proxy)
    return ordered
ENABLE_EXTRA_PLATFORMS = os.getenv("ENABLE_EXTRA_PLATFORMS", "true").strip().lower() in (
    "true", "1", "yes", "on", "")

# The event handler starts after Discord delivers MESSAGE_CREATE. Reserving a
# little room beneath five seconds means the checker fan-out cannot consume all
# the time that the reaction REST call needs. Values from the environment are
# intentionally clamped: a typo such as CHECK_TIMEOUT=30 must not turn one
# chat message into a 30-second wait.
RESPONSE_BUDGET_SECONDS = _bounded_float(
    "RESPONSE_BUDGET_SECONDS", 4.5, minimum=0.5, maximum=4.8)
REACTION_TIMEOUT = _bounded_float(
    "REACTION_TIMEOUT", 0.75, minimum=0.05,
    maximum=max(0.05, RESPONSE_BUDGET_SECONDS - 0.05))
CHECK_TIMEOUT = _bounded_float(
    "CHECK_TIMEOUT", 3.0, minimum=0.05, maximum=RESPONSE_BUDGET_SECONDS)
# Per-socket connect cap inside CHECK_TIMEOUT. Dead proxies and black-holed
# hosts should fail over quickly instead of consuming the whole check budget;
# healthy TLS connects land in tens of milliseconds, so this never trims a
# working connection.
CONNECT_DEADLINE = _bounded_float(
    "CONNECT_DEADLINE", 2.0, minimum=0.1, maximum=CHECK_TIMEOUT)
# Anti-abuse throttle. Defaults are deliberately sub-second so a member can
# fire checks back-to-back and get availability answers instantly; the window
# only exists to absorb genuine flood/spam bursts.
USER_MAX_CHECKS = _bounded_int("USER_MAX_CHECKS", 5, minimum=1, maximum=10_000)
USER_WINDOW_SECONDS = max(_opt_float("USER_WINDOW_SECONDS", 0.5), 0.01)
# Base cache lifetime. The two smart TTLs below default to multiples of it,
# so setting RESULT_CACHE_TTL alone still tunes caching as a whole.
RESULT_CACHE_TTL = max(_opt_float("RESULT_CACHE_TTL", 300), 0.0)

# Smart cache: taken names stay cached longer (they rarely free up), while
# available names expire faster (they might get sniped by someone else).
CACHE_TTL_TAKEN = max(_opt_float("CACHE_TTL_TAKEN", RESULT_CACHE_TTL * 2), 0.0)
CACHE_TTL_AVAILABLE = max(
    _opt_float("CACHE_TTL_AVAILABLE", RESULT_CACHE_TTL * 0.4), 0.0)
# Hard ceiling on cache entries so a busy server cannot grow it without bound.
CACHE_MAX_ENTRIES = _bounded_int("CACHE_MAX_ENTRIES", 5000, 64, 1_000_000)

# Outbound connection pool. Every lookup opens up to one connection per
# platform, so a channel where a dozen members paste usernames at the same
# moment needs a pool far larger than a single lookup does. Too small a pool
# does not fail loudly - requests silently queue until they blow their
# deadline and report "Unknown" - so these default generously.
HTTP_POOL_LIMIT = _bounded_int("HTTP_POOL_LIMIT", 200, 8, 5_000)
HTTP_POOL_LIMIT_PER_HOST = _bounded_int(
    "HTTP_POOL_LIMIT_PER_HOST", 40, 2, 1_000)

# Coalesce duplicate lookups: when several members paste the SAME username
# while a check for it is already running, they all wait on that one check
# instead of starting their own. Every member still gets their own reply.
COALESCE_DUPLICATES = os.getenv(
    "COALESCE_DUPLICATES", "true").strip().lower() in (
        "true", "1", "yes", "on", "")

# Second-opinion provider. When a platform's own endpoint stops answering
# (Cloudflare wall, rate limit, network error) the check falls back to
# instantusername.com instead of reporting "Unknown".
INSTANTUSERNAME_FALLBACK = os.getenv(
    "INSTANTUSERNAME_FALLBACK", "true").strip().lower() in (
        "true", "1", "yes", "on", "")

# Proxy behaviour when every proxy is benched. Direct fallback is OFF by
# default: falling back would expose the host's real IP to the platforms,
# which is usually the exact thing the proxies were configured to prevent.
PROXY_ALLOW_DIRECT_FALLBACK = os.getenv(
    "PROXY_ALLOW_DIRECT_FALLBACK", "false").strip().lower() in (
        "true", "1", "yes", "on")

# Stream reactions: add each platform's emoji the moment that platform
# answers, instead of waiting for the slowest one. This is the single biggest
# win in time-to-first-reaction. Set to false for the old batched behaviour
# (all emojis appear at once, in platform order).
STREAM_REACTIONS = os.getenv("STREAM_REACTIONS", "true").strip().lower() in (
    "true", "1", "yes", "on", "")

# Pre-open a pooled TLS connection to every platform host at startup so the
# first real lookup does not pay DNS + TCP + TLS setup.
PREWARM_CONNECTIONS = os.getenv("PREWARM_CONNECTIONS", "true").strip().lower() in (
    "true", "1", "yes", "on", "")

# How the bot answers a lookup:
#   reply  = post a readable "Platform: Status" list as a reply (default)
#   react  = add one emoji per free platform to the original message
#   both   = do both
RESPONSE_MODE_RAW = os.getenv("RESPONSE_MODE", "reply").strip().lower()
RESPONSE_MODE = RESPONSE_MODE_RAW if RESPONSE_MODE_RAW in (
    "reply", "react", "both") else "reply"
REPLY_ENABLED = RESPONSE_MODE in ("reply", "both")
REACT_ENABLED = RESPONSE_MODE in ("react", "both")

# Minimum gap between live edits of the reply while results stream in. The
# first paint is always immediate; this only throttles the follow-up edits so
# a lookup cannot burn Discord's per-channel edit rate limit.
REPLY_EDIT_INTERVAL = _bounded_float(
    "REPLY_EDIT_INTERVAL", 0.7, minimum=0.1, maximum=5.0)

# Show platforms the config disabled (Discord when DISCORD_CHECK_MODE=off).
REPLY_INCLUDE_SKIPPED = os.getenv(
    "REPLY_INCLUDE_SKIPPED", "false").strip().lower() in ("true", "1", "yes", "on")

# Ping the requester when replying. Off by default: it is noisy in a busy
# sniping channel and the reply is already attached to their message.
REPLY_MENTION_AUTHOR = os.getenv(
    "REPLY_MENTION_AUTHOR", "false").strip().lower() in ("true", "1", "yes", "on")

# Wording used in the reply for each normalized status.
STATUS_LABELS = {
    checkers.AVAILABLE: "Available",
    checkers.TAKEN: "Unavailable",
    checkers.INVALID: "Invalid",
    checkers.BLOCKED: "Unknown",
    checkers.ERROR: "Unknown",
    checkers.SKIPPED: "Not checked",
}
PENDING_LABEL = "Checking..."

# Optional tiny HTTP server. Free hosting tiers (Render, Koyeb, Replit) only
# keep a service alive if it binds a port and answers health checks, so this
# turns the worker into something they will host for free. Enabled whenever
# PORT or KEEPALIVE_PORT is present in the environment.
KEEPALIVE_PORT = _opt_int("KEEPALIVE_PORT") or _opt_int("PORT")

# Feedback emojis
EMOJI_NONE_AVAILABLE = "❌"
EMOJI_ALL_FAILED = "⚠️"
EMOJI_COOLDOWN = "\u23f3"  # ⏳

def format_results(
    results: list[checkers.Result],
    pending: bool = False,
    include_extra: bool | None = None,
) -> str:
    """Render results as a readable "Platform: Status" list.

    Lines always follow the fixed platform order, even though results stream
    back in completion order, so the message never reshuffles under the reader
    while it is being updated. Platforms that have not answered yet show as
    pending rather than silently missing.
    """

    if include_extra is None:
        include_extra = ENABLE_EXTRA_PLATFORMS
    expected = checkers.PLATFORMS if include_extra else checkers.CORE_PLATFORMS
    by_platform = {result.platform: result for result in results}

    lines: list[str] = []
    for platform, _emoji in expected:
        result = by_platform.get(platform)
        if result is None:
            if not pending:
                continue
            lines.append(f"{platform}: {PENDING_LABEL}")
            continue
        if result.status == checkers.SKIPPED and not REPLY_INCLUDE_SKIPPED:
            continue
        label = STATUS_LABELS.get(result.status, "Unknown")
        lines.append(f"{platform}: {label}")

    if not lines:
        return "No platforms were checked."
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("multi-sniper")

# ---------------------------------------------------------------------------
# Bot client
# ---------------------------------------------------------------------------


class SniperBot(discord.Client):
    """Discord client with one shared aiohttp session for platform checks."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # also enable it in the Developer Portal
        super().__init__(intents=intents)
        self.http_sniper: aiohttp.ClientSession | None = None
        # The literal DNS Robot mode owns a long-lived browser process so each
        # lookup only creates a short-lived isolated context/page.
        self._playwright = None
        self.dnsrobot_browser = None
        self.dnsrobot_semaphore: asyncio.Semaphore | None = None
        # Per-user token bucket: {user_id: deque[timestamps]}
        self._buckets: dict[int, deque[float]] = defaultdict(deque)
        # Recent results cache: {username_lower: (timestamp, [Result, ...])}
        self._cache: dict[str, tuple[float, list[checkers.Result]]] = {}
        # Lookups running right now: {username_lower: Future[[Result, ...]]}.
        # Two members pasting the same name at the same time share one check.
        self._inflight: dict[str, asyncio.Future] = {}
        # Callable handed to the checkers; also collects live health reports.
        self.proxy_provider: ProxyProvider = ProxyProvider(static_url=PROXY_URL)
        # Proxy pool for rotation and failover (see the property below).
        self.proxy_pool = None
        # Background tasks (health checks) kept referenced so they are not
        # garbage collected mid-flight.
        self._health_task: asyncio.Task | None = None
        self._initial_health_task: asyncio.Task | None = None
        self._prewarm_task: asyncio.Task | None = None
        self._services_task: asyncio.Task | None = None
        self._keepalive_runner = None
        # Remote proxies not yet in the pool, drawn on when the verified count
        # falls short of PROXY_MIN_POOL.
        self._proxy_reserve: list[str] = []
        # Proxies from PROXY_URL / PROXY_URLS / proxies.txt: kept whatever a
        # single probe says, because the operator chose them deliberately.
        self._curated_proxies: set[str] = set()
        self._started_at = time.monotonic()
        self._checks_served = 0
        # on_ready fires again after every gateway resume; print once.
        self._banner_shown = False

    async def setup_hook(self) -> None:
        """Create one pooled outbound session before gateway events arrive."""

        # Optimized TCP connector: pooled keep-alive connections, cached DNS,
        # and Happy Eyeballs where the installed aiohttp supports it.
        connector = checkers.make_fast_connector(
            HTTP_POOL_LIMIT, HTTP_POOL_LIMIT_PER_HOST)

        # Connect fast, fail fast: a socket that cannot even connect inside
        # CONNECT_DEADLINE (dead proxy, black-holed host) rotates to the next
        # proxy instead of eating the entire per-check budget.
        timeout = aiohttp.ClientTimeout(
            total=CHECK_TIMEOUT,
            sock_connect=min(CONNECT_DEADLINE, CHECK_TIMEOUT),
        )
        self.http_sniper = aiohttp.ClientSession(
            headers=checkers.BROWSER_HEADERS,
            timeout=timeout,
            connector=connector,
        )

        # Initialize the proxy pool instantly from the locally configured
        # sources (PROXY_URL / PROXY_URLS / proxies.txt). The optional remote
        # list is fetched, sampled and verified in the background so a slow
        # download can never delay the gateway login; the pool is upgraded
        # in place while the bot is already answering. Until a remote list
        # arrives, a pool built only from a remote source hands out direct
        # connections - the same window the old blocking download had, minus
        # the offline time.
        local = configured_proxies()
        self._curated_proxies = set(local)
        if local or PROXY_LIST_URL:
            self.proxy_pool = ProxyPool(
                local, allow_direct_fallback=PROXY_ALLOW_DIRECT_FALLBACK)
            # Background health checking, refreshed every 30s.
            self._health_task = asyncio.create_task(
                self.proxy_pool.periodic_health_check(
                    self.http_sniper, PROXY_PROBE_URL,
                    concurrency=PROXY_HEALTH_CONCURRENCY))
            # The first sweep runs in the background: awaiting it here would
            # delay the gateway login by a full probe timeout for no benefit,
            # since live traffic reports health on its own.
            if PROXY_LIST_URL:
                self._initial_health_task = asyncio.create_task(
                    self._build_remote_pool())
            else:
                self._initial_health_task = asyncio.create_task(
                    self._verify_proxies() if PROXY_VERIFY_ON_START
                    else self._initial_health_check())

        if KEEPALIVE_PORT:
            await self._start_keepalive_server()

        if PREWARM_CONNECTIONS:
            # Background: never delay the gateway login for an optimisation.
            self._prewarm_task = asyncio.create_task(self._prewarm())

        if INSTANTUSERNAME_FALLBACK:
            # Learn the fallback provider's live service list so platforms it
            # adds later are picked up without a code change. Background, and
            # harmless if it fails: a built-in map is already loaded.
            self._services_task = asyncio.create_task(
                checkers.refresh_instantusername_services(
                    self.http_sniper, self._next_proxy))

        if DISCORD_CHECK_MODE == "dnsrobot":
            try:
                proxy_for_browser = self._next_proxy()
                self._playwright, self.dnsrobot_browser = (
                    await checkers.start_dnsrobot_browser(proxy_for_browser))
                self.dnsrobot_semaphore = asyncio.Semaphore(2)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "DNS Robot browser unavailable; Discord results will be "
                    "ERROR until Chromium is installed: %s",
                    checkers._redact_sensitive_text(exc),
                )

    @property
    def proxy_pool(self) -> ProxyPool | None:
        return self.proxy_provider.pool

    @proxy_pool.setter
    def proxy_pool(self, pool: ProxyPool | None) -> None:
        """Keep the pool and the provider handed to the checkers in sync."""

        self.proxy_provider.pool = pool

    async def _build_remote_pool(self) -> None:
        """Fetch the remote proxy list and merge it in, off the login path.

        Anything configured locally (PROXY_URL / PROXY_URLS / proxies.txt) is
        already in the pool and is curated on purpose: never sampled away,
        never filtered, never dropped by verification. The remote list is the
        bulk source: cached, filtered, sampled down to PROXY_MAX_POOL, merged
        into the pool and then verified. All of it runs in the background so
        the gateway login - and every lookup served meanwhile - is never
        blocked on a download.
        """

        pool = self.proxy_pool
        try:
            remote, age = read_proxy_cache(PROXY_CACHE_FILE)
            if remote and age <= PROXY_LIST_TTL:
                log.info("Using %d cached proxies (%.0f min old)",
                         len(remote), age / 60)
            else:
                downloaded = await fetch_proxy_list(
                    self.http_sniper, PROXY_LIST_URL,
                    timeout=PROXY_LIST_TIMEOUT)
                if downloaded:
                    remote = downloaded
                    write_proxy_cache(remote, PROXY_CACHE_FILE)
                elif remote:
                    log.warning("Download failed; falling back to the cached "
                                "list (%.0f min old)", age / 60)

            if remote and PROXY_SKIP_SOCKS_PORTS:
                remote, dropped = drop_socks_ports(remote)
                if dropped:
                    log.info("Skipped %d entries on SOCKS-only ports", dropped)

            if remote and pool is not None:
                budget = max(0, PROXY_MAX_POOL - pool.size)
                sampled = sample_proxies(remote, budget)
                if len(sampled) < len(remote):
                    log.info("Sampled %d of %d remote proxies "
                             "(PROXY_MAX_POOL=%d)",
                             len(sampled), len(remote), PROXY_MAX_POOL)
                existing = set(pool.urls)
                fresh = [url for url in sampled if url not in existing]
                if fresh:
                    pool.add(fresh)
                # Everything not in the pool stays available as a reserve, so
                # verification can keep hunting until it has PROXY_MIN_POOL
                # survivors.
                in_pool = set(pool.urls)
                self._proxy_reserve = [url for url in remote
                                       if url not in in_pool]
                random.shuffle(self._proxy_reserve)

            if PROXY_VERIFY_ON_START:
                await self._verify_proxies()
            else:
                await self._initial_health_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Remote proxy pool build failed: %s",
                        checkers._redact_sensitive_text(exc))

    async def _verify_proxies(self) -> None:
        """Probe the pool once and drop whatever did not answer.

        A scraped public list is mostly dead on arrival. Without this the
        rotation would spend its first minutes handing out corpses, and every
        lookup would pay a timeout before failing over.
        """

        pool = self.proxy_pool
        if pool is None or not pool.size:
            return
        started = time.monotonic()
        deadline = started + PROXY_VERIFY_MAX_SECONDS
        # What this host can actually sustain, which may be less than asked
        # for: every in-flight probe holds a file descriptor.
        width = usable_concurrency(PROXY_VERIFY_CONCURRENCY)
        log.info("Verifying %d proxies (%d at a time) against %s, "
                 "aiming for %d working...",
                 pool.size, width, PROXY_PROBE_URL, PROXY_MIN_POOL)

        # Its own connector: hundreds of doomed proxy connections must not
        # queue behind - or evict - the connections live lookups are using.
        connector = aiohttp.TCPConnector(
            limit=width, force_close=True,
            enable_cleanup_closed=True)
        session = aiohttp.ClientSession(connector=connector)
        try:
            working = await probe_proxies(
                session, pool.urls, PROXY_PROBE_URL,
                timeout=PROXY_VERIFY_TIMEOUT,
                concurrency=width)
            tested = pool.size

            # A public list is mostly dead, so one sample rarely yields
            # enough. Keep pulling fresh candidates until the target is met,
            # the reserve runs out, or the time budget expires.
            while (len(working) < PROXY_MIN_POOL
                   and self._proxy_reserve
                   and time.monotonic() < deadline):
                hit_rate = max(len(working) / tested, 0.01) if tested else 0.05
                wanted = PROXY_MIN_POOL - len(working)
                batch_size = min(
                    len(self._proxy_reserve),
                    max(width, int(wanted / hit_rate) + 1),
                    10_000,
                )
                batch = self._proxy_reserve[:batch_size]
                del self._proxy_reserve[:batch_size]
                found = await probe_proxies(
                    session, batch, PROXY_PROBE_URL,
                    timeout=PROXY_VERIFY_TIMEOUT,
                    concurrency=width)
                tested += len(batch)
                working.extend(found)
                log.info("Proxy search: %d/%d working after testing %d "
                         "(%.1f%% alive, %.0fs elapsed)",
                         len(working), PROXY_MIN_POOL, tested,
                         100 * len(working) / tested,
                         time.monotonic() - started)
        finally:
            await session.close()

        elapsed = time.monotonic() - started

        # A curated proxy that missed one probe is not evidence it is dead -
        # it may simply have been busy - and the operator paid for it. Keep
        # it, but say so.
        curated_in_pool = [url for url in pool.urls
                           if url in self._curated_proxies]
        curated_failed = [url for url in curated_in_pool
                          if url not in set(working)]
        if curated_failed:
            log.warning(
                "%d configured prox%s did not answer the probe but "
                "%s kept anyway (they were set explicitly): %s",
                len(curated_failed), "y" if len(curated_failed) == 1 else "ies",
                "is" if len(curated_failed) == 1 else "are",
                ", ".join(short_proxy_url(u) for u in curated_failed[:5]))

        if not working and not curated_in_pool:
            # Keep the pool: an empty pool means direct, unproxied traffic,
            # which is exactly what proxies were configured to avoid.
            log.warning(
                "No proxy answered out of %d tested in %.1fs. Keeping the "
                "pool and retrying live - checks may be slow until one "
                "recovers. Consider a paid provider, or point "
                "PROXY_LIST_URL at a fresher list.", tested, elapsed)
            return

        # Curated first, then verified remote proxies, up to the cap.
        keep = list(curated_in_pool)
        for url in working:
            if len(keep) >= PROXY_MAX_POOL:
                break
            if url not in self._curated_proxies:
                keep.append(url)
        pool.add(keep)
        removed = pool.keep_only(keep)
        for url in working:
            pool.report_success(url)
        log.info("Proxy pool ready: %d working (tested %d, dropped %d) "
                 "in %.1fs", pool.size, tested, removed, elapsed)
        if len(working) < PROXY_MIN_POOL:
            log.warning(
                "Only %d of the requested %d proxies are working. The list "
                "is %s. Add known-good proxies to proxies.txt, or raise "
                "PROXY_VERIFY_MAX_SECONDS to search longer.",
                len(working), PROXY_MIN_POOL,
                "exhausted" if not self._proxy_reserve else "still being searched")

    async def _start_keepalive_server(self) -> None:
        """Serve a small health endpoint so free hosts keep the bot running.

        Free tiers on Render/Koyeb/Replit only keep a service alive if it binds
        the port they hand out and answers health checks. This is deliberately
        tiny: it exposes no configuration, no secrets, and no control surface.
        """

        from aiohttp import web

        async def health(_request: web.Request) -> web.Response:
            uptime = time.monotonic() - self._started_at
            return web.json_response({
                "status": "ok" if self.is_ready() else "starting",
                "bot": str(self.user) if self.user else None,
                "uptime_seconds": round(uptime, 1),
                "checks_served": self._checks_served,
                "cached_names": len(self._cache),
                "checks_in_flight": len(self._inflight),
                "proxies_alive": (
                    self.proxy_pool.alive_count if self.proxy_pool else 0),
            })

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_get("/health", health)
        try:
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", KEEPALIVE_PORT)
            await site.start()
            self._keepalive_runner = runner
            log.info("Keepalive HTTP server listening on 0.0.0.0:%d",
                     KEEPALIVE_PORT)
        except OSError as exc:
            log.warning("Could not start keepalive server on port %d: %s",
                        KEEPALIVE_PORT, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Keepalive server failed to start: %s", exc)

    async def _prewarm(self) -> None:
        """Open keep-alive connections to the platform hosts up front."""

        if self.http_sniper is None:
            return
        urls = list(checkers.PREWARM_URLS)
        if INSTANTUSERNAME_FALLBACK:
            # Warm the second-opinion host too, so a rescued check does not
            # pay DNS+TLS on its first use either.
            urls.append(f"{checkers.INSTANTUSERNAME_BASE_URL}/")
        started = time.monotonic()
        try:
            warmed = await checkers.prewarm_connections(
                self.http_sniper, self.proxy_provider, urls=urls)
            log.info("Pre-warmed %d/%d platform connections in %.2fs",
                     warmed, len(urls), time.monotonic() - started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("Connection pre-warm failed: %s",
                      checkers._redact_sensitive_text(exc))

    async def _initial_health_check(self) -> None:
        """Probe every proxy once at startup without blocking the login."""

        if self.proxy_pool is None or self.http_sniper is None:
            return
        try:
            await self.proxy_pool.health_check(self.http_sniper, timeout=3.0)
            log.info("Initial proxy health: %d/%d alive",
                     self.proxy_pool.alive_count, self.proxy_pool.size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Initial proxy health check failed: %s",
                        checkers._redact_sensitive_text(exc))

    def _next_proxy(self) -> str | None:
        """Get the next proxy from the pool, or None for a direct connection."""

        return self.proxy_provider()

    async def close(self) -> None:
        try:
            for task in (self._health_task, self._initial_health_task,
                         self._prewarm_task, self._services_task):
                if task is None or task.done():
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    log.debug("Background task ended with an error: %s", exc)
        finally:
            if self._keepalive_runner is not None:
                try:
                    await self._keepalive_runner.cleanup()
                except Exception as exc:  # noqa: BLE001
                    log.debug("Keepalive shutdown failed: %s", exc)
                finally:
                    self._keepalive_runner = None
            try:
                if self.http_sniper and not self.http_sniper.closed:
                    await self.http_sniper.close()
            finally:
                try:
                    if self.dnsrobot_browser is not None:
                        await self.dnsrobot_browser.close()
                finally:
                    try:
                        if self._playwright is not None:
                            await self._playwright.stop()
                    finally:
                        self.http_sniper = None
                        self.dnsrobot_browser = None
                        self.dnsrobot_semaphore = None
                        self._playwright = None
                        await super().close()

    # -- state helpers ------------------------------------------------------

    def _cooldown_hit(self, user_id: int) -> bool:
        """Return True when this user exhausted their current check window."""

        now = time.monotonic()
        if len(self._buckets) > 1000:
            self._buckets = defaultdict(
                deque,
                {uid: bucket for uid, bucket in self._buckets.items()
                 if bucket and now - bucket[-1] <= USER_WINDOW_SECONDS},
            )
        bucket = self._buckets[user_id]
        while bucket and now - bucket[0] > USER_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= USER_MAX_CHECKS:
            return True
        bucket.append(now)
        return False

    def _cached(self, username: str) -> list[checkers.Result] | None:
        key = username.lower()
        hit = self._cache.get(key)
        if hit is None:
            return None
        # Use smart TTL: taken results live longer, available results expire sooner
        results = hit[1]
        has_available = any(r.status == checkers.AVAILABLE for r in results)
        ttl = CACHE_TTL_AVAILABLE if has_available else CACHE_TTL_TAKEN
        if time.monotonic() - hit[0] < ttl:
            return hit[1]

        # Expired: evict on read.
        del self._cache[key]
        return None

    def _store(self, username: str, results: list[checkers.Result]) -> None:
        """Cache one definitive answer, keeping the cache bounded."""

        self._cache[username.lower()] = (time.monotonic(), results)
        if len(self._cache) <= CACHE_MAX_ENTRIES:
            return
        # Drop everything already stale, then oldest-first until back in
        # budget. Pruning on write (not only on an expired read) means a busy
        # server can never grow the cache without bound.
        now = time.monotonic()
        self._cache = {
            key: value for key, value in self._cache.items()
            if now - value[0] < CACHE_TTL_TAKEN
        }
        if len(self._cache) > CACHE_MAX_ENTRIES:
            for key, _ in sorted(
                    self._cache.items(), key=lambda item: item[1][0],
            )[:len(self._cache) - CACHE_MAX_ENTRIES]:
                self._cache.pop(key, None)

    @staticmethod
    def _cacheable(results: list[checkers.Result]) -> bool:
        """Cache complete, definitive answers; never cache a partial outage."""

        definitive = {checkers.AVAILABLE, checkers.TAKEN, checkers.INVALID}
        allowed = definitive | {checkers.SKIPPED}
        return (bool(results)
                and all(result.status in allowed for result in results)
                and any(result.status in definitive for result in results))

    # -- Discord reaction helpers -----------------------------------------

    async def _react(
        self,
        message: discord.Message,
        emoji: str,
        timeout: float | None = None,
    ) -> None:
        """Add one reaction without allowing Discord REST to stall the bot."""

        cap = REACTION_TIMEOUT if timeout is None else timeout
        if cap <= 0:
            log.warning("Skipping reaction %r: response deadline exhausted", emoji)
            return
        reaction_task = asyncio.create_task(message.add_reaction(emoji))
        try:
            done, _ = await asyncio.wait({reaction_task}, timeout=cap)
        except asyncio.CancelledError:
            reaction_task.cancel()
            reaction_task.add_done_callback(self._consume_cancelled_reaction_task)
            raise

        if reaction_task not in done:
            reaction_task.cancel()
            reaction_task.add_done_callback(self._consume_cancelled_reaction_task)
            log.warning("Reaction %r exceeded the %.2fs response cap", emoji, cap)
            return

        try:
            reaction_task.result()
        except discord.Forbidden:
            log.warning("Missing 'Add Reactions' permission in #%s",
                        getattr(message.channel, "name", message.channel.id))
        except discord.HTTPException as exc:
            log.warning("Reaction %r failed: %s", emoji, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Unexpected reaction failure for %r: %s", emoji, exc)

    async def _react_all(
        self,
        message: discord.Message,
        emojis: list[str],
        deadline: float,
    ) -> None:
        """React in parallel, sharing the remaining valid-message budget."""

        if not emojis:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning("Skipping reactions: response deadline exhausted")
            return
        per_reaction_cap = min(REACTION_TIMEOUT, remaining)
        await asyncio.gather(*(
            self._react(message, emoji, timeout=per_reaction_cap)
            for emoji in emojis
        ))

    @staticmethod
    def _consume_cancelled_reaction_task(task: asyncio.Task) -> None:
        """Consume a late reaction outcome after the response budget expires."""

        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("Late reaction task exited after deadline: %s",
                      checkers._redact_sensitive_text(exc))

    @staticmethod
    def _consume_cancelled_checker_task(task: asyncio.Task) -> None:
        """Consume a late task outcome so cancellation never emits a warning."""

        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            log.debug("Late checker task exited after deadline: %s",
                      checkers._redact_sensitive_text(exc))

    async def _run_checks_with_deadline(
        self,
        username: str,
        check_budget: float,
    ) -> list[checkers.Result]:
        """Return checker results without waiting for a bad task to cancel."""

        checker_task = asyncio.create_task(checkers.run_all_checks(
            self.http_sniper, username,
            proxy=self.proxy_provider,
            discord_mode=DISCORD_CHECK_MODE,
            discord_probe_url=DISCORD_PROBE_URL,
            discord_probe_headers=DISCORD_PROBE_HEADERS,
            timeout=check_budget,
            discord_account_api_url=DISCORD_ACCOUNT_API_URL,
            discord_account_api_headers=DISCORD_ACCOUNT_API_HEADERS,
            dnsrobot_browser=self.dnsrobot_browser,
            dnsrobot_semaphore=self.dnsrobot_semaphore,
            enable_extra_platforms=ENABLE_EXTRA_PLATFORMS,
            instantusername_fallback=INSTANTUSERNAME_FALLBACK,
        ))

        try:
            done, _ = await asyncio.wait({checker_task}, timeout=check_budget)
        except asyncio.CancelledError:
            checker_task.cancel()
            checker_task.add_done_callback(self._consume_cancelled_checker_task)
            raise

        if checker_task not in done:
            checker_task.cancel()
            checker_task.add_done_callback(self._consume_cancelled_checker_task)
            return checkers.timeout_results("response deadline reached")

        try:
            return checker_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Checker task failed before its deadline: %s",
                        checkers._redact_sensitive_text(exc))
            return checkers.timeout_results(
                "checker task failed", include_extra=ENABLE_EXTRA_PLATFORMS)

    async def _send_reply(
        self,
        message: discord.Message,
        text: str,
        timeout: float,
    ) -> discord.Message | None:
        """Post the answer as a reply, bounded so REST can never stall us."""

        if timeout <= 0:
            log.warning("Skipping reply: response deadline exhausted")
            return None
        try:
            return await asyncio.wait_for(
                message.reply(text, mention_author=REPLY_MENTION_AUTHOR),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Reply exceeded the %.2fs cap", timeout)
        except discord.Forbidden:
            log.warning("Missing 'Send Messages' permission in #%s",
                        getattr(message.channel, "name", message.channel.id))
        except discord.HTTPException as exc:
            log.warning("Reply failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Unexpected reply failure: %s",
                        checkers._redact_sensitive_text(exc))
        return None

    async def _edit_reply(
        self,
        sent: discord.Message,
        text: str,
        timeout: float,
    ) -> bool:
        """Update an already-posted reply. Returns False if editing is futile."""

        if timeout <= 0:
            return True
        try:
            await asyncio.wait_for(sent.edit(content=text), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.warning("Reply edit exceeded the %.2fs cap", timeout)
        except discord.NotFound:
            log.warning("Reply was deleted before it could be updated")
            return False
        except discord.Forbidden:
            log.warning("Not allowed to edit the reply")
            return False
        except discord.HTTPException as exc:
            log.warning("Reply edit failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("Unexpected reply-edit failure: %s",
                        checkers._redact_sensitive_text(exc))
        return True

    async def _publish_results(
        self,
        message: discord.Message,
        results: list[checkers.Result],
        deadline: float,
    ) -> None:
        """One-shot output for a complete result set (cache hits, batched mode)."""

        jobs = []
        if REACT_ENABLED:
            jobs.append(self._react_all(
                message, self._verdict_emojis(results), deadline))
        if REPLY_ENABLED:
            jobs.append(self._send_reply(
                message, format_results(results),
                min(REACTION_TIMEOUT, deadline - time.monotonic())))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    @staticmethod
    def _verdict_emojis(results: list[checkers.Result]) -> list[str]:
        """Translate normalized checker results into reaction emojis."""

        available = [result for result in results if result.available]
        if available:
            return [result.emoji for result in available]
        statuses = {result.status for result in results}
        known_non_unknown = {
            checkers.AVAILABLE, checkers.TAKEN, checkers.INVALID, checkers.SKIPPED,
        }
        unknown = {checkers.ERROR, checkers.BLOCKED}
        if (not statuses or statuses - known_non_unknown
                or statuses & unknown or statuses <= {checkers.SKIPPED}):
            return [EMOJI_ALL_FAILED]
        return [EMOJI_NONE_AVAILABLE]

    @staticmethod
    def _fill_missing(results: list[checkers.Result]) -> list[checkers.Result]:
        """Add an honest ERROR for any configured platform that never answered."""

        expected = (checkers.PLATFORMS if ENABLE_EXTRA_PLATFORMS
                    else checkers.CORE_PLATFORMS)
        seen = {result.platform for result in results}
        by_platform = {result.platform: result for result in results}
        ordered: list[checkers.Result] = []
        for platform, emoji in expected:
            if platform in seen:
                ordered.append(by_platform[platform])
            else:
                ordered.append(checkers.Result(
                    platform, emoji, checkers.ERROR, "check deadline reached"))
        return ordered

    async def _live_reply(
        self,
        message: discord.Message,
        results: list[checkers.Result],
        done: asyncio.Event,
        deadline: float,
        changed: asyncio.Event | None = None,
    ) -> None:
        """Paint the reply immediately, then refresh it as results arrive.

        The first paint happens on the very first tick so the member sees an
        answer almost instantly; later paints are throttled to
        REPLY_EDIT_INTERVAL so a lookup cannot exhaust Discord's edit rate
        limit. The final paint always runs, so the message never ends up
        showing a stale, half-finished list.

        Wakes are event-driven: the stream loop signals ``changed`` whenever a
        platform reports, so the loop sleeps until something actually happens
        (or until a throttled edit becomes due) instead of polling on a timer.
        """

        sent: discord.Message | None = None
        last_text: str | None = None
        last_paint = 0.0
        editable = True
        send_failures = 0

        while True:
            finished = done.is_set()
            now = time.monotonic()
            due = last_paint == 0.0 or now - last_paint >= REPLY_EDIT_INTERVAL
            text = format_results(results, pending=not finished)

            if text != last_text and (due or finished) and editable:
                cap = min(REACTION_TIMEOUT, max(0.0, deadline - now))
                if finished:
                    # The final state matters more than the budget: allow the
                    # closing paint even if the response window just closed.
                    cap = max(cap, REACTION_TIMEOUT)
                if sent is None:
                    sent = await self._send_reply(message, text, cap)
                    if sent is None:
                        # Give up quickly on a channel we cannot post in
                        # rather than retrying every interval.
                        send_failures += 1
                        if finished or send_failures >= 2:
                            return
                else:
                    editable = await self._edit_reply(sent, text, cap)
                last_text = text
                last_paint = time.monotonic()

            if finished:
                return
            if not editable:
                # Cannot update the message any further; wait for the checks to
                # finish so the caller still gets a complete result list.
                await done.wait()
                continue

            # Sleep until the next interesting moment: a new result, the checks
            # finishing, or (when a paint is owed) the edit interval elapsing.
            if changed is not None:
                waiter = changed.wait()
            else:
                waiter = done.wait()
            if changed is not None and text != last_text and last_paint > 0.0:
                wake_in = max(0.0, REPLY_EDIT_INTERVAL
                              - (time.monotonic() - last_paint))
            else:
                wake_in = 1.0
            try:
                await asyncio.wait_for(waiter, timeout=wake_in)
            except asyncio.TimeoutError:
                continue
            if changed is not None:
                # Single-threaded loop: nothing can append between the wake
                # and this clear, so no result is ever missed.
                changed.clear()

    async def _stream_checks_and_respond(
        self,
        message: discord.Message,
        username: str,
        check_budget: float,
        deadline: float,
    ) -> list[checkers.Result]:
        """Answer each platform the instant it reports, then settle the rest.

        Every check still starts simultaneously under the same shared budget;
        the difference is that a fast platform is no longer held hostage by the
        slowest one. Time-to-first-answer drops from "slowest check" to
        "fastest check".
        """

        results: list[checkers.Result] = []
        reaction_tasks: list[asyncio.Task] = []
        done = asyncio.Event()
        changed = asyncio.Event()
        reply_task: asyncio.Task | None = None
        if REPLY_ENABLED:
            reply_task = asyncio.create_task(
                self._live_reply(message, results, done, deadline, changed))

        stream = checkers.stream_all_checks(
            self.http_sniper, username,
            proxy=self.proxy_provider,
            discord_mode=DISCORD_CHECK_MODE,
            discord_probe_url=DISCORD_PROBE_URL,
            discord_probe_headers=DISCORD_PROBE_HEADERS,
            timeout=check_budget,
            discord_account_api_url=DISCORD_ACCOUNT_API_URL,
            discord_account_api_headers=DISCORD_ACCOUNT_API_HEADERS,
            dnsrobot_browser=self.dnsrobot_browser,
            dnsrobot_semaphore=self.dnsrobot_semaphore,
            enable_extra_platforms=ENABLE_EXTRA_PLATFORMS,
            instantusername_fallback=INSTANTUSERNAME_FALLBACK,
        )

        # Hard outer bound, mirroring the batched path: a checker that ignores
        # its own timeout must never hold the answer past the budget.
        stream_iter = stream.__aiter__()
        checks_end = time.monotonic() + check_budget
        try:
            while True:
                remaining_checks = checks_end - time.monotonic()
                if remaining_checks <= 0:
                    log.warning("Check budget exhausted with %d platforms in",
                                len(results))
                    break
                try:
                    result = await asyncio.wait_for(
                        stream_iter.__anext__(), timeout=remaining_checks)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    log.warning("Check budget reached while streaming results")
                    break

                results.append(result)
                changed.set()
                if not (REACT_ENABLED and result.available):
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log.warning("Skipping %r reaction: deadline exhausted",
                                result.platform)
                    continue
                reaction_tasks.append(asyncio.create_task(self._react(
                    message, result.emoji,
                    timeout=min(REACTION_TIMEOUT, remaining))))
        except asyncio.CancelledError:
            for task in reaction_tasks:
                task.cancel()
            if reply_task is not None:
                reply_task.cancel()
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Checker stream failed: %s",
                        checkers._redact_sensitive_text(exc))
        finally:
            await stream.aclose()

        # Replace the shared list's contents in place so the reply task, which
        # holds a reference to it, renders the completed set. Signal the change
        # before ``done`` so the reply loop always wakes for the final paint.
        results[:] = self._fill_missing(results)
        changed.set()
        done.set()

        if reply_task is not None:
            try:
                await reply_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Reply task failed: %s",
                            checkers._redact_sensitive_text(exc))

        # Nothing was free: the single summary emoji can only be decided once
        # every platform has reported.
        if REACT_ENABLED and not any(result.available for result in results):
            await self._react_all(
                message, self._verdict_emojis(results), deadline)

        if reaction_tasks:
            await asyncio.gather(*reaction_tasks, return_exceptions=True)
        return list(results)

    async def _write_hit_log(
        self,
        message: discord.Message,
        available: list[checkers.Result],
        deadline: float,
    ) -> None:
        """Write the optional hit log without extending the user response path."""

        if not LOG_CHANNEL_ID:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.debug("Skipping hit log: response deadline exhausted")
            return

        channel = self.get_channel(LOG_CHANNEL_ID)
        try:
            if channel is None:
                channel = await asyncio.wait_for(
                    self.fetch_channel(LOG_CHANNEL_ID), timeout=remaining)
        except asyncio.TimeoutError:
            log.warning("Could not fetch hit-log channel before the response deadline")
            return
        except discord.HTTPException as exc:
            log.warning("Could not fetch hit-log channel: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Unexpected hit-log lookup failure: %s",
                checkers._redact_sensitive_text(exc),
            )
            return

        if channel is None:
            return
        names = ", ".join(
            f"{result.platform} {result.emoji}" for result in available)
        text = (
            f"🎯 `{message.content.strip()}` is FREE on: {names} "
            f"(found by {message.author.mention})")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.debug("Skipping hit log send: response deadline exhausted")
            return
        try:
            await asyncio.wait_for(channel.send(text), timeout=remaining)
        except asyncio.TimeoutError:
            log.warning("Hit-log message exceeded the response deadline")
        except discord.HTTPException as exc:
            log.warning("Could not write to log channel: %s", exc)
        except AttributeError as exc:
            log.warning("Hit-log channel cannot receive messages: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Unexpected hit-log failure: %s",
                checkers._redact_sensitive_text(exc),
            )

    # -- events -------------------------------------------------------------

    async def on_ready(self) -> None:
        # Discord fires on_ready again after every resume; the banner is
        # startup information, so print it only the first time. The
        # keepalive server, connection pre-warm and fallback service refresh
        # all start once in setup_hook - restarting them here would double
        # the work (and fail to re-bind the keepalive port).
        if self._banner_shown:
            log.info("Reconnected to the Discord gateway as %s", self.user)
            return
        self._banner_shown = True
        print("=" * 62)
        print(f"🟢 MULTI-SNIPER v3.0 ONLINE as {self.user}")
        print("🔒 Watching channel : "
              f"{TARGET_CHANNEL_ID if TARGET_CHANNEL_ID else 'ALL CHANNELS'}")
        if ENABLE_EXTRA_PLATFORMS:
            print("🕹️ Platforms        : Minecraft | guns.lol | Discord | "
                  "GitHub | Steam | Reddit | Instagram | Twitter/X")
        else:
            print("🕹️ Platforms        : Minecraft | guns.lol | "
                  f"Discord (mode: {DISCORD_CHECK_MODE})")
        if DISCORD_CHECK_MODE == "dnsrobot":
            print("🌐 DNS Robot browser : "
                  f"{'ready' if self.dnsrobot_browser else 'unavailable'}")
        # Proxy pool status
        if self.proxy_pool is not None:
            print(f"🧊 Proxy pool       : {self.proxy_pool.size} proxies | "
                  f"{self.proxy_pool.alive_count} alive "
                  f"(from {_proxy_source_summary()})")
            print(f"   └─ {self.proxy_pool.status_summary()}")
        elif PROXY_URL:
            print("🧊 Proxy            : on (single)")
        else:
            print("🧊 Proxy            : off (direct)")
        print(f"💬 Answer mode      : {RESPONSE_MODE}"
              + (f" (live edits every {REPLY_EDIT_INTERVAL:.2f}s)"
                 if REPLY_ENABLED else ""))
        if KEEPALIVE_PORT:
            print(f"🌐 Keepalive HTTP   : 0.0.0.0:{KEEPALIVE_PORT} (/health)")
        print(f"⏳ User cooldown    : {USER_MAX_CHECKS} checks / "
              f"{USER_WINDOW_SECONDS:.2f}s")
        print(f"⚡ Response budget  : {RESPONSE_BUDGET_SECONDS:.2f}s "
              f"(reaction cap {REACTION_TIMEOUT:.2f}s)")
        print(f"🔁 Fallback source  : "
              f"{'instantusername.com' if INSTANTUSERNAME_FALLBACK else 'off'}"
              f" ({len(checkers.INSTANTUSERNAME_SERVICES)} platforms covered)")
        print(f"🧵 Concurrency      : pool {HTTP_POOL_LIMIT} conns "
              f"({HTTP_POOL_LIMIT_PER_HOST}/host) | duplicate lookups "
              f"{'shared' if COALESCE_DUPLICATES else 'independent'}")
        print(f"💾 Cache TTL        : {CACHE_TTL_AVAILABLE:.0f}s (free) / "
              f"{CACHE_TTL_TAKEN:.0f}s (taken)")
        print("=" * 62)

    async def _lookup(
        self,
        message: discord.Message,
        username: str,
        deadline: float,
    ) -> list[checkers.Result]:
        """Answer one message, sharing work with identical lookups in flight.

        Lookups are otherwise fully independent: every message gets its own
        budget, its own result list and its own reply, so any number of
        members can ask about different usernames at the same time.
        """

        key = username.lower()
        if COALESCE_DUPLICATES:
            running = self._inflight.get(key)
            if running is not None:
                log.info("joining in-flight lookup for %r", username)
                results = await self._await_inflight(running, deadline)
                if results is not None:
                    await self._publish_results(message, results, deadline)
                    return results
                # The leader failed or ran out of time: fall through and do
                # the work ourselves rather than reporting nothing.

        leader: asyncio.Future | None = None
        if COALESCE_DUPLICATES and key not in self._inflight:
            leader = asyncio.get_running_loop().create_future()
            self._inflight[key] = leader

        results: list[checkers.Result] = []
        try:
            results = await self._perform_lookup(message, username, deadline)
        finally:
            if leader is not None:
                # Only retract our own entry; a later lookup may own it now.
                if self._inflight.get(key) is leader:
                    del self._inflight[key]
                if not leader.done():
                    # Followers copy the list: nobody should be able to mutate
                    # another message's results.
                    leader.set_result(list(results))
        return results

    @staticmethod
    async def _await_inflight(
        running: asyncio.Future,
        deadline: float,
    ) -> list[checkers.Result] | None:
        """Wait for another message's identical lookup, within our budget."""

        remaining = deadline - time.monotonic() - REACTION_TIMEOUT
        if remaining <= 0:
            return None
        try:
            results = await asyncio.wait_for(
                asyncio.shield(running), timeout=remaining)
        except asyncio.TimeoutError:
            log.info("shared lookup did not finish inside our budget")
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("shared lookup failed: %s",
                      checkers._redact_sensitive_text(exc))
            return None
        return list(results) if results else None

    async def _perform_lookup(
        self,
        message: discord.Message,
        username: str,
        deadline: float,
    ) -> list[checkers.Result]:
        """Run the platform checks for one message and publish the answer."""

        # Reserve time for the reaction. All checkers get this same
        # wall-clock cap, not sequential caps.
        check_budget = deadline - time.monotonic() - REACTION_TIMEOUT
        if check_budget <= 0:
            results = checkers.timeout_results(
                "response deadline reached",
                include_extra=ENABLE_EXTRA_PLATFORMS)
            await self._publish_results(message, results, deadline)
        elif STREAM_REACTIONS:
            # Fastest path: answer each platform as it reports.
            results = await self._stream_checks_and_respond(
                message, username, check_budget, deadline)
        else:
            # Batched path: wait for every platform, then answer once.
            results = await self._run_checks_with_deadline(
                username, check_budget)
            await self._publish_results(message, results, deadline)

        if self._cacheable(results):
            self._store(username, results)
        else:
            log.info("not caching inconclusive results for %r", username)

        for result in results:
            log.info("%-12s %-9s %-28s (%s)",
                     result.platform, result.status, result.detail, username)
        return results

    async def on_message(self, message: discord.Message) -> None:
        """Filter -> cooldown -> parallel checks -> same-message reactions."""

        # 1. Never react to bots or webhooks (prevents reaction loops).
        if message.webhook_id or getattr(message.author, "bot", False):
            return

        # 2. Only the configured channel, if one was set.
        if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
            return

        # 3. The payload must be one bare username-looking token.
        username = message.content.strip()
        if not checkers.USERNAME_MESSAGE_PATTERN.fullmatch(username):
            return

        # All later work has one budget. Start after inexpensive filtering so
        # ignored messages don't consume a deadline at all.
        deadline = time.monotonic() + RESPONSE_BUDGET_SECONDS

        # 4. Cooldown guard: protects the platforms and receives immediate UX.
        if self._cooldown_hit(message.author.id):
            await self._react_all(message, [EMOJI_COOLDOWN], deadline)
            return

        self._checks_served += 1

        # 5. Serve repeat lookups from cache when possible. A cache hit needs
        # no sockets at all, so it is by far the fastest path.
        cached = self._cached(username)
        if cached is not None:
            log.info("cache hit for %r", username)
            results = cached
            await self._publish_results(message, results, deadline)
        else:
            results = await self._lookup(message, username, deadline)

        # 7. Optional private log for genuine availability hits. It is bounded
        # by the same deadline and never delays the member-visible reaction.
        available = [result for result in results if result.available]
        if available and LOG_CHANNEL_ID:
            await self._write_hit_log(message, available, deadline)


def main() -> None:
    """Validate user-supplied configuration before connecting to Discord."""

    if not TOKEN:
        raise SystemExit(
            "❌ DISCORD_TOKEN missing. Copy .env.example to .env and paste "
            "your bot token from the Discord Developer Portal.")
    if _has_http_control_chars(TOKEN):
        raise SystemExit("❌ DISCORD_TOKEN must not contain control characters.")
    if RESPONSE_MODE_RAW and RESPONSE_MODE_RAW != RESPONSE_MODE:
        raise SystemExit(
            f"❌ RESPONSE_MODE={RESPONSE_MODE_RAW!r} is not valid. "
            "Use 'reply', 'react', or 'both'.")
    if DISCORD_CHECK_MODE not in (
            "off", "dnsrobot", "account", "account_api", "probe"):
        raise SystemExit(
            "❌ DISCORD_CHECK_MODE must be 'off', 'dnsrobot', 'account', "
            "'account_api', or 'probe'.")
    # Validate every proxy that will actually be used, whether it came from
    # PROXY_URL, PROXY_URLS, or the proxy file.
    resolved_proxies = configured_proxies()
    if PROXY_FILE and not resolved_proxies and os.path.exists(PROXY_FILE):
        raise SystemExit(
            f"❌ {PROXY_FILE} exists but contains no usable proxy. Use one "
            "line per proxy, for example 1.2.3.4:8080 or "
            "1.2.3.4:8080:user:pass, or delete the file to run without "
            "proxies.")
    for proxy in resolved_proxies:
        proxy_error = checkers.validate_proxy_url(proxy)
        if proxy_error:
            raise SystemExit(
                f"❌ Proxy in POOL: {proxy_error} "
                f"(offending entry: {short_proxy_url(proxy)})"
                + ("\n   SOCKS proxies are not supported: aiohttp needs an "
                   "http:// or https:// proxy endpoint."
                   if proxy.lower().startswith("socks") else ""))
    if DISCORD_ACCOUNT_API_TOKEN:
        if not checkers.is_valid_header_name(DISCORD_ACCOUNT_API_TOKEN_HEADER):
            raise SystemExit(
                "❌ DISCORD_ACCOUNT_API_TOKEN_HEADER is not a valid HTTP header name.")
        if _has_http_control_chars(DISCORD_ACCOUNT_API_TOKEN):
            raise SystemExit("❌ DISCORD_ACCOUNT_API_TOKEN must not contain control characters.")
        if _has_http_control_chars(DISCORD_ACCOUNT_API_TOKEN_SCHEME):
            raise SystemExit(
                "❌ DISCORD_ACCOUNT_API_TOKEN_SCHEME must not contain control characters.")
    if DISCORD_CHECK_MODE in ("account", "account_api"):
        account_error = checkers.validate_account_api_url(DISCORD_ACCOUNT_API_URL)
        if account_error:
            raise SystemExit(f"❌ {account_error}")
    if DISCORD_PROBE_TOKEN:
        if not checkers.is_valid_header_name(DISCORD_PROBE_TOKEN_HEADER):
            raise SystemExit("❌ DISCORD_PROBE_TOKEN_HEADER is not a valid HTTP header name.")
        if _has_http_control_chars(DISCORD_PROBE_TOKEN):
            raise SystemExit("❌ DISCORD_PROBE_TOKEN must not contain control characters.")
        if _has_http_control_chars(DISCORD_PROBE_TOKEN_SCHEME):
            raise SystemExit("❌ DISCORD_PROBE_TOKEN_SCHEME must not contain control characters.")
    if DISCORD_CHECK_MODE == "probe" and DISCORD_PROBE_URL:
        probe_error = checkers.validate_probe_url_template(DISCORD_PROBE_URL)
        if probe_error:
            raise SystemExit(f"❌ {probe_error}")
    SniperBot().run(TOKEN)


if __name__ == "__main__":
    main()
