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
    interpret_discord_dnsrobot_page,
    interpret_discord_probe,
    interpret_github,
    interpret_gunslol,
    interpret_instagram,
    interpret_minecraft,
    interpret_reddit,
    interpret_steam,
    interpret_twitter,
)


def _session_with_status(status: int, body: str = ""):
    """Fake aiohttp session whose GET yields a status and small HTML body."""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=body)
    response.json = AsyncMock(
        return_value={"id": "069a79f444e94726a5befca90e38aaf5", "name": "Notch"}
        if status == 200 else {})
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


def _browser_with_status(status):
    """Fake Playwright browser whose rendered Discord card has ``status``."""
    page = MagicMock()
    page.url = "https://dnsrobot.net/username-checker?u=vortex"
    page.goto = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.evaluate = AsyncMock(return_value=status)
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser, page, context


class TestInterpreters(unittest.TestCase):
    def test_minecraft(self):
        self.assertEqual(interpret_minecraft(200), TAKEN)      # profile exists
        self.assertEqual(interpret_minecraft(204), AVAILABLE)  # no content
        self.assertEqual(interpret_minecraft(404), AVAILABLE)  # no profile
        self.assertEqual(interpret_minecraft(400), INVALID)    # bad name
        self.assertEqual(interpret_minecraft(429), BLOCKED)    # rate limited
        self.assertEqual(interpret_minecraft(500), ERROR)
        self.assertEqual(interpret_minecraft(200, {"id": "uuid", "name": "Notch"}), TAKEN)
        self.assertEqual(interpret_minecraft(200, {"message": "challenge"}), BLOCKED)
        self.assertEqual(interpret_minecraft(200, {"id": "", "name": "Notch"}), BLOCKED)

    def test_gunslol(self):
        self.assertEqual(interpret_gunslol(200), TAKEN)
        self.assertEqual(interpret_gunslol(200, ""), BLOCKED)
        self.assertEqual(interpret_gunslol(200, 123), BLOCKED)
        self.assertEqual(interpret_gunslol(404), AVAILABLE)
        self.assertEqual(interpret_gunslol(410), AVAILABLE)
        self.assertEqual(interpret_gunslol(403), BLOCKED)      # Cloudflare
        self.assertEqual(interpret_gunslol(503), BLOCKED)
        self.assertEqual(interpret_gunslol(418), ERROR)

    def test_gunslol_200_page_semantics(self):
        self.assertEqual(
            interpret_gunslol(200, "<h1>Username not found</h1>"), AVAILABLE)
        self.assertEqual(
            interpret_gunslol(200, "<title>Everything you want | guns.lol</title>"), AVAILABLE)
        self.assertEqual(
            interpret_gunslol(200, "<title>Just a moment...</title>"), BLOCKED)
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

    def test_discord_dnsrobot_page_is_strict(self):
        self.assertEqual(interpret_discord_dnsrobot_page("Available"), AVAILABLE)
        self.assertEqual(interpret_discord_dnsrobot_page("Taken"), TAKEN)
        self.assertEqual(interpret_discord_dnsrobot_page("Rate limited"), BLOCKED)
        self.assertEqual(interpret_discord_dnsrobot_page("Unknown"), BLOCKED)
        self.assertEqual(interpret_discord_dnsrobot_page("Pending"), BLOCKED)
        self.assertEqual(interpret_discord_dnsrobot_page("Unexpected"), ERROR)
        self.assertEqual(interpret_discord_dnsrobot_page(None), ERROR)

    def test_discord_dnsrobot_network_contract_remains_strict_for_diagnostics(self):
        self.assertEqual(interpret_discord_dnsrobot(200, {"taken": False}), AVAILABLE)
        self.assertEqual(interpret_discord_dnsrobot(200, {"taken": True}), TAKEN)
        self.assertEqual(interpret_discord_dnsrobot(403, {"taken": False}), BLOCKED)
        self.assertEqual(interpret_discord_dnsrobot(200, {"status": "available"}), ERROR)

    def test_github(self):
        # 200 with login = taken
        self.assertEqual(interpret_github(200, {"login": "octocat"}), TAKEN)
        # 200 without valid login = blocked
        self.assertEqual(interpret_github(200, {"message": "rate limit"}), BLOCKED)
        self.assertEqual(interpret_github(200, None), TAKEN)
        # 404 = available
        self.assertEqual(interpret_github(404), AVAILABLE)
        # Rate limited
        self.assertEqual(interpret_github(403), BLOCKED)
        self.assertEqual(interpret_github(429), BLOCKED)
        # Other
        self.assertEqual(interpret_github(500), ERROR)

    def test_steam(self):
        # 200 with normal profile = taken
        self.assertEqual(interpret_steam(200, "<html>profile content</html>"), TAKEN)
        # 200 with "profile not found" = available
        self.assertEqual(interpret_steam(200, "The specified profile could not be found"), AVAILABLE)
        # 200 with empty body = blocked
        self.assertEqual(interpret_steam(200, ""), BLOCKED)
        # 200 with challenge = blocked
        self.assertEqual(interpret_steam(200, "Just a moment..."), BLOCKED)
        # 404 = available
        self.assertEqual(interpret_steam(404), AVAILABLE)
        # Rate limited
        self.assertEqual(interpret_steam(403), BLOCKED)
        self.assertEqual(interpret_steam(429), BLOCKED)
        self.assertEqual(interpret_steam(503), BLOCKED)
        # Other
        self.assertEqual(interpret_steam(500), ERROR)

    def test_reddit(self):
        # 200 with user data = taken
        self.assertEqual(interpret_reddit(200, {"data": {"name": "octocat"}}), TAKEN)
        # 200 without data = blocked
        self.assertEqual(interpret_reddit(200, {"message": "blocked"}), BLOCKED)
        self.assertEqual(interpret_reddit(200, None), BLOCKED)
        # 404 = available
        self.assertEqual(interpret_reddit(404), AVAILABLE)
        # Rate limited
        self.assertEqual(interpret_reddit(403), BLOCKED)
        self.assertEqual(interpret_reddit(429), BLOCKED)
        self.assertEqual(interpret_reddit(503), BLOCKED)
        # Other
        self.assertEqual(interpret_reddit(500), ERROR)

    def test_instagram(self):
        # 200 with normal page = taken
        self.assertEqual(interpret_instagram(200, "<html>profile page</html>"), TAKEN)
        # 200 with "not available" = available
        self.assertEqual(interpret_instagram(200, "Sorry, this page isn't available"), AVAILABLE)
        # 200 with login wall = blocked
        self.assertEqual(interpret_instagram(200, "Login to Instagram"), BLOCKED)
        self.assertEqual(
            interpret_instagram(200, "redirecting to /challenge/ required"), BLOCKED)
        # 200 with empty body = blocked
        self.assertEqual(interpret_instagram(200, ""), BLOCKED)
        # 404 = available
        self.assertEqual(interpret_instagram(404), AVAILABLE)
        # Redirect = blocked (login wall)
        self.assertEqual(interpret_instagram(302), BLOCKED)
        # Auth wall
        self.assertEqual(interpret_instagram(401), BLOCKED)
        self.assertEqual(interpret_instagram(403), BLOCKED)
        self.assertEqual(interpret_instagram(429), BLOCKED)
        # Other
        self.assertEqual(interpret_instagram(500), ERROR)

    def test_instagram_typographic_apostrophe(self):
        """Instagram serves a curly apostrophe; the ASCII marker must still hit."""
        page = "Sorry, this page isn\u2019t available."
        self.assertEqual(interpret_instagram(200, page), AVAILABLE)

    def test_instagram_bio_word_does_not_block(self):
        """A live profile that merely mentions 'challenge' is TAKEN, not BLOCKED."""
        page = "<html>bio: I love a good challenge and captcha puzzles</html>"
        self.assertEqual(interpret_instagram(200, page), TAKEN)

    def test_twitter_typographic_apostrophe(self):
        page = "This account doesn\u2019t exist"
        self.assertEqual(interpret_twitter(200, page), AVAILABLE)

    def test_twitter_bio_word_does_not_block(self):
        page = "<html>class='challenge-card' bio: captcha enjoyer</html>"
        self.assertEqual(interpret_twitter(200, page), TAKEN)

    def test_twitter(self):
        # 200 with normal page = taken
        self.assertEqual(interpret_twitter(200, "<html>profile content</html>"), TAKEN)
        # 200 with "doesn't exist" = available
        self.assertEqual(interpret_twitter(200, "This account doesn't exist"), AVAILABLE)
        self.assertEqual(interpret_twitter(200, "This user doesn't exist"), AVAILABLE)
        self.assertEqual(interpret_twitter(200, "Hmm...this page doesn't exist"), AVAILABLE)
        # 200 with challenge = blocked
        self.assertEqual(interpret_twitter(200, "Rate limit exceeded"), BLOCKED)
        self.assertEqual(interpret_twitter(200, "Solve this captcha"), BLOCKED)
        # 200 with empty body = blocked
        self.assertEqual(interpret_twitter(200, ""), BLOCKED)
        # 404 = available
        self.assertEqual(interpret_twitter(404), AVAILABLE)
        # Rate limited
        self.assertEqual(interpret_twitter(403), BLOCKED)
        self.assertEqual(interpret_twitter(429), BLOCKED)
        # Other
        self.assertEqual(interpret_twitter(500), ERROR)


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
        # GitHub pattern
        self.assertIsNotNone(checkers.GITHUB_PATTERN.fullmatch("octocat"))
        self.assertIsNotNone(checkers.GITHUB_PATTERN.fullmatch("a"))
        self.assertIsNone(checkers.GITHUB_PATTERN.fullmatch("-bad"))         # starts with -
        self.assertIsNone(checkers.GITHUB_PATTERN.fullmatch("bad-"))         # ends with -
        # Steam pattern
        self.assertIsNotNone(checkers.STEAM_PATTERN.fullmatch("gabelogannewell"))
        # Reddit pattern
        self.assertIsNotNone(checkers.REDDIT_PATTERN.fullmatch("spez"))
        self.assertIsNone(checkers.REDDIT_PATTERN.fullmatch("ab"))          # <3
        # Instagram pattern
        self.assertIsNotNone(checkers.INSTAGRAM_PATTERN.fullmatch("kevin"))
        self.assertIsNotNone(checkers.INSTAGRAM_PATTERN.fullmatch("user.name"))
        # Twitter pattern
        self.assertIsNotNone(checkers.TWITTER_PATTERN.fullmatch("jack"))
        self.assertIsNone(checkers.TWITTER_PATTERN.fullmatch("a" * 16))     # >15

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
        self.assertIn("https://", checkers.validate_probe_url_template(
            "http://checker.example/lookup/{username}") or "")
        self.assertIn("placeholder", checkers.validate_probe_url_template(
            "https://checker.example/lookup") or "")
        self.assertIsNone(checkers.validate_account_api_url(
            "https://discord.example/api/account"))
        self.assertIn("https://", checkers.validate_account_api_url(
            "http://discord.example/api/account") or "")
        self.assertIn("JSON body", checkers.validate_account_api_url(
            "https://discord.example/{username}") or "")
        self.assertTrue(checkers.is_valid_header_name("X-API-Key"))
        self.assertFalse(checkers.is_valid_header_name("Bad\nHeader"))
        self.assertEqual(
            checkers.playwright_proxy_config("http://proxy-user:proxy-pass@proxy.example:8080"),
            {
                "server": "http://proxy.example:8080",
                "username": "proxy-user",
                "password": "proxy-pass",
            },
        )
        self.assertIn("path", checkers.validate_proxy_url(
            "http://proxy.example/route") or "")

    def test_sensitive_error_text_is_redacted(self):
        msg = checkers._redact_sensitive_text(
            "failed http://user:pass@proxy.example/path?token=secret")
        self.assertNotIn("pass", msg)
        self.assertNotIn("secret", msg)
        self.assertIn("***", msg)
        msg2 = checkers._redact_sensitive_text(
            aiohttp.ClientError("Authorization: Bearer eyJhbGciOi.test.sig"))
        self.assertNotIn("eyJhbGciOi", msg2)
        self.assertIn("***", msg2)
        self.assertLessEqual(len(msg2), 120)


