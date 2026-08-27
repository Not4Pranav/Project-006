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

Proxy pool features:
    - Round-robin rotation across healthy proxies
    - Automatic health checking every 30s
    - Dead-proxy cooldown and automatic recovery
    - Falls back to direct connection when all proxies are down

Run locally: python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import defaultdict, deque

import aiohttp
import discord
from dotenv import load_dotenv

import checkers
from proxies import ProxyPool, parse_proxy_list

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
USER_MAX_CHECKS = _bounded_int("USER_MAX_CHECKS", 3, minimum=1, maximum=10_000)
USER_WINDOW_SECONDS = max(_opt_float("USER_WINDOW_SECONDS", 60), 0.1)
RESULT_CACHE_TTL = max(_opt_float("RESULT_CACHE_TTL", 300), 0.0)

# Smart cache: taken names stay available longer (they rarely free up),
# while available names expire faster (they might get sniped).
CACHE_TTL_TAKEN = max(_opt_float("CACHE_TTL_TAKEN", 600), 0.0)   # 10 min
CACHE_TTL_AVAILABLE = max(_opt_float("CACHE_TTL_AVAILABLE", 120), 0.0)  # 2 min

# Feedback emojis
EMOJI_NONE_AVAILABLE = "❌"
EMOJI_ALL_FAILED = "⚠️"
EMOJI_COOLDOWN = "\u23f3"  # ⏳

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
        # Proxy pool for rotation and failover
        self.proxy_pool: ProxyPool | None = None
        # Background health check task
        self._health_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        """Create one pooled outbound session before gateway events arrive."""

        # Optimized TCP connector for connection pooling and reuse
        connector = aiohttp.TCPConnector(
            limit=25,                 # Total connection pool size
            limit_per_host=10,        # Max connections per target host
            ttl_dns_cache=300,        # DNS cache for 5 minutes
            enable_cleanup_closed=True,  # Clean up closed connections
            force_close=False,        # Keep connections alive for reuse
            keepalive_timeout=30,     # Keep idle connections for 30s
        )

        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
        self.http_sniper = aiohttp.ClientSession(
            headers=checkers.BROWSER_HEADERS,
            timeout=timeout,
            connector=connector,
        )

        # Initialize proxy pool
        proxy_list = parse_proxy_list(PROXY_URLS_RAW)
        if PROXY_URL and PROXY_URL not in proxy_list:
            proxy_list.insert(0, PROXY_URL)
        if proxy_list:
            self.proxy_pool = ProxyPool(proxy_list)
            # Start background health checking
            self._health_task = asyncio.create_task(
                self.proxy_pool.periodic_health_check(self.http_sniper))
            # Do an initial health check
            try:
                await self.proxy_pool.health_check(self.http_sniper, timeout=2.0)
            except Exception as exc:
                log.warning("Initial proxy health check failed: %s", exc)

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

    def _next_proxy(self) -> str | None:
        """Get the next proxy from the pool, or None for direct connection."""
        if self.proxy_pool is not None:
            return self.proxy_pool.next()
        return PROXY_URL

    async def close(self) -> None:
        try:
            if self._health_task and not self._health_task.done():
                self._health_task.cancel()
                try:
                    await self._health_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
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

        # Expired: evict on read
        del self._cache[key]
        if len(self._cache) > 5000:
            now = time.monotonic()
            self._cache = {
                cache_key: value for cache_key, value in self._cache.items()
                if now - value[0] < CACHE_TTL_TAKEN
            }
        return None

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
            proxy=self._next_proxy,
            discord_mode=DISCORD_CHECK_MODE,
            discord_probe_url=DISCORD_PROBE_URL,
            discord_probe_headers=DISCORD_PROBE_HEADERS,
            timeout=check_budget,
            discord_account_api_url=DISCORD_ACCOUNT_API_URL,
            discord_account_api_headers=DISCORD_ACCOUNT_API_HEADERS,
            dnsrobot_browser=self.dnsrobot_browser,
            dnsrobot_semaphore=self.dnsrobot_semaphore,
            enable_extra_platforms=ENABLE_EXTRA_PLATFORMS,
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
            return checkers.timeout_results("checker task failed")

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
        print("=" * 62)
        print(f"🟢 MULTI-SNIPER v2.0 ONLINE as {self.user}")
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
                  f"{self.proxy_pool.alive_count} alive")
            print(f"   └─ {self.proxy_pool.status_summary()}")
        elif PROXY_URL:
            print(f"🧊 Proxy            : on (single)")
        else:
            print(f"🧊 Proxy            : off (direct)")
        print(f"⏳ User cooldown    : {USER_MAX_CHECKS} checks / "
              f"{USER_WINDOW_SECONDS:.0f}s")
        print(f"⚡ Response budget  : {RESPONSE_BUDGET_SECONDS:.2f}s "
              f"(reaction cap {REACTION_TIMEOUT:.2f}s)")
        print(f"💾 Cache TTL        : {CACHE_TTL_AVAILABLE:.0f}s (free) / "
              f"{CACHE_TTL_TAKEN:.0f}s (taken)")
        print("=" * 62)

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

        # 5. Serve repeat lookups from cache when possible.
        cached = self._cached(username)
        if cached is not None:
            results = cached
            log.info("cache hit for %r", username)
        else:
            # Reserve time for the reaction. All checkers get this same
            # wall-clock cap, not sequential caps.
            check_budget = deadline - time.monotonic() - REACTION_TIMEOUT
            if check_budget <= 0:
                results = checkers.timeout_results("response deadline reached")
            else:
                results = await self._run_checks_with_deadline(username, check_budget)

            if self._cacheable(results):
                self._cache[username.lower()] = (time.monotonic(), results)
            else:
                log.info("not caching inconclusive results for %r", username)

            for result in results:
                log.info("%-12s %-9s %-28s (%s)",
                         result.platform, result.status, result.detail, username)

        # 6. Translate normalized checker results into reactions.
        available = [result for result in results if result.available]
        if available:
            reaction_emojis = [result.emoji for result in available]
        else:
            statuses = {result.status for result in results}
            known_non_unknown = {
                checkers.AVAILABLE, checkers.TAKEN, checkers.INVALID,
                checkers.SKIPPED,
            }
            unknown = {checkers.ERROR, checkers.BLOCKED}
            if (not statuses or statuses - known_non_unknown
                    or statuses & unknown or statuses <= {checkers.SKIPPED}):
                reaction_emojis = [EMOJI_ALL_FAILED]
            else:
                reaction_emojis = [EMOJI_NONE_AVAILABLE]

        # 7. The member-visible answer is complete before optional logging.
        await self._react_all(message, reaction_emojis, deadline)

        # 8. Optional private log for genuine availability hits. It is bounded
        # by the same deadline and never delays the member-visible reaction.
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
    if DISCORD_CHECK_MODE not in (
            "off", "dnsrobot", "account", "account_api", "probe"):
        raise SystemExit(
            "❌ DISCORD_CHECK_MODE must be 'off', 'dnsrobot', 'account', "
            "'account_api', or 'probe'.")
    if PROXY_URL:
        proxy_error = checkers.validate_proxy_url(PROXY_URL)
        if proxy_error:
            raise SystemExit(f"❌ {proxy_error}")
    # Validate each proxy in the pool
    for proxy in parse_proxy_list(PROXY_URLS_RAW):
        proxy_error = checkers.validate_proxy_url(proxy)
        if proxy_error:
            raise SystemExit(f"❌ Proxy in POOL: {proxy_error}")
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
