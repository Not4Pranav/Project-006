"""
Stress and robustness tests for Multi-Sniper.

These push the pipeline harder than the unit suites: random result soup,
hostile Discord behaviour, high concurrency, and leak checks.

Run with:   python test_stress.py
"""

import asyncio
import gc
import random
import unittest
from unittest.mock import MagicMock, patch

import bot as bot_module
import checkers
from test_bot import (
    ReactModeMixin,
    final_reply,
    make_bot,
    make_message,
    patch_workers,
)

ALL_STATUSES = [
    checkers.AVAILABLE, checkers.TAKEN, checkers.INVALID,
    checkers.BLOCKED, checkers.SKIPPED, checkers.ERROR,
    "unrecognised", "",
]

# Discord rejects messages over 2000 characters.
DISCORD_MESSAGE_LIMIT = 2000


class ReplyMode(ReactModeMixin):
    RESPONSE_MODE = "reply"


class TestFuzzRendering(ReplyMode, unittest.TestCase):
    """Rendering and verdicts must survive any result combination."""

    def test_random_result_sets(self):
        random.seed(1234)
        for _ in range(2000):
            results = [
                checkers.Result(platform, emoji, random.choice(ALL_STATUSES),
                                "x" * random.randint(0, 60))
                for platform, emoji in checkers.PLATFORMS
                if random.random() > 0.3
            ]
            text = bot_module.format_results(
                results, pending=random.random() > 0.5)
            self.assertTrue(text.strip())
            self.assertLess(len(text), DISCORD_MESSAGE_LIMIT)
            verdict = bot_module.SniperBot._verdict_emojis(results)
            self.assertTrue(verdict)

    def test_interpreters_never_raise_on_junk(self):
        junk = [None, "", "x" * 50_000, 123, {}, b"bytes", [], 4.5]
        interpreters = (
            checkers.interpret_minecraft, checkers.interpret_gunslol,
            checkers.interpret_steam, checkers.interpret_instagram,
            checkers.interpret_twitter, checkers.interpret_github,
            checkers.interpret_reddit,
        )
        valid = {checkers.AVAILABLE, checkers.TAKEN, checkers.INVALID,
                 checkers.BLOCKED, checkers.ERROR, checkers.SKIPPED}
        for interpret in interpreters:
            for status in (0, -1, 200, 204, 301, 400, 403, 404, 429, 500, 999):
                for payload in junk:
                    self.assertIn(interpret(status, payload), valid,
                                  f"{interpret.__name__}({status})")


class TestConcurrency(ReplyMode, unittest.TestCase):
    """Many simultaneous lookups must all be answered, with no leaks."""

    @staticmethod
    def _flaky_workers(*_args, **_kwargs):
        async def one(platform, emoji):
            await asyncio.sleep(random.uniform(0, 0.03))
            if random.random() < 0.1:
                raise RuntimeError("simulated checker crash")
            return checkers.Result(platform, emoji, random.choice(
                [checkers.AVAILABLE, checkers.TAKEN, checkers.BLOCKED]))

        return [one(p, e) for p, e in checkers.PLATFORMS]

    def test_two_hundred_concurrent_lookups(self):
        random.seed(99)

        async def run():
            b = make_bot()
            messages = [make_message(f"user{i % 37:04d}", user_id=i % 11)
                        for i in range(200)]
            with patch.object(checkers, "build_check_workers",
                              self._flaky_workers):
                await asyncio.gather(*(b.on_message(m) for m in messages))
            return b, messages

        saved = bot_module.USER_WINDOW_SECONDS
        bot_module.USER_WINDOW_SECONDS = 0.0001
        try:
            b, messages = asyncio.run(run())
        finally:
            bot_module.USER_WINDOW_SECONDS = saved

        answered = sum(1 for m in messages if m.reply.await_count)
        self.assertEqual(answered, len(messages))
        self.assertLessEqual(len(b._cache), bot_module.CACHE_MAX_ENTRIES)

    def test_no_tasks_are_left_pending(self):
        gc.collect()
        pending = [obj for obj in gc.get_objects()
                   if isinstance(obj, asyncio.Task) and not obj.done()]
        self.assertEqual(pending, [])


class TestHostileDiscord(ReplyMode, unittest.TestCase):
    """Discord misbehaving must never hang or crash a lookup."""

    def test_hanging_reply_is_bounded(self):
        async def never_returns(*_args, **_kwargs):
            await asyncio.sleep(30)

        b = make_bot(404)
        message = make_message("zxqw99182")
        message.reply = never_returns

        async def run():
            loop = asyncio.get_running_loop()
            started = loop.time()
            await b.on_message(message)
            return loop.time() - started

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, bot_module.RESPONSE_BUDGET_SECONDS + 1)

    def test_every_worker_crashing_still_answers(self):
        def exploding(*_args, **_kwargs):
            async def bad():
                raise ValueError("nope")
            return [bad() for _ in checkers.PLATFORMS]

        b = make_bot()
        message = make_message("vortex")
        with patch.object(checkers, "build_check_workers", exploding):
            asyncio.run(b.on_message(message))

        text = final_reply(message)
        self.assertTrue(text)
        self.assertIn("Unknown", text)
        self.assertNotIn("vortex", b._cache)   # never cache an outage

    def test_edit_failure_does_not_break_the_lookup(self):
        sent = MagicMock()
        sent.edit = MagicMock(side_effect=RuntimeError("gateway exploded"))

        async def fast():
            return checkers.Result("Minecraft", "🕹️", checkers.AVAILABLE)

        async def slow():
            await asyncio.sleep(0.2)
            return checkers.Result("guns.lol", "🔫", checkers.TAKEN)

        b = make_bot()
        message = make_message("vortex")
        message.reply = MagicMock(
            side_effect=lambda *a, **k: _completed(sent))
        with patch_workers(factory=lambda: [fast(), slow()]):
            asyncio.run(b.on_message(message))   # must not raise


class TestOddInput(ReplyMode, unittest.TestCase):
    """Unusual message content must be handled or ignored, never crash."""

    def test_weird_content(self):
        contents = [
            "a" * 32, "a" * 33, "a.b-c_d", "ＡＢＣ", "🕹️", "name\u202e",
            " ", "", "..", "---", "0", "a" * 31 + ".",
        ]
        b = make_bot(404)
        saved = bot_module.USER_WINDOW_SECONDS
        bot_module.USER_WINDOW_SECONDS = 0.0001
        try:
            for content in contents:
                asyncio.run(b.on_message(make_message(content)))
        finally:
            bot_module.USER_WINDOW_SECONDS = saved


def _completed(value):
    future = asyncio.get_event_loop().create_future()
    future.set_result(value)
    return future


if __name__ == "__main__":
    unittest.main(verbosity=2)
