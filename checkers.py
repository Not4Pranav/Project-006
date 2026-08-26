"""
Platform username-availability checkers for the Multi-Sniper Discord bot.

Each checker performs one HTTP request against the target platform and maps the
returned HTTP status code to one of the normalized statuses below:

    AVAILABLE  the platform says the name is free to register
    TAKEN      an active profile exists
    INVALID    the name can never be registered there (bad length / charset)
    BLOCKED    anti-bot wall or rate limit hit (Cloudflare, HTTP 429, ...) - unknown
    SKIPPED    checker disabled by configuration
    ERROR      network / timeout failure

Corrected endpoints (the AI Mode draft used malformed URLs like
"https://mojang.com{username}" - these are the real, verified ones):

    Minecraft : https://api.mojang.com/users/profiles/minecraft/<name>
                200 = taken (profile JSON returned)
                204 / 404 = no profile exists -> free
                (documented on the Mojang API page, minecraft.wiki)
    Guns.lol  : https://guns.lol/<name>
                200 = profile page exists -> taken
                404 = no such page -> free
                403 / 503 = Cloudflare bot wall -> unknown
    Discord   : there is NO public API to check arbitrary username
                availability. Disabled ("off") by default; an optional
                best-effort "probe" mode is kept for blueprint parity.

You can test every checker from your own machine without Discord:

    python checkers.py Notch
    python checkers.py zxqw99182vlt --proxy http://user:pass@host:port
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

# ---------------------------------------------------------------------------
# Normalized result statuses
# ---------------------------------------------------------------------------

AVAILABLE = "available"
TAKEN = "taken"
INVALID = "invalid"
BLOCKED = "blocked"      # anti-bot wall / rate limit - availability unknown
SKIPPED = "skipped"      # checker disabled in config
ERROR = "error"          # timeout / network failure

# Realistic browser headers so simple user-agent filters do not drop us.
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
# Input validation (why waste a request on a name that can never exist)
# ---------------------------------------------------------------------------

# Minecraft names: 3-16 chars, letters/digits/underscore only.
MINECRAFT_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,16}$")
# guns.lol usernames: alphanumeric (plus - and _), roughly 2-24 chars.
GUNSLOL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,24}$")
# Discord's new-style usernames: 2-32 chars, lowercase a-z 0-9 . _
DISCORD_PATTERN = re.compile(r"^[a-z0-9._]{2,32}$")

# A message must look like a bare username for the bot to pick it up.
USERNAME_MESSAGE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")


# ---------------------------------------------------------------------------
# Status-code interpreters (pure functions - trivially unit-testable)
# ---------------------------------------------------------------------------

def interpret_minecraft(status: int) -> str:
    if status == 200:
        return TAKEN           # profile JSON came back -> name claimed
    if status in (204, 404):   # 204 No Content / 404 Not Found -> no profile
        return AVAILABLE
    if status == 400:
        return INVALID         # name rejected by Mojang's own validation
    if status in (403, 405, 429):
        return BLOCKED         # Mojang's aggressive rate limiting / auth wall
    return ERROR


def interpret_gunslol(status: int) -> str:
    if status == 200:
        return TAKEN           # profile page rendered -> claimed
    if status in (404, 410):
        return AVAILABLE       # no landing page -> free to register
    if status in (403, 429, 503):
        return BLOCKED         # Cloudflare challenge / rate limit
    return ERROR


def interpret_discord_probe(status: int) -> str:
    # Best-effort semantics from the blueprint (NOT an official API).
    if status in (200, 401, 403):
        return TAKEN
    if status == 404:
        return AVAILABLE
    if status == 429:
        return BLOCKED
    return ERROR


# ---------------------------------------------------------------------------
# Async checkers
# ---------------------------------------------------------------------------

async def _fetch_status(
    session: aiohttp.ClientSession,
    url: str,
    proxy: Optional[str] = None,
) -> int:
    """GET a URL and return only its HTTP status code. Raises on network errors."""
    async with session.get(url, proxy=proxy, allow_redirects=False) as resp:
        return resp.status


async def check_minecraft(session, username, proxy=None) -> Result:
    """Minecraft (Mojang) - emoji 🕹️."""
    if not MINECRAFT_PATTERN.fullmatch(username):
        return Result("Minecraft", "\U0001F579\uFE0F", INVALID,
                      "name must be 3-16 chars of A-Z a-z 0-9 _")
    url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
    try:
        status = await _fetch_status(session, url, proxy)
        return Result("Minecraft", "\U0001F579\uFE0F",
                      interpret_minecraft(status), f"HTTP {status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return Result("Minecraft", "\U0001F579\uFE0F", ERROR, str(exc)[:120])


async def check_gunslol(session, username, proxy=None) -> Result:
    """guns.lol - emoji 🔫."""
    if not GUNSLOL_PATTERN.fullmatch(username):
        return Result("guns.lol", "\U0001F52B", INVALID,
                      "name must be 2-24 chars of A-Z a-z 0-9 - _")
    url = f"https://guns.lol/{username}"
    try:
        status = await _fetch_status(session, url, proxy)
        return Result("guns.lol", "\U0001F52B",
                      interpret_gunslol(status), f"HTTP {status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return Result("guns.lol", "\U0001F52B", ERROR, str(exc)[:120])


async def check_discord(
    session,
    username,
    proxy=None,
    mode: str = "off",
    probe_url: Optional[str] = None,
) -> Result:
    """Discord - emoji 🐈‍⬛.

    Discord exposes NO public endpoint for checking arbitrary username
    availability. mode="off" (default) skips the check. mode="probe" performs
    a best-effort GET against `probe_url` (default https://discord.com/<name>)
    using the blueprint's status mapping - treat its results with scepticism.
    """
    emoji = "\U0001F408\u200D\u2B1B"
    if mode == "off":
        return Result("Discord", emoji, SKIPPED, "check disabled (DISCORD_CHECK_MODE=off)")
    if not DISCORD_PATTERN.fullmatch(username):
        return Result("Discord", emoji, INVALID,
                      "Discord usernames are 2-32 chars, lowercase a-z 0-9 . _")
    url = (probe_url or f"https://discord.com/{username}").format(username=username)
    try:
        status = await _fetch_status(session, url, proxy)
        return Result("Discord", emoji,
                      interpret_discord_probe(status), f"HTTP {status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return Result("Discord", emoji, ERROR, str(exc)[:120])


async def run_all_checks(
    session: aiohttp.ClientSession,
    username: str,
    proxy: Optional[str] = None,
    discord_mode: str = "off",
    discord_probe_url: Optional[str] = None,
) -> list[Result]:
    """Fan out every platform check in parallel (asyncio.gather)."""
    return list(await asyncio.gather(
        check_minecraft(session, username, proxy),
        check_gunslol(session, username, proxy),
        check_discord(session, username, proxy, discord_mode, discord_probe_url),
    ))


# ---------------------------------------------------------------------------
# CLI self-test:  python checkers.py <username> [--proxy URL]
# ---------------------------------------------------------------------------

async def _cli() -> int:
    args = __import__("argparse").ArgumentParser(
        description="Test the platform checkers without running the bot.")
    args.add_argument("username", help="name to check, e.g. Notch")
    args.add_argument("--proxy", default=None, help="optional http(s) proxy URL")
    ns = args.parse_args()

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(headers=BROWSER_HEADERS, timeout=timeout) as session:
        results = await run_all_checks(session, ns.username, ns.proxy)

    icon = {AVAILABLE: "[FREE]  ", TAKEN: "[TAKEN]  ", INVALID: "[INVALID]",
            BLOCKED: "[BLOCKED]", SKIPPED: "[SKIP]  ", ERROR: "[ERROR] "}
    print(f"\nAvailability report for '{ns.username}':")
    print("-" * 62)
    for r in results:
        print(f"  {r.emoji} {r.platform:<10} {icon[r.status]} {r.detail}")
    print("-" * 62)
    emojis = [r.emoji for r in results if r.available]
    if emojis:
        verdict = " ".join(emojis)
    elif all(r.status in (ERROR, BLOCKED, SKIPPED) for r in results):
        verdict = "⚠️  (every check failed - network blocked or down)"
    else:
        verdict = "❌"
    print(f"  Bot would react: {verdict}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli()))