class TestCheckers(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_minecraft_taken(self):
        r = self.run_async(checkers.check_minecraft(_session_with_status(200), "Notch"))
        self.assertEqual(r.status, TAKEN)
        self.assertEqual(r.emoji, "\U0001F579\uFE0F")

    def test_minecraft_free(self):
        r = self.run_async(checkers.check_minecraft(_session_with_status(404), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)

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
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

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
            allow_redirects=False,
        )

    def test_discord_account_api_uses_first_party_default(self):
        session = _session_with_json(200, {"taken": True})
        r = self.run_async(checkers.check_discord_account_api(session, "vortex"))
        self.assertEqual(r.status, TAKEN)
        self.assertEqual(
            session.post.call_args.args[0],
            checkers.DEFAULT_DISCORD_ACCOUNT_API_URL,
        )

    def test_discord_account_api_normalizes_case(self):
        session = _session_with_json(200, {"taken": False})
        r = self.run_async(checkers.check_discord_account_api(session, "Vortex"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(session.post.call_args.kwargs["json"], {"username": "vortex"})

    def test_discord_dnsrobot_loads_page_without_credentials(self):
        session = _session_with_json(200, {"taken": False})
        browser, page, context = _browser_with_status("Available")
        r = self.run_async(checkers.check_discord(
            session, "Vortex", mode="dnsrobot", dnsrobot_browser=browser,
            account_api_headers={"Authorization": "Bearer must-not-forward"},
            probe_headers={"X-Checker-Token": "must-not-forward"}))
        self.assertEqual(r.status, AVAILABLE)
        page.goto.assert_called_once_with(
            checkers.dnsrobot_username_checker_url("Vortex"),
            wait_until="domcontentloaded",
            timeout=unittest.mock.ANY,
        )
        page.wait_for_function.assert_called_once_with(
            checkers.DNSROBOT_PAGE_STATUS_SCRIPT,
            timeout=unittest.mock.ANY,
        )
        page.evaluate.assert_called_once_with(checkers.DNSROBOT_PAGE_STATUS_SCRIPT)
        session.post.assert_not_called()
        browser.new_context.assert_called_once()
        context.close.assert_awaited_once()
        self.assertEqual(
            checkers.dnsrobot_username_checker_url("a.b"),
            "https://dnsrobot.net/username-checker?u=a.b",
        )

    def test_discord_dnsrobot_without_browser_is_unknown(self):
        session = _session_with_json(200, {"taken": False})
        r = self.run_async(checkers.check_discord_dnsrobot(session, "vortex"))
        self.assertEqual(r.status, ERROR)
        session.post.assert_not_called()

    def test_discord_dnsrobot_block_is_unknown(self):
        browser, _, _ = _browser_with_status("Rate limited")
        r = self.run_async(checkers.check_discord_dnsrobot(
            _session_with_json(200, {}), "vortex", browser=browser))
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
        # Now returns 8 results (3 core + 5 extra)
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r.available for r in results))

    def test_parallel_run_all_core_only(self):
        results = self.run_async(checkers.run_all_checks(
            _session_with_status(404), "zxqw99182", discord_mode="probe",
            discord_probe_url="https://checker.example/{username}",
            enable_extra_platforms=False))
        # Core only = 3 results
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.available for r in results))

    def test_probe_token_is_sent_only_to_external_checker(self):
        session = _session_with_status(404)
        headers = {"Authorization": "Bearer not-a-real-secret"}
        self.run_async(checkers.run_all_checks(
            session, "zxqw99182", discord_mode="probe",
            discord_probe_url="https://checker.example/{username}",
            discord_probe_headers=headers,
            enable_extra_platforms=False))

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
             patch.object(checkers, "check_discord", slow_checker), \
             patch.object(checkers, "check_github", slow_checker), \
             patch.object(checkers, "check_steam", slow_checker), \
             patch.object(checkers, "check_reddit", slow_checker), \
             patch.object(checkers, "check_instagram", slow_checker), \
             patch.object(checkers, "check_twitter", slow_checker):
            results = self.run_async(checkers.run_all_checks(
                _session_with_status(404), "zxqw99182", timeout=0.001))

        self.assertEqual([r.status for r in results], [ERROR] * 8)
        self.assertTrue(all("deadline" in r.detail for r in results))

    # -- New platform checker tests --

    def test_github_free(self):
        session = _session_with_status(404)
        r = self.run_async(checkers.check_github(session, "zxqw99182nonexistent"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(r.emoji, "\U0001F4BB")

    def test_github_taken(self):
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={"login": "octocat", "id": 1})
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=ctx)
        r = self.run_async(checkers.check_github(session, "octocat"))
        self.assertEqual(r.status, TAKEN)

    def test_github_invalid_name(self):
        r = self.run_async(checkers.check_github(_session_with_status(200), "-bad"))
        self.assertEqual(r.status, INVALID)

    def test_steam_free(self):
        r = self.run_async(checkers.check_steam(
            _session_with_status(200, "The specified profile could not be found"),
            "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(r.emoji, "\U0001F3AE")

    def test_steam_taken(self):
        r = self.run_async(checkers.check_steam(
            _session_with_status(200, "<html>normal profile</html>"),
            "gabelogannewell"))
        self.assertEqual(r.status, TAKEN)

    def test_reddit_free(self):
        r = self.run_async(checkers.check_reddit(_session_with_status(404), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(r.emoji, "\U0001F440")

    def test_reddit_taken(self):
        response = MagicMock()
        response.status = 200
        response.json = AsyncMock(return_value={"data": {"name": "spez"}})
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=ctx)
        r = self.run_async(checkers.check_reddit(session, "spez"))
        self.assertEqual(r.status, TAKEN)

    def test_instagram_free(self):
        r = self.run_async(checkers.check_instagram(
            _session_with_status(404), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(r.emoji, "\U0001F4F8")

    def test_instagram_login_wall(self):
        r = self.run_async(checkers.check_instagram(
            _session_with_status(200, "Login to Instagram"), "kevin"))
        self.assertEqual(r.status, BLOCKED)

    def test_twitter_free(self):
        r = self.run_async(checkers.check_twitter(
            _session_with_status(404), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)
        self.assertEqual(r.emoji, "\U0001F426")

    def test_twitter_account_doesnt_exist(self):
        r = self.run_async(checkers.check_twitter(
            _session_with_status(200, "This account doesn't exist"), "zxqw99182"))
        self.assertEqual(r.status, AVAILABLE)


class TestProxyReportingAndRetries(unittest.TestCase):
    """The request layer must feed real outcomes back into the proxy pool."""

    @staticmethod
    def run_async(coro):
        return asyncio.run(coro)

    def test_success_is_reported_to_the_pool(self):
        from proxies import ProxyPool, ProxyProvider

        pool = ProxyPool(["http://p1:8080"])
        provider = ProxyProvider(pool=pool)
        session = _session_with_status(404)

        result = self.run_async(
            checkers.check_minecraft(session, "zxqw99182", provider))

        self.assertEqual(result.status, AVAILABLE)
        self.assertEqual(pool.alive_count, 1)
        # The request was actually routed through the proxy.
        self.assertEqual(session.get.call_args.kwargs["proxy"], "http://p1:8080")

    def test_failure_benches_the_proxy_without_waiting_for_health_sweep(self):
        from proxies import ProxyPool, ProxyProvider

        pool = ProxyPool(["http://p1:8080"])
        provider = ProxyProvider(pool=pool)
        session = MagicMock()
        session.get = MagicMock(
            side_effect=aiohttp.ClientProxyConnectionError(MagicMock(), OSError()))

        result = self.run_async(
            checkers.check_minecraft(session, "zxqw99182", provider))

        self.assertEqual(result.status, ERROR)
        # Live traffic (not just the 30s health sweep) recorded the failures.
        self.assertIn("100% fail", pool.status_summary())

    def test_transient_error_is_retried_once(self):
        calls = []
        ok = _session_with_status(404)

        def flaky_get(*args, **kwargs):
            calls.append(kwargs.get("proxy"))
            if len(calls) == 1:
                raise aiohttp.ClientConnectionError("reset")
            return ok.get(*args, **kwargs)

        session = MagicMock()
        session.get = MagicMock(side_effect=flaky_get)

        result = self.run_async(
            checkers.check_minecraft(session, "zxqw99182", None))

        self.assertEqual(result.status, AVAILABLE)
        self.assertEqual(len(calls), 2)  # failed once, retried, succeeded

    def test_retry_rotates_to_the_next_proxy(self):
        from proxies import ProxyPool, ProxyProvider

        pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
        provider = ProxyProvider(pool=pool)
        seen = []
        ok = _session_with_status(404)

        def flaky_get(*args, **kwargs):
            seen.append(kwargs.get("proxy"))
            if len(seen) == 1:
                raise aiohttp.ClientConnectionError("reset")
            return ok.get(*args, **kwargs)

        session = MagicMock()
        session.get = MagicMock(side_effect=flaky_get)

        self.run_async(checkers.check_minecraft(session, "zxqw99182", provider))

        self.assertEqual(seen, ["http://p1:8080", "http://p2:8080"])

    def test_plain_string_proxy_still_works(self):
        session = _session_with_status(404)
        result = self.run_async(
            checkers.check_minecraft(session, "zxqw99182", "http://p1:8080"))
        self.assertEqual(result.status, AVAILABLE)
        self.assertEqual(session.get.call_args.kwargs["proxy"], "http://p1:8080")


class TestTimeoutResults(unittest.TestCase):

    def test_includes_extra_platforms_by_default(self):
        self.assertEqual(len(checkers.timeout_results()), len(checkers.PLATFORMS))

    def test_core_only_when_extras_disabled(self):
        results = checkers.timeout_results("x", include_extra=False)
        self.assertEqual(len(results), len(checkers.CORE_PLATFORMS))
        self.assertTrue(all(r.status == ERROR for r in results))


class TestHeaders(unittest.TestCase):

    def test_no_brotli_is_advertised(self):
        """aiohttp cannot decode br without the optional Brotli package."""
        for headers in (checkers.BROWSER_HEADERS, checkers.API_HEADERS):
            self.assertNotIn("br", headers["Accept-Encoding"])


class TestProxyPool(unittest.TestCase):
    """Tests for the proxy pool module."""

    def test_import(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])
        self.assertEqual(pool.size, 2)
        self.assertEqual(pool.alive_count, 2)

    def test_round_robin(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080", "http://p2:8080", "http://p3:8080"])
        seen = set()
        for _ in range(6):
            seen.add(pool.next())
        self.assertEqual(len(seen), 3)

    def test_failure_tracking(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080", "http://p2:8080"])
        pool.report_failure("http://p1:8080")
        pool.report_failure("http://p1:8080")
        pool.report_failure("http://p1:8080")
        self.assertEqual(pool.alive_count, 1)
        # Only p2 should be returned now
        for _ in range(5):
            self.assertEqual(pool.next(), "http://p2:8080")

    def test_recovery_after_cooldown(self):
        import time
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080"], recovery_cooldown=0.01)
        for _ in range(3):
            pool.report_failure("http://p1:8080")
        self.assertEqual(pool.alive_count, 0)
        # After cooldown, should recover
        time.sleep(0.02)
        url = pool.next()
        self.assertEqual(url, "http://p1:8080")

    def test_empty_pool(self):
        from proxies import ProxyPool
        pool = ProxyPool()
        self.assertIsNone(pool.next())
        self.assertEqual(pool.size, 0)

    def test_parse_proxy_list(self):
        from proxies import parse_proxy_list
        result = parse_proxy_list("http://p1:8080,http://p2:8080,http://p3:8080")
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0], "http://p1:8080")
        # With newlines
        result2 = parse_proxy_list("http://p1:8080\nhttp://p2:8080")
        self.assertEqual(len(result2), 2)
        # Empty
        self.assertEqual(parse_proxy_list(""), [])
        self.assertEqual(parse_proxy_list("  "), [])

    def test_status_summary(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080"])
        summary = pool.status_summary()
        self.assertIn("p1", summary)
        self.assertIn("alive", summary)

    def test_direct_fallback_is_opt_in(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080"], allow_direct_fallback=True)
        for _ in range(3):
            pool.report_failure("http://p1:8080")
        # Opted in: prefer a direct connection over a known-dead proxy.
        self.assertIsNone(pool.next())

    def test_provider_wraps_static_url(self):
        from proxies import ProxyProvider
        provider = ProxyProvider(static_url="http://only:8080")
        self.assertEqual(provider(), "http://only:8080")
        self.assertTrue(provider.enabled)
        provider.report_failure("http://only:8080")  # must not raise

    def test_provider_without_proxies(self):
        from proxies import ProxyProvider
        provider = ProxyProvider()
        self.assertIsNone(provider())
        self.assertFalse(provider.enabled)

    def test_duplicate_proxies_are_collapsed(self):
        from proxies import ProxyPool, parse_proxy_list
        pool = ProxyPool(["http://p1:8080", "http://p1:8080"])
        self.assertEqual(pool.size, 1)
        self.assertEqual(
            parse_proxy_list("http://p1:8080,http://p1:8080"), ["http://p1:8080"])

    def test_unknown_url_report_is_ignored(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080"])
        pool.report_failure("http://not-in-pool:8080")
        pool.report_success(None)
        self.assertEqual(pool.alive_count, 1)

    def test_all_dead_falls_back(self):
        from proxies import ProxyPool
        pool = ProxyPool(["http://p1:8080"])
        for _ in range(3):
            pool.report_failure("http://p1:8080")
        self.assertEqual(pool.alive_count, 0)
        # Should still return the proxy (reset on all-dead)
        url = pool.next()
        self.assertEqual(url, "http://p1:8080")


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
        self.assertIn(free.status, (AVAILABLE, BLOCKED), free.detail)

    @unittest.skipUnless(os.getenv("LIVE") == "1", "set LIVE=1 to run")
    def test_live_github(self):
        taken = self._check(lambda s: checkers.check_github(s, "octocat"))
        free = self._check(lambda s: checkers.check_github(s, "zxqw7k3vlt9m42qnonexistent"))
        self.assertEqual(taken.status, TAKEN, taken.detail)
        self.assertEqual(free.status, AVAILABLE, free.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
