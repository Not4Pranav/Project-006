"""
Offline tests for the Multi-Sniper checkers - no network needed.

Run with plain Python:     python test_checkers.py

Live tests (hit the REAL Mojang / guns.lol endpoints from your machine):
    LIVE=1 python test_checkers.py
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock

import aiohttp

import checkers
from checkers import (AVAILABLE, BLOCKED, ERROR, INVALID, SKIPPED, TAKEN,
                      interpret_discord_probe, interpret_gunslol,
                      interpret_minecraft)


def _session_with_status(status: int):
    """Fake aiohttp session whose GET always yields the given status."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock(status=status))
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


class TestInterpreters(unittest.TestCase):
    def test_minecraft(self):
        self.assertEqual(interpret_minecraft(200), TAKEN)      # profile exists
        self.assertEqual(interpret_minecraft(204), AVAILABLE)  # no content
        self.assertEqual(interpret_minecraft(404), AVAILABLE)  # no profile
        self.assertEqual(interpret_minecraft(400), INVALID)    # bad name
        self.assertEqual(interpret_minecraft(429), BLOCKED)    # rate limited
        self.assertEqual(interpret_minecraft(500), ERROR)

    def test_gunslol(self):
        self.assertEqual(interpret_gunslol(200), TAKEN)
        self.assertEqual(interpret_gunslol(404), AVAILABLE)
        self.assertEqual(interpret_gunslol(410), AVAILABLE)
        self.assertEqual(interpret_gunslol(403), BLOCKED)      # Cloudflare
        self.assertEqual(interpret_gunslol(503), BLOCKED)
        self.assertEqual(interpret_gunslol(418), ERROR)

    def test_discord_probe(self):
        self.assertEqual(interpret_discord_probe(200), TAKEN)
        self.assertEqual(interpret_discord_probe(401), TAKEN)
        self.assertEqual(interpret_discord_probe(404), AVAILABLE)
        self.assertEqual(interpret_discord_probe(429), BLOCKED)


class TestValidators(unittest.TestCase):
    def test_message_pattern(self):
        good = ["Notch", "zxqw_99182", "abc", "a.b_c-d"]
        bad = ["", "two words", "hello world", "way" + "too" * 40 + "long",
               "<@123>", "check this", "#general"]
        for g in good:
            self.assertIsNotNone(
                checkers.USERNAME_MESSAGE_PATTERN.fullmatch(g), g)
        for b in bad:
            self.assertIsNone(
                checkers.USERNAME_MESSAGE_PATTERN.fullmatch(b), b)

    def test_platform_patterns(self):
        self.assertIsNotNone(checkers.MINECRAFT_PATTERN.fullmatch("Notch"))
        self.assertIsNone(checkers.MINECRAFT_PATTERN.fullmatch("ab"))       # <3
        self.assertIsNone(checkers.MINECRAFT_PATTERN.fullmatch("x" * 17))   # >16
        self.assertIsNone(checkers.MINECRAFT_PATTERN.fullmatch("bad name"))
        self.assertIsNone(checkers.DISCORD_PATTERN.fullmatch("Notch"))      # uppercase
        self.assertIsNotNone(checkers.DISCORD_PATTERN.fullmatch("notch.dev"))


class TestCheckers(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_minecraft_taken(self):
        r = self.run_async(checkers.check_minecraft(_session_with_status(200), "Notch"))
        self.assertEqual(r.status, TAKEN)
        self.assertFalse(r.available)

    def test_minecraft_free(self):
        r = self.run_async(checkers.check_minecraft(_session_with_status(404), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertTrue(r.available)
        self.assertEqual(r.emoji, "\U0001F579\uFE0F")

    def test_minecraft_invalid_name_short_circuits(self):
        r = self.run_async(checkers.check_minecraft(_session_with_status(200), "ab"))
        self.assertEqual(r.status, INVALID)  # no request should matter

    def test_gunslol_free(self):
        r = self.run_async(checkers.check_gunslol(_session_with_status(404), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(r.emoji, "\U0001F52B")

    def test_gunslol_cloudflare_block(self):
        r = self.run_async(checkers.check_gunslol(_session_with_status(403), "zxqw99182"))
        self.assertEqual(r.status, BLOCKED)

    def test_discord_off_by_default(self):
        r = self.run_async(checkers.check_discord(_session_with_status(200), "vortex", mode="off"))
        self.assertEqual(r.status, SKIPPED)

    def test_discord_probe(self):
        r = self.run_async(checkers.check_discord(_session_with_status(404), "vortex", mode="probe"))
        self.assertEqual(r.status, AVAILABLE)

    def test_network_error_handled(self):
        broken = MagicMock()
        broken.get = MagicMock(side_effect=checkers.aiohttp.ClientError("boom"))
        r = self.run_async(checkers.check_minecraft(broken, "Notch"))
        self.assertEqual(r.status, ERROR)
        self.assertFalse(r.available)

    def test_parallel_run_all(self):
        results = self.run_async(checkers.run_all_checks(
            _session_with_status(404), "zxqw99182", discord_mode="probe"))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.available for r in results))


class TestLiveNetwork(unittest.TestCase):
    """REAL network tests - opt in with LIVE=1 (e.g. from your machine)."""

    def _check(self, coro):
        async def runner():
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(
                    headers=checkers.BROWSER_HEADERS,
                    timeout=timeout) as session:
                return await coro(session)
        return asyncio.run(runner())

    @unittest.skipUnless(os.getenv("LIVE") == "1", "set LIVE=1 to run")
    def test_live_minecraft(self):
        taken = self._check(lambda s: checkers.check_minecraft(s, "Notch"))
        free = self._check(
            lambda s: checkers.check_minecraft(s, "zxqw7k3vlt9m42q"))
        self.assertEqual(taken.status, TAKEN, taken.detail)
        self.assertEqual(free.status, AVAILABLE, free.detail)

    @unittest.skipUnless(os.getenv("LIVE") == "1", "set LIVE=1 to run")
    def test_live_gunslol(self):
        free = self._check(
            lambda s: checkers.check_gunslol(s, "zxqw7k3vlt9m42q"))
        # Cloudflare may wall datacenter IPs, so FREE or BLOCKED are both sane
        self.assertIn(free.status, (AVAILABLE, BLOCKED), free.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
