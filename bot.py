"""
Multi-Sniper - a Discord username availability checker.

How it behaves
--------------
When a member posts a bare username in the watched channel, the bot checks
Minecraft, guns.lol and (optionally) Discord IN PARALLEL and reacts to the
message with one emoji per platform where the name is FREE:

    🕹️  Minecraft   free (Mojang found no profile)
    🔫  guns.lol    free (no profile page at guns.lol/<name>)
    🐈‍⬛ Discord     free (best-effort probe only - no public API exists)
    ❌  fallback    name is not available on any checked platform
    ⚠️  fallback    every check failed (network down / blocked)
    ⏳  cooldown    user is checking too fast

Configuration lives in .env (see .env.example):
    DISCORD_TOKEN         bot token from the Discord Developer Portal
    TARGET_CHANNEL_ID     channel to watch (blank = every channel)
    LOG_CHANNEL_ID        optional channel to log "available" hits
    PROXY_URL             optional HTTP(S) proxy for outbound checks
    DISCORD_CHECK_MODE    off (default) | probe
    CHECK_TIMEOUT         per-request timeout seconds   (default 3)
    USER_MAX_CHECKS       checks per user per window    (default 3)
    USER_WINDOW_SECONDS   cooldown window seconds       (default 60)
    RESULT_CACHE_TTL      cache repeat lookups seconds  (default 300)

Run locally:   python bot.py
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque
from typing import Optional

import aiohttp
import discord
from dotenv import load_dotenv

import checkers

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()


def _opt_int(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw.isdigit() else None


def _opt_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


TARGET_CHANNEL_ID = _opt_int("TARGET_CHANNEL_ID")
LOG_CHANNEL_ID = _opt_int("LOG_CHANNEL_ID")
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
DISCORD_CHECK_MODE = os.getenv("DISCORD_CHECK_MODE", "off").strip().lower()
DISCORD_PROBE_URL = os.getenv("DISCORD_PROBE_URL", "").strip() or None
CHECK_TIMEOUT = _opt_float("CHECK_TIMEOUT", 3.0)
USER_MAX_CHECKS = max(int(_opt_float("USER_MAX_CHECKS", 3)), 1)
USER_WINDOW_SECONDS = _opt_float("USER_WINDOW_SECONDS", 60)
RESULT_CACHE_TTL = _opt_float("RESULT_CACHE_TTL", 300)

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
    """discord.py client that owns one shared aiohttp session for checks."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # needs the Dev Portal toggle!
        super().__init__(intents=intents)
        self.http_sniper: Optional[aiohttp.ClientSession] = None
        # Per-user token bucket: {user_id: deque[timestamps]}
        self._buckets: dict[int, deque[float]] = defaultdict(deque)
        # Recent results cache: {username_lower: (timestamp, [Result, ...])}
        self._cache: dict[str, tuple[float, list[checkers.Result]]] = {}

    async def setup_hook(self) -> None:
        """Runs once after login, BEFORE any gateway event is dispatched."""
        timeout = aiohttp.ClientTimeout(total=CHECK_TIMEOUT)
        self.http_sniper = aiohttp.ClientSession(
            headers=checkers.BROWSER_HEADERS, timeout=timeout)

    async def close(self) -> None:
        if self.http_sniper:
            await self.http_sniper.close()
        await super().close()

    # -- helpers ------------------------------------------------------------

    def _cooldown_hit(self, user_id: int) -> bool:
        """True if the user exhausted their checks in the current window."""
        now = time.monotonic()
        bucket = self._buckets[user_id]
        while bucket and now - bucket[0] > USER_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= USER_MAX_CHECKS:
            return True
        bucket.append(now)
        return False

    def _cached(self, username: str) -> Optional[list[checkers.Result]]:
        hit = self._cache.get(username.lower())
        if hit and time.monotonic() - hit[0] < RESULT_CACHE_TTL:
            return hit[1]
        return None

    async def _react(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.Forbidden:
            log.warning("Missing 'Add Reactions' permission in #%s",
                        message.channel)
        except discord.HTTPException as exc:
            log.warning("Reaction %r failed: %s", emoji, exc)

    # -- events -------------------------------------------------------------

    async def on_ready(self) -> None:
        print("=" * 58)
        print(f"🟢 MULTI-SNIPER ONLINE as {self.user}")
        print(f"🔒 Watching channel : "
              f"{TARGET_CHANNEL_ID if TARGET_CHANNEL_ID else 'ALL CHANNELS'}")
        print(f"🕹️ Platforms        : Minecraft | guns.lol | "
              f"Discord (mode: {DISCORD_CHECK_MODE})")
        print(f"🧊 Proxy            : {'on' if PROXY_URL else 'off (direct)'}")
        print(f"⏳ User cooldown    : {USER_MAX_CHECKS} checks / "
              f"{USER_WINDOW_SECONDS:.0f}s")
        print("=" * 58)

    async def on_message(self, message: discord.Message) -> None:
        """Core pipeline: filter -> cooldown -> parallel checks -> reactions."""
        # 1. Never react to bots (prevents loops with ourselves and others).
        if message.author.bot:
            return

        # 2. Only the configured channel, if one was set.
        if TARGET_CHANNEL_ID and message.channel.id != TARGET_CHANNEL_ID:
            return

        # 3. The payload must be a single bare username-looking token.
        username = message.content.strip()
        if not checkers.USERNAME_MESSAGE_PATTERN.fullmatch(username):
            return

        # 4. Cooldown guard - protects your IP from platform rate limits.
        if self._cooldown_hit(message.author.id):
            await self._react(message, EMOJI_COOLDOWN)
            return

        # 5. Serve repeat lookups from cache when possible.
        cached = self._cached(username)
        if cached is not None:
            results = cached
            log.info("cache hit for %r", username)
        else:
            # 6. Fan out all three platform checks SIMULTANEOUSLY.
            results = await checkers.run_all_checks(
                self.http_sniper, username,
                proxy=PROXY_URL,
                discord_mode=DISCORD_CHECK_MODE,
                discord_probe_url=DISCORD_PROBE_URL,
            )
            self._cache[username.lower()] = (time.monotonic(), results)
            for r in results:
                log.info("%-10s %-9s %-14s (%s)",
                         r.platform, r.status, r.detail, username)

        # 7. React with the emoji of every platform where the name is FREE.
        available = [r for r in results if r.available]
        for r in available:
            await self._react(message, r.emoji)

        # 8. Fallback reactions.
        if not available:
            statuses = {r.status for r in results}
            if statuses <= {checkers.ERROR, checkers.BLOCKED, checkers.SKIPPED}:
                # Nothing definitive came back - signal trouble, not "taken".
                await self._react(message, EMOJI_ALL_FAILED)
            else:
                await self._react(message, EMOJI_NONE_AVAILABLE)

        # 9. Optional: log hits to a private channel.
        if available and LOG_CHANNEL_ID:
            channel = self.get_channel(LOG_CHANNEL_ID)
            if channel:
                names = ", ".join(f"{r.platform} {r.emoji}" for r in available)
                try:
                    await channel.send(
                        f"🎯 `{username}` is FREE on: {names} "
                        f"(found by {message.author.mention})")
                except discord.HTTPException as exc:
                    log.warning("Could not write to log channel: %s", exc)


def main() -> None:
    if not TOKEN or TOKEN == "your_bot_token_here":
        raise SystemExit(
            "❌ DISCORD_TOKEN missing. Copy .env.example to .env and paste "
            "your bot token from the Discord Developer Portal.")
    if DISCORD_CHECK_MODE not in ("off", "probe"):
        raise SystemExit("❌ DISCORD_CHECK_MODE must be 'off' or 'probe'.")
    SniperBot().run(TOKEN)


if __name__ == "__main__":
    main()
