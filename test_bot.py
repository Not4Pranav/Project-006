"""
End-to-end pipeline tests for the Multi-Sniper bot - no Discord, no network.

These simulate real messages hitting SniperBot.on_message and verify the whole
flow: filtering -> cooldown -> cache -> parallel checks -> reactions.

Run with plain Python:     python test_bot.py
"""

import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import contextlib

import aiohttp
import discord

import bot as bot_module
import checkers
from test_checkers import _browser_with_status, _session_with_status

WATCHED = 42  # pretend channel id


def discord_forbidden():
    response = MagicMock()
    response.status = 403
    response.reason = "Forbidden"
    return discord.Forbidden(response, "no permission")


def discord_not_found():
    response = MagicMock()
    response.status = 404
    response.reason = "Not Found"
    return discord.NotFound(response, "gone")


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
    sent = MagicMock()
    sent.edit = AsyncMock()
    msg.reply = AsyncMock(return_value=sent)
    msg._sent = sent
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


def reply_texts(message):
    """Every body the bot posted or edited into its reply, in order."""
    texts = [call.args[0] for call in message.reply.await_args_list]
    texts += [call.kwargs.get("content") for call
              in message._sent.edit.await_args_list]
    return texts


def final_reply(message):
    """The last text the member ends up seeing."""
    texts = reply_texts(message)
    return texts[-1] if texts else None


class ReactModeMixin:
    """Force emoji-reaction mode for suites that assert on reactions."""

    RESPONSE_MODE = "react"

    def setUp(self):
        super().setUp()
        saved = (bot_module.RESPONSE_MODE, bot_module.REPLY_ENABLED,
                 bot_module.REACT_ENABLED)

        def restore():
            (bot_module.RESPONSE_MODE, bot_module.REPLY_ENABLED,
             bot_module.REACT_ENABLED) = saved

        self.addCleanup(restore)
        bot_module.RESPONSE_MODE = self.RESPONSE_MODE
        bot_module.REPLY_ENABLED = self.RESPONSE_MODE in ("reply", "both")
        bot_module.REACT_ENABLED = self.RESPONSE_MODE in ("react", "both")


@contextlib.contextmanager
def patch_workers(results=None, factory=None):
    """Replace the shared worker builder used by both check paths.

    ``bot.py`` has two fan-out modes (streaming and batched) that share
    ``checkers.build_check_workers``; patching that keeps these scenarios
    honest for whichever mode is active.
    """

    def build(*_args, **_kwargs):
        if factory is not None:
            return factory()

        async def done(result):
            return result

        return [done(result) for result in results]

    with patch.object(checkers, "build_check_workers", build):
        yield


class TestFilters(ReactModeMixin, unittest.TestCase):
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


class TestReactions(ReactModeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
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
        b = make_bot()
        msg = make_message("vortex")
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.TAKEN),
                checkers.Result("guns.lol", "🔫", checkers.BLOCKED),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
        ]):
            asyncio.run(b.on_message(msg))
        self.assertEqual(reactions(msg), ["⚠️"])
        self.assertNotIn("vortex", b._cache)

    def test_unrecognized_status_gets_warning_and_is_not_cached(self):
        b = make_bot()
        msg = make_message("vortex")
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.TAKEN),
                checkers.Result("guns.lol", "🔫", "unexpected"),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
        ]):
            asyncio.run(b.on_message(msg))
        self.assertEqual(reactions(msg), ["⚠️"])
        self.assertNotIn("vortex", b._cache)


class TestCooldown(ReactModeMixin, unittest.TestCase):
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


class TestCache(ReactModeMixin, unittest.TestCase):
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
            # Streaming reacts in completion order and a cache hit reacts in
            # platform order, so compare the emoji *sets*, not the sequence.
            self.assertEqual(set(reactions(first)), set(reactions(second)))
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


