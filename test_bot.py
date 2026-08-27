"""
End-to-end pipeline tests for the Multi-Sniper bot - no Discord, no network.

These simulate real messages hitting SniperBot.on_message and verify the whole
flow: filtering -> cooldown -> cache -> parallel checks -> reactions.

Run with plain Python:     python test_bot.py
"""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import bot as bot_module
import checkers
from test_checkers import _browser_with_status, _session_with_status

WATCHED = 42  # pretend channel id


def make_message(content, user_id=1, channel_id=WATCHED, author_bot=False,
                 webhook_id=None):
    """Build a mock discord.Message."""
    msg = MagicMock()
    msg.author.bot = author_bot
    msg.author.id = user_id
    msg.author.mention = f"<@{user_id}>"
    msg.webhook_id = webhook_id
    msg.channel.id = channel_id
    msg.content = content
    msg.add_reaction = AsyncMock()
    return msg


def make_bot(session_status=404):
    """Build a SniperBot wired to a fake HTTP session."""
    b = bot_module.SniperBot()
    body = "<html><body>claimed profile</body></html>" if session_status == 200 else ""
    b.http_sniper = _session_with_status(session_status, body)
    return b


def reactions(message):
    """The emojis the bot reacted with, in order."""
    return [call.args[0] for call in message.add_reaction.await_args_list]


class TestFilters(unittest.TestCase):
    def run_msg(self, message):
        b = make_bot()
        asyncio.run(b.on_message(message))
        return b, message

    def test_bot_author_ignored(self):
        msg = make_message("Notch", author_bot=True)
        _, msg = self.run_msg(msg)
        self.assertEqual(reactions(msg), [])

    def test_webhook_message_ignored(self):
        msg = make_message("Notch", webhook_id=999)
        _, msg = self.run_msg(msg)
        self.assertEqual(reactions(msg), [])

    def test_multi_word_message_ignored(self):
        _, msg = self.run_msg(make_message("is notch free"))
        self.assertEqual(reactions(msg), [])

    def test_empty_message_ignored(self):
        _, msg = self.run_msg(make_message("   "))
        self.assertEqual(reactions(msg), [])

    def test_mention_ignored(self):
        _, msg = self.run_msg(make_message("<@123456789>"))
        self.assertEqual(reactions(msg), [])

    def test_wrong_channel_ignored(self):
        old = bot_module.TARGET_CHANNEL_ID
        bot_module.TARGET_CHANNEL_ID = WATCHED
        try:
            _, msg = self.run_msg(make_message("Notch", channel_id=777))
            self.assertEqual(reactions(msg), [])
        finally:
            bot_module.TARGET_CHANNEL_ID = old

    def test_right_channel_processed(self):
        old = bot_module.TARGET_CHANNEL_ID
        bot_module.TARGET_CHANNEL_ID = WATCHED
        try:
            _, msg = self.run_msg(make_message("Notch", channel_id=WATCHED))
            self.assertNotEqual(reactions(msg), [])
        finally:
            bot_module.TARGET_CHANNEL_ID = old


