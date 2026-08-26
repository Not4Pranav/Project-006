"""
Multi-Sniper - a Discord username availability checker.

A member posts one bare username in the watched channel. The bot validates it,
runs Minecraft, guns.lol, and optional Discord checks in parallel, receives the
normalized checker results, and reacts to the *same* Discord message:

    🕹️  Minecraft free
    🔫  guns.lol free
    🐈‍⬛ Discord free (best-effort probe only; disabled by default)
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
    PROXY_URL                 optional HTTP(S) proxy for outbound checks
    DISCORD_CHECK_MODE        off (default) | probe
    DISCORD_PROBE_URL         authorized external checker URL template (optional)
    DISCORD_PROBE_TOKEN       optional token sent only to that checker endpoint
    CHECK_TIMEOUT             per outbound HTTP request (default 3)
    RESPONSE_BUDGET_SECONDS   checks + reactions after MESSAGE_CREATE (default 4.5)
    REACTION_TIMEOUT          cap for each Discord reaction call (default .75)
    USER_MAX_CHECKS           checks per user per window (default 3)
    USER_WINDOW_SECONDS       cooldown window seconds (default 60)
    RESULT_CACHE_TTL          cache repeat lookups seconds (default 300)

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


def _discord_probe_headers() -> dict[str, str] | None:
    """Build the optional auth header without ever logging its token value."""

    if not DISCORD_PROBE_TOKEN:
        return None
    value = (f"{DISCORD_PROBE_TOKEN_SCHEME} {DISCORD_PROBE_TOKEN}".strip()
             if DISCORD_PROBE_TOKEN_SCHEME else DISCORD_PROBE_TOKEN)
    return {DISCORD_PROBE_TOKEN_HEADER: value}


TARGET_CHANNEL_ID = _opt_int("TARGET_CHANNEL_ID")
LOG_CHANNEL_ID = _opt_int("LOG_CHANNEL_ID")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
DISCORD_CHECK_MODE = os.getenv("DISCORD_CHECK_MODE", "off").strip().lower()
DISCORD_PROBE_URL = os.getenv("DISCORD_PROBE_URL", "").strip() or None
DISCORD_PROBE_TOKEN = os.getenv("DISCORD_PROBE_TOKEN", "").strip()
DISCORD_PROBE_TOKEN_HEADER = os.getenv(
    "DISCORD_PROBE_TOKEN_HEADER", "Authorization").strip() or "Authorization"
DISCORD_PROBE_TOKEN_SCHEME = os.getenv("DISCORD_PROBE_TOKEN_SCHEME", "Bearer").strip()
DISCORD_PROBE_HEADERS = _discord_probe_headers()

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
        # Per-user token bucket: {user_id: deque[timestamps]}
        self._buckets: dict[int, deque[float]] = defaultdict(deque)
        # Recent results cache: {username_lower: (timestamp, [Result, ...])}
        self._cache: dict[str, tuple[float, list[checkers.Result]]] = {}

    async def setup_hook(self) -> None:
        """Create one pooled outbound session before gateway events arrive."""

        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
        self.http_sniper = aiohttp.ClientSession(
            headers=checkers.BROWSER_HEADERS, timeout=timeout)

    async def close(self) -> None:
        if self.http_sniper and not self.http_sniper.closed:
            await self.http_sniper.close()
        self.http_sniper = None
        await super().close()

    # -- state helpers ------------------------------------------------------

    def _cooldown_hit(self, user_id: int) -> bool:
        """Return True when this user exhausted their current check window."""

        now = time.monotonic()
        # Occasional pruning keeps a long-running bot from retaining stale users.
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
        if time.monotonic() - hit[0] < RESULT_CACHE_TTL:
            return hit[1]

        # Expired: evict on read, then occasionally sweep old stragglers.
        del self._cache[key]
        if len(self._cache) > 5000:
            now = time.monotonic()
            self._cache = {
                cache_key: value for cache_key, value in self._cache.items()
                if now - value[0] < RESULT_CACHE_TTL
            }
        return None

    @staticmethod
    def _cacheable(results: list[checkers.Result]) -> bool:
        """Cache complete, definitive answers; never cache a partial outage."""

        definitive = {checkers.AVAILABLE, checkers.TAKEN, checkers.INVALID}
        unknown = {checkers.ERROR, checkers.BLOCKED}
        return (bool(results)
                and not any(result.status in unknown for result in results)
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
        try:
            await asyncio.wait_for(message.add_reaction(emoji), timeout=cap)
        except asyncio.TimeoutError:
            log.warning("Reaction %r exceeded the %.2fs response cap", emoji, cap)
        except discord.Forbidden:
            log.warning("Missing 'Add Reactions' permission in #%s",
                        getattr(message.channel, "name", message.channel.id))
        except discord.HTTPException as exc:
            log.warning("Reaction %r failed: %s", emoji, exc)
        except Exception as exc:  # noqa: BLE001 - intentionally isolate one reaction
            # Keep one malformed/failed reaction from aborting the remaining
            # reactions or the optional hit log. asyncio.CancelledError is a
            # BaseException, so shutdown cancellation still propagates.
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
        # Discord's REST rate limiter remains in control; parallel coroutines
        # simply avoid serializing independent emoji calls in our handler.
        await asyncio.gather(*(
            self._react(message, emoji, timeout=per_reaction_cap)
            for emoji in emojis
        ))

    @staticmethod
    def _consume_cancelled_checker_task(task: asyncio.Task) -> None:
        """Consume a late task outcome so cancellation never emits a warning."""

        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - consume any late task failure
            log.debug("Late checker task exited after deadline: %s",
                      checkers._redact_sensitive_text(exc))

    async def _run_checks_with_deadline(
        self,
        username: str,
        check_budget: float,
    ) -> list[checkers.Result]:
        """Return checker results without waiting for a bad task to cancel.

        ``asyncio.wait_for`` waits for cancellation cleanup. That is normally
        correct, but a future/custom checker that swallows cancellation could
        otherwise consume the reaction reserve. This fence cancels late work,
        consumes its eventual outcome, and immediately leaves time for Discord
        reactions.
        """

        checker_task = asyncio.create_task(checkers.run_all_checks(
            self.http_sniper, username,
            proxy=PROXY_URL,
            discord_mode=DISCORD_CHECK_MODE,
            discord_probe_url=DISCORD_PROBE_URL,
            discord_probe_headers=DISCORD_PROBE_HEADERS,
            timeout=check_budget,
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
        except Exception as exc:  # noqa: BLE001 - checker faults must not kill the handler
            log.warning("Checker task failed before its deadline: %s",
                        checkers._redact_sensitive_text(exc))
            return checkers.timeout_results("checker task failed")

    # -- events -------------------------------------------------------------

    async def on_ready(self) -> None:
        print("=" * 58)
        print(f"🟢 MULTI-SNIPER ONLINE as {self.user}")
        print("🔒 Watching channel : "
              f"{TARGET_CHANNEL_ID if TARGET_CHANNEL_ID else 'ALL CHANNELS'}")
        print("🕹️ Platforms        : Minecraft | guns.lol | "
              f"Discord (mode: {DISCORD_CHECK_MODE})")
        print(f"🧊 Proxy            : {'on' if PROXY_URL else 'off (direct)'}")
        print(f"⏳ User cooldown    : {USER_MAX_CHECKS} checks / "
              f"{USER_WINDOW_SECONDS:.0f}s")
        print(f"⚡ Response budget  : {RESPONSE_BUDGET_SECONDS:.2f}s "
              f"(reaction cap {REACTION_TIMEOUT:.2f}s)")
        print("=" * 58)

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
            # Reserve time for the reaction. All three checkers get this same
            # wall-clock cap, not three sequential caps.
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
                log.info("%-10s %-9s %-28s (%s)",
                         result.platform, result.status, result.detail, username)

        # 6. Translate normalized checker results into reactions.
        available = [result for result in results if result.available]
        if available:
            reaction_emojis = [result.emoji for result in available]
        else:
            statuses = {result.status for result in results}
            unknown = {checkers.ERROR, checkers.BLOCKED}
            if not statuses or statuses & unknown or statuses <= {checkers.SKIPPED}:
                # A partial outage does not prove the name is taken everywhere.
                # Surface the uncertainty rather than issuing a misleading ❌.
                reaction_emojis = [EMOJI_ALL_FAILED]
            else:
                reaction_emojis = [EMOJI_NONE_AVAILABLE]

        # 7. The member-visible answer is complete before optional logging.
        await self._react_all(message, reaction_emojis, deadline)

        # 8. Optional private log for genuine availability hits.
        if available and LOG_CHANNEL_ID:
            channel = self.get_channel(LOG_CHANNEL_ID)
            if channel is None:
                try:  # Not in cache yet? Ask Discord's API once.
                    channel = await self.fetch_channel(LOG_CHANNEL_ID)
                except discord.HTTPException:
                    channel = None
            if channel:
                names = ", ".join(
                    f"{result.platform} {result.emoji}" for result in available)
                try:
                    await channel.send(
                        f"🎯 `{username}` is FREE on: {names} "
                        f"(found by {message.author.mention})")
                except discord.HTTPException as exc:
                    log.warning("Could not write to log channel: %s", exc)


def main() -> None:
    """Validate user-supplied configuration before connecting to Discord."""

    if not TOKEN:
        raise SystemExit(
            "❌ DISCORD_TOKEN missing. Copy .env.example to .env and paste "
            "your bot token from the Discord Developer Portal.")
    if DISCORD_CHECK_MODE not in ("off", "probe"):
        raise SystemExit("❌ DISCORD_CHECK_MODE must be 'off' or 'probe'.")
    if PROXY_URL:
        proxy_error = checkers.validate_http_url(PROXY_URL, "PROXY_URL")
        if proxy_error:
            raise SystemExit(f"❌ {proxy_error}")
    if DISCORD_PROBE_TOKEN:
        if not checkers.is_valid_header_name(DISCORD_PROBE_TOKEN_HEADER):
            raise SystemExit("❌ DISCORD_PROBE_TOKEN_HEADER is not a valid HTTP header name.")
        if "\r" in DISCORD_PROBE_TOKEN or "\n" in DISCORD_PROBE_TOKEN:
            raise SystemExit("❌ DISCORD_PROBE_TOKEN must not contain a line break.")
        if "\r" in DISCORD_PROBE_TOKEN_SCHEME or "\n" in DISCORD_PROBE_TOKEN_SCHEME:
            raise SystemExit("❌ DISCORD_PROBE_TOKEN_SCHEME must not contain a line break.")
    if DISCORD_CHECK_MODE == "probe" and DISCORD_PROBE_URL:
        probe_error = checkers.validate_probe_url_template(DISCORD_PROBE_URL)
        if probe_error:
            raise SystemExit(f"❌ {probe_error}")
    SniperBot().run(TOKEN)


if __name__ == "__main__":
    main()