class TestStreamingReactions(ReactModeMixin, unittest.TestCase):
    """A fast free platform must not wait for a slow one."""

    def test_fast_platform_reacts_before_slow_one_finishes(self):
        order = []

        async def fast():
            order.append("fast")
            return checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE)

        async def slow():
            await asyncio.sleep(0.25)
            order.append("slow")
            return checkers.Result("guns.lol", "🔫", checkers.AVAILABLE)

        b = make_bot()
        message = make_message("vortex")
        seen_at = {}
        original = b._react

        async def timed_react(msg, emoji, timeout=None):
            seen_at[emoji] = time.monotonic()
            return await original(msg, emoji, timeout)

        b._react = timed_react
        started = time.monotonic()
        with patch_workers(factory=lambda: [fast(), slow()]):
            asyncio.run(b.on_message(message))

        self.assertIn("🕹️", seen_at)
        # The fast platform's emoji lands almost immediately, well before the
        # 0.25s slow check completes.
        self.assertLess(seen_at["🕹️"] - started, 0.15)
        self.assertEqual(order, ["fast", "slow"])

    def test_missing_platforms_are_reported_as_errors(self):
        async def only_one():
            return checkers.Result("Minecraft", "🕹️", checkers.TAKEN)

        b = make_bot()
        message = make_message("vortex")
        with patch_workers(factory=lambda: [only_one()]):
            asyncio.run(b.on_message(message))
        # Nothing free and platforms missing -> honest warning, no caching.
        self.assertEqual(reactions(message), ["⚠️"])
        self.assertNotIn("vortex", b._cache)

    def test_batched_mode_still_works(self):
        old = bot_module.STREAM_REACTIONS
        bot_module.STREAM_REACTIONS = False
        try:
            b = make_bot(404)
            message = make_message("zxqw99182")
            asyncio.run(b.on_message(message))
            self.assertIn("🕹️", reactions(message))
        finally:
            bot_module.STREAM_REACTIONS = old


class ReplyModeMixin(ReactModeMixin):
    RESPONSE_MODE = "reply"