class TestReactions(unittest.TestCase):
    def setUp(self):
        self.old_mode = bot_module.DISCORD_CHECK_MODE
        self.old_probe_url = bot_module.DISCORD_PROBE_URL
        self.old_extra = bot_module.ENABLE_EXTRA_PLATFORMS
        bot_module.DISCORD_CHECK_MODE = "probe"
        bot_module.DISCORD_PROBE_URL = "https://checker.example/{username}"
        bot_module.ENABLE_EXTRA_PLATFORMS = True

    def tearDown(self):
        bot_module.DISCORD_CHECK_MODE = self.old_mode
        bot_module.DISCORD_PROBE_URL = self.old_probe_url
        bot_module.ENABLE_EXTRA_PLATFORMS = self.old_extra

    def test_free_everywhere_gets_all_emojis(self):
        b = make_bot(404)  # 404 everywhere -> free on all platforms
        msg = make_message("zxqw99182")
        asyncio.run(b.on_message(msg))
        r = reactions(msg)
        # Should get all 8 platform emojis
        self.assertEqual(len(r), 8)
        self.assertIn("\U0001F579\uFE0F", r)   # 🕹️ Minecraft
        self.assertIn("\U0001F52B", r)          # 🔫 guns.lol
        self.assertIn("\U0001F408\u200D\u2B1B", r)  # 🐈‍⬛ Discord
        self.assertIn("\U0001F4BB", r)          # 💻 GitHub
        self.assertIn("\U0001F3AE", r)          # 🎮 Steam
        self.assertIn("\U0001F440", r)          # 👀 Reddit
        self.assertIn("\U0001F4F8", r)          # 📸 Instagram
        self.assertIn("\U0001F426", r)          # 🐦 Twitter/X

    def test_free_core_only_when_extra_disabled(self):
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        b = make_bot(404)
        msg = make_message("zxqw99182")
        asyncio.run(b.on_message(msg))
        r = reactions(msg)
        # Should get only 2 core emojis (MC + guns.lol, Discord is SKIPPED in off mode)
        # Wait, we set mode to probe above in setUp, so Discord is checked
        self.assertEqual(len(r), 3)

    def test_dnsrobot_mode_loads_page_without_probe_credentials(self):
        old_mode = bot_module.DISCORD_CHECK_MODE
        bot_module.DISCORD_CHECK_MODE = "dnsrobot"
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        try:
            b = make_bot(404)
            browser, page, _ = _browser_with_status("Available")
            b.dnsrobot_browser = browser
            msg = make_message("zxqw99182")
            asyncio.run(b.on_message(msg))
            r = reactions(msg)
            self.assertIn("\U0001F579\uFE0F", r)
            self.assertIn("\U0001F52B", r)
            self.assertIn("\U0001F408\u200D\u2B1B", r)
            page.goto.assert_called_once()
            b.http_sniper.post.assert_not_called()
        finally:
            bot_module.DISCORD_CHECK_MODE = old_mode
            bot_module.ENABLE_EXTRA_PLATFORMS = True

    def test_taken_everywhere_gets_cross(self):
        # With extra platforms, the simple mock may return BLOCKED for
        # platforms that need specific JSON (GitHub, Reddit), so we test
        # the core-only path where 200 reliably means TAKEN everywhere.
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        try:
            b = make_bot(200)  # 200 everywhere -> taken on core platforms
            msg = make_message("Notch")
            asyncio.run(b.on_message(msg))
            self.assertEqual(reactions(msg), ["❌"])
        finally:
            bot_module.ENABLE_EXTRA_PLATFORMS = True

    def test_discord_off_takes_no_emoji(self):
        bot_module.DISCORD_CHECK_MODE = "off"
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        try:
            b = make_bot(404)
            msg = make_message("zxqw99182")
            asyncio.run(b.on_message(msg))
            r = reactions(msg)
            self.assertIn("\U0001F579\uFE0F", r)
            self.assertIn("\U0001F52B", r)
        finally:
            bot_module.DISCORD_CHECK_MODE = "probe"
            bot_module.ENABLE_EXTRA_PLATFORMS = True

    def test_minecraft_invalid_name_reacts_gunslol_only(self):
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        # "ab" is too short for Minecraft (INVALID) but fine for guns.lol
        # and the lowercase Discord probe -> both report 404 -> free.
        b = make_bot(404)
        msg = make_message("ab")
        asyncio.run(b.on_message(msg))
        self.assertIn("\U0001F52B", reactions(msg))
        self.assertIn("\U0001F408\u200D\u2B1B", reactions(msg))

    def test_all_checks_failed_gets_warning(self):
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        broken = MagicMock()
        broken.get = MagicMock(side_effect=checkers.aiohttp.ClientError("down"))
        b = make_bot()
        b.http_sniper = broken
        # Lowercase name so the Discord probe is valid and actually errors.
        msg = make_message("vortex")
        asyncio.run(b.on_message(msg))
        self.assertEqual(reactions(msg), ["⚠️"])

    def test_partial_outage_gets_warning_not_misleading_cross(self):
        async def partial_results(*_args, **_kwargs):
            return [
                checkers.Result("Minecraft", "🕹️", checkers.TAKEN),
                checkers.Result("guns.lol", "🔫", checkers.BLOCKED),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
            ]

        b = make_bot()
        msg = make_message("vortex")
        with patch.object(checkers, "run_all_checks", partial_results):
            asyncio.run(b.on_message(msg))
        self.assertEqual(reactions(msg), ["⚠️"])
        self.assertNotIn("vortex", b._cache)

    def test_unrecognized_status_gets_warning_and_is_not_cached(self):
        async def malformed_results(*_args, **_kwargs):
            return [
                checkers.Result("Minecraft", "🕹️", checkers.TAKEN),
                checkers.Result("guns.lol", "🔫", "unexpected"),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
            ]

        b = make_bot()
        msg = make_message("vortex")
        with patch.object(checkers, "run_all_checks", malformed_results):
            asyncio.run(b.on_message(msg))
        self.assertEqual(reactions(msg), ["⚠️"])
        self.assertNotIn("vortex", b._cache)


