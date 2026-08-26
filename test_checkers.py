"""
Offline tests for the Multi-Sniper checkers - no network needed.

Run with plain Python:     python test_checkers.py

Live tests (hit the REAL Mojang / guns.lol endpoints from your machine):
    LIVE=1 python test_checkers.py
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import checkers
from checkers import (
    AVAILABLE,
    BLOCKED,
    ERROR,
    INVALID,
    SKIPPED,
    TAKEN,
    interpret_discord_account_api,
    interpret_discord_dnsrobot,
    interpret_discord_probe,
    interpret_gunslol,
    interpret_minecraft,
)


def _session_with_status(status: int, body: str = ""):
    """Fake aiohttp session whose GET yields a status and small HTML body."""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=body)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=ctx)
    return session


def _session_with_json(status: int, payload):
    """Fake aiohttp session whose POST yields a JSON account-api response."""
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=payload)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
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

    def test_gunslol_200_page_semantics(self):
        # guns.lol may return 200 for its semantic "unclaimed" page.
        self.assertEqual(
            interpret_gunslol(200, "<h1>Username not found</h1>"), AVAILABLE)
        self.assertEqual(
            interpret_gunslol(200, "<title>Everything you want | guns.lol</title>"), AVAILABLE)
        self.assertEqual(
            interpret_gunslol(200, "<title>Just a moment...</title>"), BLOCKED)
        # Do not confuse a claimed profile's generic Discord-widget text with
        # the narrower availability marker above.
        self.assertEqual(
            interpret_gunslol(200, "<p>User Not Found on Discord</p>"), TAKEN)

    def test_discord_probe(self):
        self.assertEqual(interpret_discord_probe(200), TAKEN)
        self.assertEqual(interpret_discord_probe(404), AVAILABLE)
        self.assertEqual(interpret_discord_probe(401), BLOCKED)
        self.assertEqual(interpret_discord_probe(403), BLOCKED)
        self.assertEqual(interpret_discord_probe(429), BLOCKED)

    def test_discord_account_api(self):
        self.assertEqual(interpret_discord_account_api(200, {"taken": True}), TAKEN)
        self.assertEqual(interpret_discord_account_api(200, {"taken": False}), AVAILABLE)
        self.assertEqual(interpret_discord_account_api(200, {"available": True}), AVAILABLE)
        self.assertEqual(interpret_discord_account_api(200, {"available": False}), TAKEN)
        self.assertEqual(interpret_discord_account_api(
            200, {"taken": False, "available": True}), AVAILABLE)
        self.assertEqual(interpret_discord_account_api(
            200, {"taken": False, "available": False}), ERROR)
        self.assertEqual(interpret_discord_account_api(
            200, {"taken": "false"}), ERROR)
        self.assertEqual(interpret_discord_account_api(
            200, {"data": {"check": {"status": 2}}}), AVAILABLE)
        self.assertEqual(interpret_discord_account_api(
            200, {"data": {"check": {"status": 3}}}), TAKEN)
        self.assertEqual(interpret_discord_account_api(
            200, {"data": {"check": {"status": False}}}), ERROR)
        self.assertEqual(interpret_discord_account_api(
            200, {"data": {"check": {"status": 2.0}}}), ERROR)
        self.assertEqual(interpret_discord_account_api(403, {"taken": False}), BLOCKED)
        self.assertEqual(interpret_discord_account_api(200, {}), ERROR)

    def test_discord_dnsrobot_uses_the_same_strict_browser_contract(self):
        self.assertEqual(interpret_discord_dnsrobot(200, {"taken": False}), AVAILABLE)
        self.assertEqual(interpret_discord_dnsrobot(200, {"taken": True}), TAKEN)
        self.assertEqual(interpret_discord_dnsrobot(403, {"taken": False}), BLOCKED)
        self.assertEqual(interpret_discord_dnsrobot(200, {"status": "available"}), ERROR)


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
        self.assertIsNotNone(checkers.GUNSLOL_PATTERN.fullmatch("id.search"))
        self.assertIsNone(checkers.DISCORD_PATTERN.fullmatch("Notch"))      # uppercase
        self.assertIsNotNone(checkers.DISCORD_PATTERN.fullmatch("notch.dev"))

    def test_user_supplied_endpoint_validation(self):
        self.assertIsNone(checkers.validate_http_url("https://proxy.example:8443",
                                                     "PROXY_URL"))
        self.assertIsNotNone(checkers.validate_http_url("socks5://proxy.example",
                                                        "PROXY_URL"))
        self.assertIn("port", checkers.validate_http_url(
            "https://proxy.example:not-a-port", "PROXY_URL") or "")
        self.assertIn("valid URL", checkers.validate_http_url(
            "https://[invalid", "PROXY_URL") or "")
        self.assertIsNone(checkers.validate_probe_url_template(
            "https://checker.example/lookup/{username}"))
        self.assertIn("placeholder", checkers.validate_probe_url_template(
            "https://checker.example/lookup") or "")
        self.assertIsNone(checkers.validate_account_api_url(
            "https://discord.example/api/account"))
        self.assertIn("JSON body", checkers.validate_account_api_url(
            "https://discord.example/{username}") or "")
        self.assertTrue(checkers.is_valid_header_name("X-API-Key"))
        self.assertFalse(checkers.is_valid_header_name("Bad\nHeader"))

    def test_sensitive_error_text_is_redacted(self):
        detail = checkers._redact_sensitive_text(
            "proxy=http://person:secret@proxy.example?token=private-value "
            "Authorization: Bearer private-header-value")
        self.assertNotIn("secret", detail)
        self.assertNotIn("private-value", detail)
        self.assertNotIn("private-header-value", detail)
        self.assertIn("***", detail)
        self.assertNotIn("\n", checkers._redact_sensitive_text("remote\nerror"))


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

    def test_gunslol_unclaimed_200_page(self):
        r = self.run_async(checkers.check_gunslol(
            _session_with_status(200, "<h1>Username not found</h1>"),
            "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertIn("unclaimed page", r.detail)

    def test_discord_off_by_default(self):
        r = self.run_async(checkers.check_discord(_session_with_status(200), "vortex", mode="off"))
        self.assertEqual(r.status, SKIPPED)

    def test_discord_probe(self):
        session = _session_with_status(404)
        headers = {"X-Checker-Token": "not-a-real-secret"}
        r = self.run_async(checkers.check_discord(
            session, "vortex", mode="probe",
            probe_url="https://checker.example/{username}",
            probe_headers=headers))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(session.get.call_args.kwargs["headers"], headers)

    def test_discord_account_api_posts_username_and_reads_taken(self):
        session = _session_with_json(200, {"taken": False})
        headers = {"Authorization": "Bearer oauth-token"}
        r = self.run_async(checkers.check_discord(
            session, "vortex", mode="account",
            account_api_url="https://discord.example/api/account/username",
            account_api_headers=headers))
        self.assertEqual(r.status, AVAILABLE)
        session.post.assert_called_once_with(
            "https://discord.example/api/account/username",
            json={"username": "vortex"},
            proxy=None,
            headers=headers,
        )

    def test_discord_account_api_uses_first_party_default(self):
        session = _session_with_json(200, {"taken": True})
        r = self.run_async(checkers.check_discord_account_api(session, "vortex"))
        self.assertEqual(r.status, TAKEN)
        self.assertEqual(
            session.post.call_args.args[0],
            checkers.DEFAULT_DISCORD_ACCOUNT_API_URL,
        )

    def test_discord_dnsrobot_mirrors_fast_browser_request_without_credentials(self):
        session = _session_with_json(200, {"taken": False})
        r = self.run_async(checkers.check_discord(
            session, "Vortex", mode="dnsrobot",
            account_api_headers={"Authorization": "Bearer must-not-forward"},
            probe_headers={"X-Checker-Token": "must-not-forward"}))
        self.assertEqual(r.status, AVAILABLE)
        expected_headers = {
            **checkers.DNSROBOT_BROWSER_HEADERS,
            "Referer": checkers.dnsrobot_username_checker_url("Vortex"),
        }
        session.post.assert_called_once_with(
            checkers.DEFAULT_DISCORD_DNSROBOT_API_URL,
            json={"username": "vortex"},
            proxy=None,
            headers=expected_headers,
        )
        self.assertNotIn("Authorization", session.post.call_args.kwargs["headers"])
        self.assertEqual(
            checkers.dnsrobot_username_checker_url("a.b"),
            "https://dnsrobot.net/username-checker?u=a.b",
        )

    def test_discord_dnsrobot_block_is_unknown(self):
        r = self.run_async(checkers.check_discord_dnsrobot(
            _session_with_json(403, {"message": "Forbidden"}), "vortex"))
        self.assertEqual(r.status, BLOCKED)

    def test_discord_account_api_rejects_bad_url_without_request(self):
        session = _session_with_json(200, {"taken": False})
        r = self.run_async(checkers.check_discord_account_api(
            session, "vortex", api_url="file:///tmp/account"))
        self.assertEqual(r.status, ERROR)
        session.post.assert_not_called()

        r = self.run_async(checkers.check_discord_account_api(
            session, "vortex", api_url="https://checker.example/{username}"))
        self.assertEqual(r.status, ERROR)
        session.post.assert_not_called()

    def test_discord_account_api_malformed_success_is_unknown(self):
        r = self.run_async(checkers.check_discord_account_api(
            _session_with_json(200, {"message": "ok"}), "vortex"))
        self.assertEqual(r.status, ERROR)

    def test_discord_probe_rejects_bad_template_without_request(self):
        session = _session_with_status(404)
        r = self.run_async(checkers.check_discord(
            session, "vortex", mode="probe",
            probe_url="file:///tmp/{username}"))
        self.assertEqual(r.status, ERROR)
        session.get.assert_not_called()

    def test_discord_probe_requires_explicit_url(self):
        r = self.run_async(checkers.check_discord(
            _session_with_status(404), "vortex", mode="probe"))
        self.assertEqual(r.status, SKIPPED)

    def test_account_api_mode_alias_uses_json_post(self):
        session = _session_with_json(200, {"taken": True})
        r = self.run_async(checkers.check_discord(
            session, "vortex", mode="account_api",
            account_api_url="https://discord.example/api/account"))
        self.assertEqual(r.status, TAKEN)
        session.post.assert_called_once()

    def test_network_error_handled(self):
        broken = MagicMock()
        broken.get = MagicMock(side_effect=checkers.aiohttp.ClientError("boom"))
        r = self.run_async(checkers.check_minecraft(broken, "Notch"))
        self.assertEqual(r.status, ERROR)
        self.assertFalse(r.available)

    def test_parallel_run_all(self):
        results = self.run_async(checkers.run_all_checks(
            _session_with_status(404), "zxqw99182", discord_mode="probe",
            discord_probe_url="https://checker.example/{username}"))
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.available for r in results))

    def test_probe_token_is_sent_only_to_external_checker(self):
        session = _session_with_status(404)
        headers = {"Authorization": "Bearer not-a-real-secret"}
        self.run_async(checkers.run_all_checks(
            session, "zxqw99182", discord_mode="probe",
            discord_probe_url="https://checker.example/{username}",
            discord_probe_headers=headers))

        checker_calls = []
        platform_calls = []
        for call in session.get.call_args_list:
            url = call.args[0]
            (checker_calls if "checker.example" in url else platform_calls).append(call)
        self.assertEqual(len(checker_calls), 1)
        self.assertEqual(checker_calls[0].kwargs["headers"], headers)
        for call in platform_calls:
            self.assertNotEqual(call.kwargs.get("headers"), headers)

    def test_shared_deadline_returns_honest_error_results(self):
        async def slow_checker(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return checkers.Result("unexpected", "?", AVAILABLE)

        with patch.object(checkers, "check_minecraft", slow_checker), \
             patch.object(checkers, "check_gunslol", slow_checker), \
             patch.object(checkers, "check_discord", slow_checker):
            results = self.run_async(checkers.run_all_checks(
                _session_with_status(404), "zxqw99182", timeout=0.001))

        self.assertEqual([r.status for r in results], [ERROR, ERROR, ERROR])
        self.assertTrue(all("deadline" in r.detail for r in results))


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
