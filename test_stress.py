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


class TestBusyChannel(ReplyMode, unittest.TestCase):
    """Many members, many different usernames, all at the same moment."""

    def setUp(self):
        super().setUp()
        saved = bot_module.COALESCE_DUPLICATES
        self.addCleanup(
            lambda: setattr(bot_module, "COALESCE_DUPLICATES", saved))
        bot_module.COALESCE_DUPLICATES = True

    @staticmethod
    def _deterministic_workers(*args, **_kwargs):
        """Results that depend only on the username being checked.

        If any lookup ever leaked another message's results, the expected
        text below would not match.
        """

        username = args[1]
        seed = sum(ord(char) for char in username)

        async def one(index, platform, emoji):
            await asyncio.sleep(random.uniform(0, 0.02))
            status = (checkers.AVAILABLE if (seed + index) % 3 == 0
                      else checkers.TAKEN)
            return checkers.Result(platform, emoji, status, username)

        return [one(i, p, e)
                for i, (p, e) in enumerate(checkers.PLATFORMS)]

    @classmethod
    def _expected_text(cls, username):
        seed = sum(ord(char) for char in username)
        return bot_module.format_results([
            checkers.Result(
                platform, emoji,
                checkers.AVAILABLE if (seed + i) % 3 == 0 else checkers.TAKEN)
            for i, (platform, emoji) in enumerate(checkers.PLATFORMS)
        ])

    def test_fifty_members_fifty_usernames(self):
        """Every member gets their own reply, on their own message."""

        random.seed(7)
        names = [f"member{i:03d}" for i in range(50)]

        async def run():
            b = make_bot()
            messages = [make_message(name, user_id=1000 + i)
                        for i, name in enumerate(names)]
            with patch.object(checkers, "build_check_workers",
                              self._deterministic_workers):
                await asyncio.gather(*(b.on_message(m) for m in messages))
            return b, messages

        b, messages = asyncio.run(run())

        for name, message in zip(names, messages):
            with self.subTest(username=name):
                # Answered as a reply to that member's own message.
                self.assertTrue(message.reply.await_count,
                                f"{name} was never answered")
                self.assertEqual(final_reply(message),
                                 self._expected_text(name))
        self.assertEqual(b._inflight, {})

    def test_distinct_usernames_are_never_coalesced(self):
        """Two members asking different things must not share an answer."""

        async def run():
            b = make_bot()
            slow = make_message("slowname", user_id=1)
            fast = make_message("fastname", user_id=2)

            def workers(*args, **_kwargs):
                username = args[1]
                delay = 0.25 if username == "slowname" else 0.0

                async def one(platform, emoji):
                    await asyncio.sleep(delay)
                    return checkers.Result(
                        platform, emoji,
                        checkers.AVAILABLE if username == "fastname"
                        else checkers.TAKEN)

                return [one(p, e) for p, e in checkers.PLATFORMS]

            with patch.object(checkers, "build_check_workers", workers):
                await asyncio.gather(b.on_message(slow), b.on_message(fast))
            return slow, fast

        slow, fast = asyncio.run(run())
        self.assertNotIn("Unavailable", final_reply(fast))
        self.assertNotIn("Available", final_reply(slow).replace(
            "Unavailable", ""))

    def test_same_username_from_many_members_runs_one_lookup(self):
        """A dozen members pasting one name must not hit the sites 12 times."""

        lookups = []

        def workers(*args, **_kwargs):
            lookups.append(args[1])

            async def one(platform, emoji):
                await asyncio.sleep(0.1)
                return checkers.Result(platform, emoji, checkers.AVAILABLE)

            return [one(p, e) for p, e in checkers.PLATFORMS]

        async def run():
            b = make_bot()
            messages = [make_message("sharedname", user_id=2000 + i)
                        for i in range(12)]
            with patch.object(checkers, "build_check_workers", workers):
                await asyncio.gather(*(b.on_message(m) for m in messages))
            return b, messages

        b, messages = asyncio.run(run())

        self.assertEqual(lookups, ["sharedname"])
        for message in messages:
            self.assertTrue(message.reply.await_count)
            self.assertIn("Available", final_reply(message))
        self.assertEqual(b._inflight, {})

    def test_coalescing_can_be_switched_off(self):
        bot_module.COALESCE_DUPLICATES = False
        lookups = []

        def workers(*args, **_kwargs):
            lookups.append(args[1])

            async def one(platform, emoji):
                await asyncio.sleep(0.05)
                return checkers.Result(platform, emoji, checkers.TAKEN)

            return [one(p, e) for p, e in checkers.PLATFORMS]

        async def run():
            b = make_bot()
            messages = [make_message("sharedname", user_id=3000 + i)
                        for i in range(4)]
            with patch.object(checkers, "build_check_workers", workers):
                await asyncio.gather(*(b.on_message(m) for m in messages))
            return messages

        messages = asyncio.run(run())
        self.assertEqual(len(lookups), 4)
        for message in messages:
            self.assertIn("Unavailable", final_reply(message))

    def test_followers_still_answer_when_the_shared_lookup_fails(self):
        def exploding(*_args, **_kwargs):
            async def bad():
                await asyncio.sleep(0.05)
                raise ValueError("every checker died")
            return [bad() for _ in checkers.PLATFORMS]

        async def run():
            b = make_bot()
            messages = [make_message("doomed", user_id=4000 + i)
                        for i in range(3)]
            with patch.object(checkers, "build_check_workers", exploding):
                await asyncio.gather(*(b.on_message(m) for m in messages))
            return b, messages

        b, messages = asyncio.run(run())
        for message in messages:
            self.assertIn("Unknown", final_reply(message))
        self.assertEqual(b._inflight, {})
        self.assertNotIn("doomed", b._cache)

    def test_mixed_flood_of_duplicates_and_new_names(self):
        random.seed(11)
        names = [f"name{random.randint(0, 9)}" for _ in range(120)]

        async def run():
            b = make_bot()
            messages = [make_message(name, user_id=5000 + i)
                        for i, name in enumerate(names)]
            with patch.object(checkers, "build_check_workers",
                              self._deterministic_workers):
                await asyncio.gather(*(b.on_message(m) for m in messages))
            return b, messages

        b, messages = asyncio.run(run())
        for name, message in zip(names, messages):
            with self.subTest(username=name):
                self.assertEqual(final_reply(message),
                                 self._expected_text(name))
        self.assertEqual(b._inflight, {})

    def test_one_member_flooding_does_not_block_everyone_else(self):
        """The per-user throttle is per user, never global."""

        async def run():
            b = make_bot()
            flooder = [make_message(f"spam{i}", user_id=1)
                       for i in range(bot_module.USER_MAX_CHECKS + 8)]
            others = [make_message(f"calm{i}", user_id=6000 + i)
                      for i in range(20)]
            with patch.object(checkers, "build_check_workers",
                              self._deterministic_workers):
                await asyncio.gather(
                    *(b.on_message(m) for m in flooder + others))
            return others

        others = asyncio.run(run())
        for message in others:
            self.assertTrue(message.reply.await_count)
            self.assertNotIn("cooldown", (final_reply(message) or "").lower())