class TestReplyMode(ReplyModeMixin, unittest.TestCase):
    """The default output: a readable 'Platform: Status' reply, no reactions."""

    def setUp(self):
        super().setUp()
        saved = bot_module.ENABLE_EXTRA_PLATFORMS
        self.addCleanup(
            lambda: setattr(bot_module, "ENABLE_EXTRA_PLATFORMS", saved))
        bot_module.ENABLE_EXTRA_PLATFORMS = False

    def test_reply_lists_each_platform_and_adds_no_reactions(self):
        b = make_bot()
        msg = make_message("vortex")
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE),
                checkers.Result("guns.lol", "🔫", checkers.TAKEN),
                checkers.Result("Discord", "🐈‍⬛", checkers.BLOCKED),
        ]):
            asyncio.run(b.on_message(msg))

        self.assertEqual(reactions(msg), [])          # no emoji spam
        self.assertEqual(final_reply(msg), (
            "Minecraft: Available\n"
            "guns.lol: Unavailable\n"
            "Discord: Unknown"
        ))

    def test_reply_does_not_ping_the_author(self):
        b = make_bot(404)
        msg = make_message("zxqw99182")
        asyncio.run(b.on_message(msg))
        self.assertFalse(msg.reply.await_args.kwargs["mention_author"])

    def test_error_and_blocked_both_read_as_unknown(self):
        b = make_bot()
        msg = make_message("vortex")
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.ERROR, "boom"),
                checkers.Result("guns.lol", "🔫", checkers.BLOCKED, "cf"),
                checkers.Result("Discord", "🐈‍⬛", checkers.INVALID),
        ]):
            asyncio.run(b.on_message(msg))
        self.assertEqual(final_reply(msg), (
            "Minecraft: Unknown\nguns.lol: Unknown\nDiscord: Invalid"))

    def test_skipped_platforms_are_hidden_by_default(self):
        b = make_bot()
        msg = make_message("vortex")
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE),
                checkers.Result("guns.lol", "🔫", checkers.TAKEN),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
        ]):
            asyncio.run(b.on_message(msg))
        self.assertNotIn("Discord", final_reply(msg))

    def test_skipped_platforms_can_be_shown(self):
        saved = bot_module.REPLY_INCLUDE_SKIPPED
        bot_module.REPLY_INCLUDE_SKIPPED = True
        try:
            b = make_bot()
            msg = make_message("vortex")
            with patch_workers([
                    checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE),
                    checkers.Result("guns.lol", "🔫", checkers.TAKEN),
                    checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
            ]):
                asyncio.run(b.on_message(msg))
            self.assertIn("Discord: Not checked", final_reply(msg))
        finally:
            bot_module.REPLY_INCLUDE_SKIPPED = saved

    def test_first_paint_is_immediate_and_final_paint_is_complete(self):
        async def fast():
            return checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE)

        async def slow():
            await asyncio.sleep(0.3)
            return checkers.Result("guns.lol", "🔫", checkers.TAKEN)

        b = make_bot()
        msg = make_message("vortex")
        started = time.monotonic()
        paint_times = []
        original = b._send_reply

        async def timed(*args, **kwargs):
            paint_times.append(time.monotonic() - started)
            return await original(*args, **kwargs)

        b._send_reply = timed
        with patch_workers(factory=lambda: [fast(), slow()]):
            asyncio.run(b.on_message(msg))

        # Painted long before the 0.3s straggler finished...
        self.assertTrue(paint_times)
        self.assertLess(paint_times[0], 0.2)
        # ...and the pending marker is gone from the final text.
        self.assertNotIn(bot_module.PENDING_LABEL, final_reply(msg))
        self.assertIn("guns.lol: Unavailable", final_reply(msg))

    def test_pending_platforms_are_marked_while_streaming(self):
        async def fast():
            return checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE)

        async def slow():
            await asyncio.sleep(0.25)
            return checkers.Result("guns.lol", "🔫", checkers.TAKEN)

        b = make_bot()
        msg = make_message("vortex")
        with patch_workers(factory=lambda: [fast(), slow()]):
            asyncio.run(b.on_message(msg))
        self.assertIn(bot_module.PENDING_LABEL, reply_texts(msg)[0])

    def test_missing_send_permission_is_survivable(self):
        b = make_bot(404)
        msg = make_message("zxqw99182")
        msg.reply = AsyncMock(side_effect=discord_forbidden())
        asyncio.run(b.on_message(msg))   # must not raise

    def test_deleted_reply_stops_further_edits(self):
        sent = MagicMock()
        sent.edit = AsyncMock(side_effect=discord_not_found())

        async def fast():
            return checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE)

        async def slow():
            await asyncio.sleep(0.2)
            return checkers.Result("guns.lol", "🔫", checkers.TAKEN)

        b = make_bot()
        msg = make_message("vortex")
        msg.reply = AsyncMock(return_value=sent)
        with patch_workers(factory=lambda: [fast(), slow()]):
            asyncio.run(b.on_message(msg))   # must not raise
        self.assertEqual(sent.edit.await_count, 1)  # gave up after NotFound

    def test_both_mode_replies_and_reacts(self):
        bot_module.RESPONSE_MODE = "both"
        bot_module.REPLY_ENABLED = True
        bot_module.REACT_ENABLED = True
        b = make_bot()
        msg = make_message("vortex")
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE),
                checkers.Result("guns.lol", "🔫", checkers.TAKEN),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
        ]):
            asyncio.run(b.on_message(msg))
        self.assertEqual(reactions(msg), ["🕹️"])
        self.assertIn("Minecraft: Available", final_reply(msg))

    def test_cache_hit_also_replies(self):
        b = make_bot()
        first = make_message("vortex", user_id=1)
        second = make_message("vortex", user_id=2)
        with patch_workers([
                checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE),
                checkers.Result("guns.lol", "🔫", checkers.TAKEN),
                checkers.Result("Discord", "🐈‍⬛", checkers.SKIPPED),
        ]):
            asyncio.run(b.on_message(first))
            asyncio.run(b.on_message(second))
        self.assertEqual(final_reply(first), final_reply(second))
        self.assertEqual(second.reply.await_count, 1)   # single shot, no edits