class TestCooldown(unittest.TestCase):
    def test_fourth_check_in_window_gets_hourglass(self):
        old_max = bot_module.USER_MAX_CHECKS
        bot_module.USER_MAX_CHECKS = 3
        try:
            b = make_bot(404)
            for i in range(3):
                msg = make_message(f"name{i:04d}", user_id=7)
                asyncio.run(b.on_message(msg))
                self.assertNotIn("\u23f3", reactions(msg))
            fourth = make_message("another", user_id=7)
            asyncio.run(b.on_message(fourth))
            self.assertEqual(reactions(fourth), ["\u23f3"])
        finally:
            bot_module.USER_MAX_CHECKS = old_max

    def test_other_users_not_affected(self):
        old_max = bot_module.USER_MAX_CHECKS
        bot_module.USER_MAX_CHECKS = 1
        try:
            b = make_bot(404)
            first = make_message("name0001", user_id=7)
            asyncio.run(b.on_message(first))
            other = make_message("name0002", user_id=8)
            asyncio.run(b.on_message(other))
            self.assertNotIn("\u23f3", reactions(other))
        finally:
            bot_module.USER_MAX_CHECKS = old_max


class TestCache(unittest.TestCase):
    def test_repeat_lookup_uses_cache(self):
        old_extra = bot_module.ENABLE_EXTRA_PLATFORMS
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        try:
            b = make_bot(404)
            first = make_message("zxqw99182", user_id=1)
            second = make_message("zxqw99182", user_id=2)  # different user
            asyncio.run(b.on_message(first))
            asyncio.run(b.on_message(second))
            # Only ONE round of HTTP requests for two messages.
            self.assertEqual(b.http_sniper.get.call_count, 2)  # MC + guns.lol
            # But both messages still get their reactions.
            self.assertEqual(reactions(first), reactions(second))
        finally:
            bot_module.ENABLE_EXTRA_PLATFORMS = old_extra

    def test_inconclusive_outage_is_not_cached(self):
        broken = MagicMock()
        broken.get = MagicMock(side_effect=checkers.aiohttp.ClientError("down"))
        b = make_bot()
        b.http_sniper = broken
        asyncio.run(b.on_message(make_message("vortex")))
        self.assertNotIn("vortex", b._cache)

    def test_smart_cache_ttl(self):
        """Available results should use the shorter TTL, taken the longer one."""
        old_available = bot_module.CACHE_TTL_AVAILABLE
        old_taken = bot_module.CACHE_TTL_TAKEN
        bot_module.CACHE_TTL_AVAILABLE = 0.01
        bot_module.CACHE_TTL_TAKEN = 100.0
        try:
            b = make_bot(404)
            # Cache an available result
            results = [checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE, "HTTP 404")]
            b._cache["testname"] = (time.monotonic(), results)
            # Should be cached (within TTL)
            self.assertIsNotNone(b._cached("testname"))
            # After TTL expires, should be gone
            import time as t
            t.sleep(0.02)
            self.assertIsNone(b._cached("testname"))
        finally:
            bot_module.CACHE_TTL_AVAILABLE = old_available
            bot_module.CACHE_TTL_TAKEN = old_taken