class TestFallbackAgainstLocalServer(ReplyMode, unittest.TestCase):
    """End-to-end fallback test over a real socket, no mocks in the path.

    instantusername.com itself is not contacted: a local aiohttp server
    speaks the same JSON contract, so the real request layer, real session
    and real fan-out are all exercised.
    """

    def setUp(self):
        super().setUp()
        saved = dict(checkers.INSTANTUSERNAME_SERVICES)

        def restore():
            checkers.INSTANTUSERNAME_SERVICES.clear()
            checkers.INSTANTUSERNAME_SERVICES.update(saved)

        self.addCleanup(restore)

    @staticmethod
    async def _serve(hits):
        from aiohttp import web

        async def services(_request):
            return web.json_response({"services": [
                {"service": "Instagram",
                 "endpoint": "/check/instagram/{username}"},
                {"service": "Twitter", "endpoint": "/check/twitter/{username}"},
                {"service": "Discord", "endpoint": "/check/discord/{username}"},
                {"service": "Nowhere", "endpoint": "/check/nowhere/{username}"},
            ]})

        async def check(request):
            username = request.match_info["username"]
            hits.append((request.match_info["service"], username))
            return web.json_response({
                "available": username.startswith("free"),
                "url": f"https://example.invalid/{username}",
            })

        app = web.Application()
        app.router.add_get("/services.json", services)
        app.router.add_get("/check/{service}/{username}", check)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}"

    def test_live_fallback_roundtrip(self):
        hits = []

        async def run():
            import aiohttp as aio
            runner, base = await self._serve(hits)
            try:
                with patch.object(checkers, "INSTANTUSERNAME_BASE_URL", base), \
                        patch.object(checkers, "INSTANTUSERNAME_SERVICES_URL",
                                     f"{base}/services.json"):
                    async with aio.ClientSession() as session:
                        await checkers.refresh_instantusername_services(session)
                        # The catalogue is read from the wire.
                        self.assertEqual(
                            checkers.INSTANTUSERNAME_SERVICES["Discord"],
                            "discord")

                        free = await checkers.check_instantusername(
                            session, "Instagram", "\U0001F4F8", "freename")
                        taken = await checkers.check_instantusername(
                            session, "Twitter/X", "\U0001F426", "takenname")

                        # 30 fallback calls at once, all answered correctly.
                        many = await asyncio.gather(*(
                            checkers.check_instantusername(
                                session, "Instagram", "\U0001F4F8",
                                f"{'free' if i % 2 else 'busy'}{i}")
                            for i in range(30)))
                return free, taken, many
            finally:
                await runner.cleanup()

        free, taken, many = asyncio.run(run())
        self.assertEqual(free.status, checkers.AVAILABLE)
        self.assertEqual(taken.status, checkers.TAKEN)
        self.assertEqual(len(hits), 32)
        for i, result in enumerate(many):
            expected = checkers.AVAILABLE if i % 2 else checkers.TAKEN
            self.assertEqual(result.status, expected, i)

    def test_blocked_platform_is_rescued_end_to_end(self):
        hits = []

        async def blocked(*_args, **_kwargs):
            return checkers.Result(
                "Instagram", "\U0001F4F8", checkers.BLOCKED, "login wall")

        async def run():
            import aiohttp as aio
            runner, base = await self._serve(hits)
            try:
                with patch.object(checkers, "INSTANTUSERNAME_BASE_URL", base), \
                        patch.object(checkers, "check_instagram", blocked):
                    async with aio.ClientSession() as session:
                        workers = checkers.build_check_workers(
                            session, "freename42", timeout=3.0)
                        results = await asyncio.gather(*workers)
                return results
            finally:
                await runner.cleanup()

        results = asyncio.run(run())
        instagram = next(r for r in results if r.platform == "Instagram")
        self.assertEqual(instagram.status, checkers.AVAILABLE)
        self.assertIn(("instagram", "freename42"), hits)


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