class TestFormatResults(unittest.TestCase):
    """Pure rendering, independent of Discord."""

    def test_order_follows_platforms_not_completion(self):
        saved = bot_module.ENABLE_EXTRA_PLATFORMS
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        try:
            text = bot_module.format_results([
                checkers.Result("Discord", "x", checkers.AVAILABLE),
                checkers.Result("guns.lol", "x", checkers.TAKEN),
                checkers.Result("Minecraft", "x", checkers.AVAILABLE),
            ])
            self.assertEqual(text.splitlines(), [
                "Minecraft: Available",
                "guns.lol: Unavailable",
                "Discord: Available",
            ])
        finally:
            bot_module.ENABLE_EXTRA_PLATFORMS = saved

    def test_pending_lines_only_when_requested(self):
        saved = bot_module.ENABLE_EXTRA_PLATFORMS
        bot_module.ENABLE_EXTRA_PLATFORMS = False
        try:
            partial = [checkers.Result("Minecraft", "x", checkers.AVAILABLE)]
            self.assertIn(bot_module.PENDING_LABEL,
                          bot_module.format_results(partial, pending=True))
            self.assertNotIn(bot_module.PENDING_LABEL,
                             bot_module.format_results(partial, pending=False))
        finally:
            bot_module.ENABLE_EXTRA_PLATFORMS = saved

    def test_empty_results_are_never_an_empty_message(self):
        self.assertTrue(bot_module.format_results([]).strip())


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


class TestLatencyBudget(ReactModeMixin, unittest.TestCase):
    def test_checker_crash_still_reacts_with_warning(self):
        async def crashes():
            raise RuntimeError("unexpected checker crash")

        b = make_bot()
        message = make_message("vortex")
        with patch_workers(factory=lambda: [crashes()]):
            asyncio.run(b.on_message(message))
        self.assertEqual(reactions(message), ["⚠️"])

    def test_outer_deadline_stops_a_non_cooperative_checker(self):
        old_budget = bot_module.RESPONSE_BUDGET_SECONDS
        old_reaction_timeout = bot_module.REACTION_TIMEOUT

        async def ignores_its_timeout():
            await asyncio.sleep(0.2)
            return checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE)

        bot_module.RESPONSE_BUDGET_SECONDS = 0.05
        bot_module.REACTION_TIMEOUT = 0.01
        try:
            b = make_bot()
            message = make_message("vortex")
            started = time.monotonic()
            with patch_workers(factory=lambda: [ignores_its_timeout()]):
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