class TestCacheBounds(unittest.TestCase):
    """The result cache must stay bounded on a busy server."""

    def test_store_prunes_over_the_ceiling(self):
        b = make_bot(404)
        old_max = bot_module.CACHE_MAX_ENTRIES
        bot_module.CACHE_MAX_ENTRIES = 10
        try:
            results = [checkers.Result("Minecraft", "x", checkers.TAKEN, "")]
            for i in range(50):
                b._store(f"name{i:04d}", results)
            self.assertLessEqual(len(b._cache), 10)
            # The most recent write survives the prune.
            self.assertIn("name0049", b._cache)
        finally:
            bot_module.CACHE_MAX_ENTRIES = old_max

    def test_expired_entry_is_evicted_on_read(self):
        b = make_bot(404)
        old_ttl = bot_module.CACHE_TTL_TAKEN
        bot_module.CACHE_TTL_TAKEN = 0.0
        try:
            b._store("vortex", [
                checkers.Result("Minecraft", "x", checkers.TAKEN, "")])
            self.assertIsNone(b._cached("vortex"))
            self.assertNotIn("vortex", b._cache)
        finally:
            bot_module.CACHE_TTL_TAKEN = old_ttl


class TestLatencyBudget(unittest.TestCase):
    def test_checker_crash_still_reacts_with_warning(self):
        async def crashes(*_args, **_kwargs):
            raise RuntimeError("unexpected checker crash")

        b = make_bot()
        message = make_message("vortex")
        with patch.object(checkers, "run_all_checks", crashes):
            asyncio.run(b.on_message(message))
        self.assertEqual(reactions(message), ["⚠️"])

    def test_outer_deadline_stops_a_non_cooperative_checker(self):
        old_budget = bot_module.RESPONSE_BUDGET_SECONDS
        old_reaction_timeout = bot_module.REACTION_TIMEOUT

        async def ignores_its_timeout(*_args, **_kwargs):
            await asyncio.sleep(0.2)
            return []

        bot_module.RESPONSE_BUDGET_SECONDS = 0.05
        bot_module.REACTION_TIMEOUT = 0.01
        try:
            b = make_bot()
            message = make_message("vortex")
            started = time.monotonic()
            with patch.object(checkers, "run_all_checks", ignores_its_timeout):
                asyncio.run(b.on_message(message))
            elapsed = time.monotonic() - started
        finally:
            bot_module.RESPONSE_BUDGET_SECONDS = old_budget
            bot_module.REACTION_TIMEOUT = old_reaction_timeout

        self.assertLess(elapsed, 0.15)
        self.assertEqual(reactions(message), ["⚠️"])

    def test_reaction_deadline_does_not_wait_for_non_cooperative_client(self):
        async def hangs_after_cancellation(_emoji):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)

        async def scenario():
            b = make_bot()
            message = make_message("vortex")
            message.add_reaction = hangs_after_cancellation
            started = time.monotonic()
            await b._react(message, "⚠️", timeout=0.01)
            return time.monotonic() - started

        loop = asyncio.new_event_loop()
        try:
            elapsed = loop.run_until_complete(scenario())
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self.assertLess(elapsed, 0.1)