class TestProxyVerificationSearch(unittest.TestCase):
    """The pool must reach PROXY_MIN_POOL even from a mostly-dead list."""

    def setUp(self):
        import proxies as proxies_module
        self.proxies_module = proxies_module
        saved = {name: getattr(bot_module, name) for name in (
            "PROXY_MIN_POOL", "PROXY_MAX_POOL", "PROXY_VERIFY_MAX_SECONDS",
            "PROXY_VERIFY_CONCURRENCY", "PROXY_VERIFY_TIMEOUT")}

        def restore():
            for name, value in saved.items():
                setattr(bot_module, name, value)

        self.addCleanup(restore)
        bot_module.PROXY_MIN_POOL = 10
        bot_module.PROXY_MAX_POOL = 25
        bot_module.PROXY_VERIFY_MAX_SECONDS = 30.0
        bot_module.PROXY_VERIFY_CONCURRENCY = 8
        bot_module.PROXY_VERIFY_TIMEOUT = 1.0

    def build(self, pool_urls, reserve, alive):
        """A bot whose probes answer only for URLs in ``alive``."""

        from proxies import ProxyPool, ProxyProvider

        bot = bot_module.SniperBot.__new__(bot_module.SniperBot)
        bot.http_sniper = MagicMock()
        bot.proxy_provider = ProxyProvider(static_url=None)
        bot.proxy_provider.pool = ProxyPool(pool_urls)
        bot._proxy_reserve = list(reserve)
        self.tested = []

        async def fake_probe(_session, urls, *_args, **_kwargs):
            self.tested.extend(urls)
            return [url for url in urls if url in alive]

        self.patch = patch.object(bot_module, "probe_proxies", fake_probe)
        return bot

    @staticmethod
    def fake_session_factory():
        """aiohttp.ClientSession stand-in whose close() is awaitable."""

        session = MagicMock()
        session.close = AsyncMock()
        return MagicMock(return_value=session)

    def run_verify(self, bot):
        async def run():
            with patch.object(aiohttp, "ClientSession",
                              self.fake_session_factory()):
                await bot._verify_proxies()
        asyncio.run(run())

    def urls(self, start, count):
        return [f"http://10.0.{(start + i) // 256}.{(start + i) % 256}:8080"
                for i in range(count)]

    def test_search_continues_until_the_floor_is_reached(self):
        pool_urls = self.urls(0, 25)
        reserve = self.urls(100, 500)
        alive = set(reserve[::40])            # ~13 working, none in the first pool
        bot = self.build(pool_urls, reserve, alive)
        with self.patch:
            self.run_verify(bot)

        pool = bot.proxy_provider.pool
        self.assertGreaterEqual(pool.size, bot_module.PROXY_MIN_POOL)
        self.assertTrue(set(pool.urls) <= alive, "a dead proxy survived")
        self.assertGreater(len(self.tested), len(pool_urls),
                           "the reserve was never touched")

    def test_stops_at_the_maximum(self):
        pool_urls = self.urls(0, 25)
        reserve = self.urls(100, 500)
        alive = set(pool_urls + reserve)      # everything works
        bot = self.build(pool_urls, reserve, alive)
        with self.patch:
            self.run_verify(bot)
        self.assertLessEqual(bot.proxy_provider.pool.size,
                             bot_module.PROXY_MAX_POOL)

    def test_exhausted_reserve_keeps_what_was_found(self):
        pool_urls = self.urls(0, 25)
        reserve = self.urls(100, 60)
        alive = {reserve[0], reserve[1], pool_urls[0]}
        bot = self.build(pool_urls, reserve, alive)
        with self.patch:
            self.run_verify(bot)

        pool = bot.proxy_provider.pool
        self.assertEqual(sorted(pool.urls), sorted(alive))
        self.assertEqual(bot._proxy_reserve, [])   # searched to the end

    def test_nothing_alive_keeps_the_pool_rather_than_going_direct(self):
        pool_urls = self.urls(0, 25)
        bot = self.build(pool_urls, self.urls(100, 100), set())
        with self.patch:
            self.run_verify(bot)

        pool = bot.proxy_provider.pool
        self.assertEqual(pool.size, 25)
        self.assertEqual(pool.alive_count, 25)
        self.assertIsNotNone(pool.next())

    def test_time_budget_stops_the_search(self):
        bot_module.PROXY_VERIFY_MAX_SECONDS = 0.25
        pool_urls = self.urls(0, 25)
        reserve = self.urls(100, 5000)
        bot = self.build(pool_urls, reserve, set())

        async def slow_probe(_session, urls, *_args, **_kwargs):
            await asyncio.sleep(0.1)
            return []

        async def run():
            with patch.object(bot_module, "probe_proxies", slow_probe), \
                    patch.object(aiohttp, "ClientSession",
                                 self.fake_session_factory()):
                started = asyncio.get_running_loop().time()
                await bot._verify_proxies()
                return asyncio.get_running_loop().time() - started

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 5.0)
        self.assertTrue(bot._proxy_reserve, "the whole reserve was consumed")

    def test_verified_proxies_are_marked_healthy(self):
        pool_urls = self.urls(0, 25)
        alive = set(pool_urls[:12])
        bot = self.build(pool_urls, [], alive)
        with self.patch:
            self.run_verify(bot)

        pool = bot.proxy_provider.pool
        self.assertEqual(pool.size, 12)
        self.assertEqual(pool.alive_count, 12)
        # Rotation hands out only survivors, and spreads across them.
        handed_out = {pool.next() for _ in range(12)}
        self.assertEqual(handed_out, alive)

    def test_minimum_above_maximum_is_corrected_at_import(self):
        """PROXY_MAX_POOL below PROXY_MIN_POOL is a typo, not a smaller pool."""

        import subprocess
        env = dict(os.environ, PROXY_MIN_POOL="150", PROXY_MAX_POOL="20")
        out = subprocess.run(
            [sys.executable, "-c",
             "import bot; print(bot.PROXY_MIN_POOL, bot.PROXY_MAX_POOL)"],
            capture_output=True, text=True, env=env, cwd=os.getcwd())
        self.assertEqual(out.stdout.split(), ["150", "150"], out.stderr)



if __name__ == "__main__":
    unittest.main(verbosity=2)