class TestConfigErrors(unittest.TestCase):
    def test_non_finite_number_uses_safe_default(self):
        with patch.dict(bot_module.os.environ, {"CHECK_TIMEOUT": "nan"}, clear=False):
            self.assertEqual(bot_module._opt_float("CHECK_TIMEOUT", 3.0), 3.0)
        with patch.dict(bot_module.os.environ, {"USER_MAX_CHECKS": "inf"}, clear=False):
            self.assertEqual(bot_module._bounded_int("USER_MAX_CHECKS", 3, 1, 10), 3)

    def test_bad_discord_mode_rejected(self):
        old_token, old_mode = bot_module.TOKEN, bot_module.DISCORD_CHECK_MODE
        bot_module.TOKEN = "fake_token_so_token_check_passes"
        bot_module.DISCORD_CHECK_MODE = "bogus"
        try:
            with self.assertRaises(SystemExit):
                bot_module.main()
        finally:
            bot_module.TOKEN, bot_module.DISCORD_CHECK_MODE = old_token, old_mode

    def test_bad_proxy_rejected_before_connecting(self):
        old_token, old_proxy = bot_module.TOKEN, bot_module.PROXY_URL
        bot_module.TOKEN = "test-bot-token"
        bot_module.PROXY_URL = "socks5://proxy.example"
        try:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main()
            self.assertIn("PROXY_URL", str(raised.exception))
        finally:
            bot_module.TOKEN, bot_module.PROXY_URL = old_token, old_proxy

    def test_bad_probe_url_rejected_before_connecting(self):
        old = (bot_module.TOKEN, bot_module.DISCORD_CHECK_MODE,
               bot_module.DISCORD_PROBE_URL)
        bot_module.TOKEN = "test-bot-token"
        bot_module.DISCORD_CHECK_MODE = "probe"
        bot_module.DISCORD_PROBE_URL = "https://checker.example/static"
        try:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main()
            self.assertIn("placeholder", str(raised.exception))
        finally:
            (bot_module.TOKEN, bot_module.DISCORD_CHECK_MODE,
             bot_module.DISCORD_PROBE_URL) = old

    def test_bad_account_api_url_rejected_before_connecting(self):
        old = (bot_module.TOKEN, bot_module.DISCORD_CHECK_MODE,
               bot_module.DISCORD_ACCOUNT_API_URL)
        bot_module.TOKEN = "test-bot-token"
        bot_module.DISCORD_CHECK_MODE = "account"
        bot_module.DISCORD_ACCOUNT_API_URL = "file:///tmp/account"
        try:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main()
            self.assertIn("DISCORD_ACCOUNT_API_URL", str(raised.exception))
        finally:
            (bot_module.TOKEN, bot_module.DISCORD_CHECK_MODE,
             bot_module.DISCORD_ACCOUNT_API_URL) = old

    def test_invalid_probe_header_rejected_before_connecting(self):
        old = (bot_module.TOKEN, bot_module.DISCORD_PROBE_TOKEN,
               bot_module.DISCORD_PROBE_TOKEN_HEADER)
        bot_module.TOKEN = "test-bot-token"
        bot_module.DISCORD_PROBE_TOKEN = "not-a-real-secret"
        bot_module.DISCORD_PROBE_TOKEN_HEADER = "Bad\nHeader"
        try:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main()
            self.assertIn("HEADER", str(raised.exception))
        finally:
            (bot_module.TOKEN, bot_module.DISCORD_PROBE_TOKEN,
             bot_module.DISCORD_PROBE_TOKEN_HEADER) = old

    def test_missing_token_rejected(self):
        old = bot_module.TOKEN
        bot_module.TOKEN = ""
        try:
            with self.assertRaises(SystemExit):
                bot_module.main()
        finally:
            bot_module.TOKEN = old

    def test_token_line_break_rejected(self):
        old = bot_module.TOKEN
        bot_module.TOKEN = "test-bot-token\nextra"
        try:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main()
            self.assertIn("control characters", str(raised.exception))
        finally:
            bot_module.TOKEN = old

    def test_proxy_pool_validation(self):
        """Proxy pool URLs should each be validated."""
        old = (bot_module.TOKEN, bot_module.PROXY_URLS_RAW)
        bot_module.TOKEN = "test-bot-token"
        bot_module.PROXY_URLS_RAW = "socks5://bad-proxy:8080"
        try:
            with self.assertRaises(SystemExit) as raised:
                bot_module.main()
            self.assertIn("POOL", str(raised.exception))
        finally:
            bot_module.TOKEN, bot_module.PROXY_URLS_RAW = old


class TestProxyIntegration(unittest.TestCase):
    """Tests for proxy pool integration in the bot."""

    def test_next_proxy_without_pool(self):
        """Without a proxy pool, _next_proxy returns PROXY_URL."""
        old_proxy = bot_module.PROXY_URL
        bot_module.PROXY_URL = None
        try:
            b = bot_module.SniperBot()
            self.assertIsNone(b._next_proxy())
        finally:
            bot_module.PROXY_URL = old_proxy

    def test_next_proxy_with_pool(self):
        """With a proxy pool, _next_proxy should use rotation."""
        b = bot_module.SniperBot()
        b.proxy_pool = MagicMock()
        b.proxy_pool.next.return_value = "http://proxy1:8080"
        self.assertEqual(b._next_proxy(), "http://proxy1:8080")
        b.proxy_pool.next.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
