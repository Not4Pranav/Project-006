"""
Deep-audit tests: adversarial verification of the hot paths.

Everything here is OFFLINE (loopback servers and fake clocks only):

  - bounded page reads must accumulate up to the cap over chunked/slow bodies
  - ProxyPool rotation fuzzed against a reference model of the original
    full-scan semantics (cache must never change observable behaviour)
  - the hedged instantusername fallback across a timing/outcome grid
  - event-driven live-reply paint semantics
  - a real-socket parallel lookup proves fan-out latency is max(), not sum()
  - startup must never block on the remote proxy list
  - cancellation must leave no live tasks behind

Run with:  python test_audit.py
"""

import asyncio
import contextlib
import random
import socket
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web

import bot as bot_module
import checkers
import proxies as proxies_module
from proxies import ProxyHealth, ProxyPool
from test_bot import final_reply, make_bot, make_message


def free_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return handle.getsockname()[1]


async def start_server(app: web.Application):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Bounded page reads
# ---------------------------------------------------------------------------


class TestBoundedReadAccumulates(unittest.TestCase):
    """read(n) alone can stop mid-chunk; _fetch_page must accumulate."""

    def test_marker_after_a_slow_second_chunk_is_still_found(self):
        """The marker arrives 0.2s after the first chunk - old single-read
        code returned before it existed and misjudged the page."""

        async def handler(request: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(status=200)
            resp.content_type = "text/html"
            await resp.prepare(request)
            await resp.write(b"<html>" + b"x" * 40_000)  # no marker yet
            await asyncio.sleep(0.2)
            await resp.write(b"SENTINEL_MARKER" + b"y" * 1_000)
            await resp.write_eof()
            return resp

        async def run():
            app = web.Application()
            app.router.add_get("/page", handler)
            runner, base = await start_server(app)
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    started = time.monotonic()
                    status, text = await checkers._fetch_page(
                        session, f"{base}/page")
                    return status, text, time.monotonic() - started
            finally:
                await runner.cleanup()

        status, text, elapsed = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertIn("SENTINEL_MARKER", text,
                      "the read stopped before the buffered prefix was full")
        self.assertLess(elapsed, 2.0)

    def test_read_stops_at_the_cap_without_draining_a_huge_body(self):
        """A 5 MB tail must not be downloaded just to honour the cap."""

        tail_written = asyncio.Event()

        async def handler(request: web.Request) -> web.StreamResponse:
            resp = web.StreamResponse(status=200)
            resp.content_type = "text/html"
            await resp.prepare(request)
            await resp.write(b"<html>" + b"a" * 100_000)
            # A slow, huge tail: if the client drains it we hang here.
            await asyncio.sleep(3.0)
            await resp.write(b"b" * (5 * 1024 * 1024))
            tail_written.set()
            await resp.write_eof()
            return resp

        async def run():
            app = web.Application()
            app.router.add_get("/big", handler)
            runner, base = await start_server(app)
            try:
                timeout = aiohttp.ClientTimeout(total=8)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    started = time.monotonic()
                    status, text = await checkers._fetch_page(
                        session, f"{base}/big")
                    elapsed = time.monotonic() - started
                    return status, len(text.encode("utf-8", "replace")), \
                        elapsed, tail_written.is_set()
            finally:
                await runner.cleanup()

        status, nbytes, elapsed, drained_tail = asyncio.run(run())
        self.assertEqual(status, 200)
        self.assertLessEqual(nbytes, checkers.MAX_PAGE_BYTES + 1024)
        self.assertGreaterEqual(nbytes, checkers.MAX_PAGE_BYTES // 2)
        self.assertLess(elapsed, 2.5, "waited on the huge tail")
        self.assertFalse(drained_tail, "downloaded the whole body")

    def test_small_page_is_read_completely(self):
        async def handler(_request: web.Request) -> web.Response:
            return web.Response(text="tiny body", content_type="text/html")

        async def run():
            app = web.Application()
            app.router.add_get("/small", handler)
            runner, base = await start_server(app)
            try:
                async with aiohttp.ClientSession() as session:
                    return await checkers._fetch_page(session, f"{base}/small")
            finally:
                await runner.cleanup()

        status, text = asyncio.run(run())
        self.assertEqual((status, text), (200, "tiny body"))


# ---------------------------------------------------------------------------
# ProxyPool fuzz vs a reference model of the ORIGINAL semantics
# ---------------------------------------------------------------------------


class ReferencePool:
    """The pre-optimisation rotation: full scan + recovery on every call."""

    def __init__(self, pool: ProxyPool, recovery_cooldown: float,
                 allow_direct_fallback: bool):
        self.proxies: list[ProxyHealth] = pool._proxies
        self.index = pool._index
        self.recovery_cooldown = recovery_cooldown
        self.allow_direct_fallback = allow_direct_fallback

    def resync(self, pool: ProxyPool) -> None:
        self.proxies = pool._proxies
        self.index = pool._index

    def next(self, now: float) -> str | None:
        if not self.proxies:
            return None
        for proxy in self.proxies:
            if (not proxy.is_alive
                    and now - proxy.last_failure_time > self.recovery_cooldown):
                proxy.consecutive_failures = 0
        alive = [p for p in self.proxies if p.is_alive]
        if not alive:
            if self.allow_direct_fallback:
                return None
            for proxy in self.proxies:
                proxy.consecutive_failures = 0
            alive = list(self.proxies)
        idx = self.index % len(alive)
        self.index = idx + 1
        return alive[idx].url


class FakeClock:
    """Controllable monotonic clock for cooldown/recovery fuzzing."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestProxyPoolFuzzMatchesReference(unittest.TestCase):
    """Random operation storms must never change observable rotation."""

    @staticmethod
    def _run_fuzz(seed: int, cooldown: float, allow_direct: bool,
                  operations: int = 2_500, pool_size: int = 40) -> None:
        rng = random.Random(seed)
        clock = FakeClock()
        urls = [f"http://10.0.{i // 256}.{i % 256}:8080"
                for i in range(pool_size)]

        with patch.object(proxies_module, "time", clock), \
                patch.object(proxies_module.log, "warning", lambda *a, **k: None):
            pool = ProxyPool(urls, recovery_cooldown=cooldown,
                             allow_direct_fallback=allow_direct)
            ref = ReferencePool(pool, cooldown, allow_direct)
            extra_counter = 0

            for step in range(operations):
                op = rng.random()
                if op < 0.55:
                    got = pool.next()
                    want = ref.next(clock.now)
                    assert got == want, (
                        f"seed={seed} step={step}: pool={got!r} ref={want!r} "
                        f"cooldown={cooldown} direct={allow_direct}")
                elif op < 0.75:
                    target = rng.choice(urls) if rng.random() < 0.9 else \
                        "http://unknown:1"
                    pool.report_failure(target)
                elif op < 0.90:
                    pool.report_success(rng.choice(urls))
                elif op < 0.95:
                    extra_counter += 1
                    new_url = f"http://10.9.0.{extra_counter}:8080"
                    pool.add([new_url])
                    urls.append(new_url)
                    ref.resync(pool)
                elif op < 0.98:
                    if rng.random() < 0.5:
                        pool.keep_only(rng.sample(
                            urls, k=rng.randint(1, len(urls))))
                    else:
                        pool.keep_only([])          # no-op path
                    ref.resync(pool)
                else:
                    clock.advance(rng.choice([0.001, 0.05, 0.5, 61.0]))

            # Observable state must agree at the end, too.
            assert pool.size == len(ref.proxies), "pool membership diverged"
            assert set(pool.urls) == {p.url for p in ref.proxies}

    def test_fuzz_default_cooldown(self):
        for seed in range(6):
            self._run_fuzz(seed, cooldown=60.0, allow_direct=False)

    def test_fuzz_tiny_cooldown(self):
        for seed in range(6):
            self._run_fuzz(seed, cooldown=0.02, allow_direct=False)

    def test_fuzz_zero_cooldown(self):
        for seed in range(3):
            self._run_fuzz(seed, cooldown=0.0, allow_direct=False)

    def test_fuzz_direct_fallback_allowed(self):
        for seed in range(3):
            self._run_fuzz(seed, cooldown=0.5, allow_direct=True)

    def test_next_after_every_op_is_consistent(self):
        """A next() after each mutation catches cache staleness immediately."""

        clock = FakeClock()
        with patch.object(proxies_module, "time", clock), \
                patch.object(proxies_module.log, "warning", lambda *a, **k: None):
            urls = [f"http://p{i}:8080" for i in range(6)]
            pool = ProxyPool(urls, recovery_cooldown=0.1)
            ref = ReferencePool(pool, 0.1, False)
            rng = random.Random(77)
            for _ in range(500):
                choice = rng.randrange(4)
                if choice == 0:
                    pool.report_failure(rng.choice(urls))
                elif choice == 1:
                    pool.report_success(rng.choice(urls))
                elif choice == 2:
                    clock.advance(rng.random() * 0.3)
                got, want = pool.next(), ref.next(clock.now)
                self.assertEqual(got, want)


# ---------------------------------------------------------------------------
# Hedged fallback invariants
# ---------------------------------------------------------------------------


class TestFallbackHedgeInvariants(unittest.TestCase):
    """Every timing/outcome combination must obey the fallback contract."""

    def _run_case(self, primary_delay: float, primary_status: str,
                  fallback_delay: float, fallback_status: str):
        calls = []

        async def primary(*_args, **_kwargs):
            if primary_delay:
                await asyncio.sleep(primary_delay)
            return checkers.Result("GitHub", "x", primary_status, "primary")

        async def fallback(_session, platform, emoji, username, proxy=None):
            calls.append((platform, username))
            if fallback_delay:
                await asyncio.sleep(fallback_delay)
            return checkers.Result(platform, emoji, fallback_status,
                                   "instantusername")

        async def run():
            with patch.object(checkers, "check_instantusername", fallback):
                started = asyncio.get_running_loop().time()
                result = await checkers._with_fallback(
                    primary(), MagicMock(), "GitHub", "x", "vortex")
                elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(0.01)  # let loser-task callbacks land
            return result, elapsed

        with patch.dict(checkers.INSTANTUSERNAME_SERVICES,
                        {"GitHub": "github"}, clear=True):
            result, elapsed = asyncio.run(run())
        return result, elapsed, calls

    def test_grid_of_timings_and_outcomes(self):
        definitive = {checkers.AVAILABLE, checkers.TAKEN}
        hedge = checkers.FALLBACK_HEDGE_DELAY
        grace = checkers.FALLBACK_PRIMARY_GRACE
        for primary_delay in (0.0, 0.2, 0.8, 1.1, 1.45):
            for primary_status in (checkers.AVAILABLE, checkers.TAKEN,
                                   checkers.INVALID, checkers.BLOCKED,
                                   checkers.ERROR):
                for fallback_delay in (0.02, 0.3):
                    for fallback_status in (checkers.AVAILABLE, checkers.TAKEN,
                                            checkers.ERROR, checkers.BLOCKED):
                        result, elapsed, calls = self._run_case(
                            primary_delay, primary_status,
                            fallback_delay, fallback_status)

                        # Never crash, always a valid normalized status.
                        self.assertIn(result.status,
                                      {checkers.AVAILABLE, checkers.TAKEN,
                                       checkers.INVALID, checkers.BLOCKED,
                                       checkers.ERROR})

                        primary_definitive = primary_status in (
                            definitive | {checkers.INVALID})
                        fallback_definitive = fallback_status in definitive
                        if primary_definitive and primary_delay < hedge:
                            # Healthy primary: fallback never contacted.
                            self.assertEqual(calls, [],
                                             f"fallback ran: {primary_delay},"
                                             f"{primary_status}")
                            self.assertEqual(result.status, primary_status)
                        elif primary_definitive and fallback_definitive:
                            # A race between two definitive sources: the
                            # primary overrules inside the grace window,
                            # otherwise the fallback's earlier answer stands.
                            self.assertEqual(len(calls), 1)
                            if primary_delay < (hedge + fallback_delay + grace):
                                self.assertEqual(result.status, primary_status)
                            else:
                                self.assertEqual(result.status,
                                                 fallback_status)
                        elif primary_definitive:
                            # Useless fallback: the primary is the only real
                            # answer, so it is waited for whatever its delay.
                            self.assertEqual(result.status, primary_status)
                            self.assertEqual(len(calls), 1)
                        elif fallback_definitive:
                            # Inconclusive primary: definitive fallback wins.
                            self.assertEqual(result.status, fallback_status)
                            self.assertEqual(len(calls), 1)
                        else:
                            # Neither definitive: keep the primary's verdict.
                            self.assertEqual(result.status, primary_status)
                            self.assertEqual(len(calls), 1)

                        # Latency contract of the hedge:
                        if primary_delay > hedge:
                            if (fallback_definitive
                                    and not primary_definitive):
                                # Rescued: the answer lands when the fallback
                                # itself completes, and no later than the
                                # primary's arrival or the grace expiry -
                                # whichever comes first. A hanging primary is
                                # never paid in full.
                                fallback_done_at = hedge + fallback_delay
                                rescue_bound = max(
                                    fallback_done_at,
                                    min(primary_delay,
                                        fallback_done_at + grace)) + 0.15
                                self.assertLess(elapsed, rescue_bound)
                            else:
                                # No early rescue possible: finish when the
                                # slower of the two reports (+ slack).
                                self.assertLess(
                                    elapsed,
                                    max(primary_delay,
                                        hedge + fallback_delay) + 0.15)

    def test_hung_primary_rescued_quickly(self):
        result, elapsed, calls = self._run_case(
            5.0, checkers.BLOCKED, 0.05, checkers.AVAILABLE)
        self.assertEqual(result.status, checkers.AVAILABLE)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(len(calls), 1)

    def test_platform_without_service_skips_fallback(self):
        async def primary(*_args, **_kwargs):
            return checkers.Result("guns.lol", "x", checkers.BLOCKED)

        async def run():
            return await checkers._with_fallback(
                primary(), MagicMock(), "guns.lol", "x", "vortex")

        result = asyncio.run(run())
        self.assertEqual(result.status, checkers.BLOCKED)


# ---------------------------------------------------------------------------
# Event-driven live reply semantics
# ---------------------------------------------------------------------------


class TestLiveReplyEventDriven(unittest.TestCase):

    def _setup_reply_mode(self):
        saved = {name: getattr(bot_module, name) for name in
                 ("RESPONSE_MODE", "REPLY_ENABLED", "REACT_ENABLED",
                  "REPLY_EDIT_INTERVAL", "ENABLE_EXTRA_PLATFORMS")}

        def restore():
            for name, value in saved.items():
                setattr(bot_module, name, value)

        self.addCleanup(restore)
        bot_module.RESPONSE_MODE = "reply"
        bot_module.REPLY_ENABLED = True
        bot_module.REACT_ENABLED = False
        bot_module.REPLY_EDIT_INTERVAL = 0.3
        bot_module.ENABLE_EXTRA_PLATFORMS = False

    def test_paints_first_instantly_throttles_mid_and_finishes_complete(self):
        self._setup_reply_mode()
        bot = make_bot()
        message = make_message("vortex")

        results: list[checkers.Result] = []
        done = asyncio.Event()
        changed = asyncio.Event()
        paint_times: list[float] = []
        original_send = bot._send_reply
        original_edit = bot._edit_reply

        async def timed_send(*args, **kwargs):
            paint_times.append(time.monotonic())
            return await original_send(*args, **kwargs)

        async def timed_edit(*args, **kwargs):
            paint_times.append(time.monotonic())
            return await original_edit(*args, **kwargs)

        bot._send_reply = timed_send
        bot._edit_reply = timed_edit

        async def run():
            deadline = time.monotonic() + 4.0
            started = time.monotonic()
            reply_task = asyncio.create_task(bot._live_reply(
                message, results, done, deadline, changed))

            await asyncio.sleep(0.05)
            results.append(checkers.Result(
                "Minecraft", "x", checkers.AVAILABLE))
            changed.set()
            await asyncio.sleep(0.05)          # inside edit interval
            results.append(checkers.Result(
                "guns.lol", "x", checkers.TAKEN))
            changed.set()
            await asyncio.sleep(0.4)           # past the edit interval
            results.append(checkers.Result(
                "Discord", "x", checkers.BLOCKED))
            changed.set()
            await asyncio.sleep(0.4)

            results[:] = bot._fill_missing(results)
            changed.set()
            done.set()
            await reply_task
            return started

        started = asyncio.run(run())

        self.assertTrue(paint_times, "nothing was ever painted")
        self.assertLess(paint_times[0] - started, 0.15,
                        "first paint was not immediate")
        # Throttling: mid-stream paints respect the edit interval.
        for earlier, later in zip(paint_times, paint_times[1:]):
            self.assertGreaterEqual(later - earlier, 0.3 - 0.05)
        final = final_reply(message)
        self.assertIn("**Minecraft** — ✅ **Available**", final)
        self.assertIn("**guns.lol** — ❌ Unavailable", final)
        self.assertNotIn(bot_module.PENDING_LABEL, final)

    def test_no_change_events_means_no_busy_polling_edits(self):
        """Idle: the loop must not repaint while nothing happens."""

        self._setup_reply_mode()
        bot = make_bot()
        message = make_message("vortex")
        results: list[checkers.Result] = [checkers.Result(
            "Minecraft", "x", checkers.TAKEN)]
        done = asyncio.Event()
        changed = asyncio.Event()

        async def run():
            deadline = time.monotonic() + 1.0
            reply_task = asyncio.create_task(bot._live_reply(
                message, results, done, deadline, changed))
            await asyncio.sleep(0.5)           # idle: nothing changes
            edits_while_idle = message._sent.edit.await_count
            done.set()                         # final paint settles pending text
            changed.set()
            await reply_task
            return edits_while_idle

        edits_while_idle = asyncio.run(run())
        # One send for the initial paint, zero edits while idle, then exactly
        # one closing edit when the checks finish (pending markers clear).
        self.assertEqual(message.reply.await_count, 1)
        self.assertEqual(edits_while_idle, 0)
        self.assertEqual(message._sent.edit.await_count, 1)


# ---------------------------------------------------------------------------
# Real-socket parallel lookup: latency must be max(), not sum()
# ---------------------------------------------------------------------------


class TestRealSocketParallelLookup(unittest.TestCase):

    def test_fan_out_latency_is_the_slowest_check_not_the_sum(self):
        async def slow_minecraft(_request: web.Request) -> web.Response:
            await asyncio.sleep(1.2)
            return web.json_response({}, status=404)   # 404 -> AVAILABLE

        async def fast_gunslol(request: web.Request) -> web.Response:
            return web.Response(status=404)            # 404 -> AVAILABLE

        async def run():
            app_m = web.Application()
            app_m.router.add_get(
                "/users/{name}", slow_minecraft)
            app_g = web.Application()
            app_g.router.add_get("/{name}", fast_gunslol)
            runner_m, base_m = await start_server(app_m)
            runner_g, base_g = await start_server(app_g)
            real_fetch_page = checkers._fetch_page

            async def redirected_fetch_page(session, url, proxy=None,
                                            headers=None,
                                            max_bytes=checkers.MAX_PAGE_BYTES):
                url = url.replace("https://guns.lol", base_g)
                return await real_fetch_page(
                    session, url, proxy, headers, max_bytes)

            endpoints = (f"{base_m}/users/{{username}}",)
            try:
                timeout = aiohttp.ClientTimeout(total=5)
                connector = checkers.make_fast_connector(20, 10)
                async with aiohttp.ClientSession(
                        headers=checkers.BROWSER_HEADERS, timeout=timeout,
                        connector=connector) as session:
                    with patch.object(checkers, "MINECRAFT_ENDPOINTS",
                                      endpoints), \
                            patch.object(checkers, "_fetch_page",
                                         redirected_fetch_page):
                        started = time.monotonic()
                        results = await checkers.run_all_checks(
                            session, "vortex",
                            discord_mode="off",
                            enable_extra_platforms=False,
                            instantusername_fallback=False,
                        )
                        elapsed = time.monotonic() - started
                        return results, elapsed
            finally:
                await runner_m.cleanup()
                await runner_g.cleanup()

        results, elapsed = asyncio.run(run())
        by_platform = {r.platform: r for r in results}
        self.assertEqual(by_platform["Minecraft"].status,
                         checkers.AVAILABLE, by_platform["Minecraft"].detail)
        self.assertEqual(by_platform["guns.lol"].status,
                         checkers.AVAILABLE, by_platform["guns.lol"].detail)
        # Parallel: ~1.2s total, never ~1.2 + the rest stacked sequentially.
        self.assertLess(elapsed, 1.6, f"fan-out serialized: {elapsed:.2f}s")
        self.assertGreaterEqual(elapsed, 1.15)


# ---------------------------------------------------------------------------
# GitHub status-only contract
# ---------------------------------------------------------------------------


class TestGitHubStatusContract(unittest.TestCase):

    @staticmethod
    def _session(status: int):
        response = MagicMock()
        response.status = status
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=response)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.get = MagicMock(return_value=ctx)
        return session

    def test_status_only_contract(self):
        cases = {200: checkers.TAKEN, 404: checkers.AVAILABLE,
                 403: checkers.BLOCKED, 429: checkers.BLOCKED,
                 500: checkers.ERROR}
        for status, expected in cases.items():
            result = asyncio.run(
                checkers.check_github(self._session(status), "octocat"))
            self.assertEqual(result.status, expected, f"HTTP {status}")

    def test_invalid_name_makes_no_request(self):
        session = self._session(200)
        result = asyncio.run(checkers.check_github(session, "-bad"))
        self.assertEqual(result.status, checkers.INVALID)
        session.get.assert_not_called()


# ---------------------------------------------------------------------------
# Startup must never block on the remote proxy list
# ---------------------------------------------------------------------------


class TestStartupIsNonBlocking(unittest.TestCase):

    def test_setup_hook_returns_before_a_slow_list_download_finishes(self):
        release = asyncio.Event()

        async def slow_list(_request: web.Request) -> web.Response:
            await release.wait()
            return web.Response(text="http://10.1.1.1:8080\n")

        async def run():
            app = web.Application()
            app.router.add_get("/list.txt", slow_list)
            runner, base = await start_server(app)
            patch_kwargs = dict(
                PROXY_LIST_URL=f"{base}/list.txt",
                PROXY_CACHE_FILE="/nonexistent-cache-file.txt",
                PROXY_FILE="/nonexistent-proxy-file.txt",
                PROXY_URL=None, PROXY_URLS_RAW="",
                PROXY_MIN_POOL=1, PROXY_MAX_POOL=10,
                PROXY_VERIFY_CONCURRENCY=5, PROXY_VERIFY_TIMEOUT=1.0,
                PROXY_VERIFY_MAX_SECONDS=5.0,
                PROXY_PROBE_URL="http://example.invalid/",
                PREWARM_CONNECTIONS=False, INSTANTUSERNAME_FALLBACK=False,
                KEEPALIVE_PORT=0, DISCORD_CHECK_MODE="off",
            )
            bot = bot_module.SniperBot()
            try:
                with patch.multiple(bot_module, **patch_kwargs):
                    started = time.monotonic()
                    await bot.setup_hook()
                    setup_elapsed = time.monotonic() - started
                    # The list is still downloading: login must not wait.
                    self.assertLess(setup_elapsed, 1.0)
                    self.assertIsNotNone(bot.proxy_pool)
                    self.assertIsNotNone(bot._initial_health_task)

                    # Unblock the download; the pool fills in the background.
                    release.set()
                    await asyncio.wait_for(bot._initial_health_task, timeout=8)
                    return bot.proxy_pool.urls
            finally:
                release.set()
                await bot.close()
                await runner.cleanup()

        urls = asyncio.run(run())
        self.assertIn("http://10.1.1.1:8080", urls)

    def test_failed_download_degrades_gracefully(self):
        async def broken(_request: web.Request) -> web.Response:
            return web.Response(status=500)

        async def run():
            app = web.Application()
            app.router.add_get("/list.txt", broken)
            runner, base = await start_server(app)
            bot = bot_module.SniperBot()
            try:
                with patch.multiple(
                    bot_module,
                    PROXY_LIST_URL=f"{base}/list.txt",
                    PROXY_CACHE_FILE="/nonexistent-cache-file.txt",
                    PROXY_FILE="/nonexistent-proxy-file.txt",
                    PROXY_URL=None, PROXY_URLS_RAW="",
                    PREWARM_CONNECTIONS=False, INSTANTUSERNAME_FALLBACK=False,
                    KEEPALIVE_PORT=0, DISCORD_CHECK_MODE="off",
                ):
                    started = time.monotonic()
                    await bot.setup_hook()
                    self.assertLess(time.monotonic() - started, 1.0)
                    await asyncio.wait_for(bot._initial_health_task, timeout=8)
                    # Empty pool hands out direct connections; never raises.
                    return bot.proxy_pool.next() if bot.proxy_pool else None
            finally:
                await bot.close()
                await runner.cleanup()

        self.assertIsNone(asyncio.run(run()))


# ---------------------------------------------------------------------------
# Cancellation leaves nothing behind
# ---------------------------------------------------------------------------


class TestCancellationLeavesNoTasks(unittest.TestCase):

    def test_cancelling_a_streaming_lookup_cleans_up(self):
        async def run():
            bot = make_bot()

            async def hang(*_args, **_kwargs):
                await asyncio.sleep(10)
                return []

            with patch.object(checkers, "run_all_checks", hang):
                task = asyncio.create_task(bot._run_checks_with_deadline(
                    "vortex", 10.0))
                await asyncio.sleep(0.05)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.sleep(0.05)
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()]
            return pending

        pending = asyncio.run(run())
        self.assertEqual(pending, [], "tasks leaked after cancellation")

    def test_cancelling_a_hedged_fallback_cleans_up_both_sides(self):
        async def hanging_primary(*_args, **_kwargs):
            await asyncio.sleep(10)
            return checkers.Result("GitHub", "x", checkers.BLOCKED)

        async def hanging_fallback(*_args, **_kwargs):
            await asyncio.sleep(10)
            return checkers.Result("GitHub", "x", checkers.AVAILABLE)

        async def run():
            with patch.object(checkers, "check_instantusername",
                              hanging_fallback), \
                    patch.dict(checkers.INSTANTUSERNAME_SERVICES,
                               {"GitHub": "github"}, clear=True):
                task = asyncio.create_task(checkers._with_fallback(
                    hanging_primary(), MagicMock(), "GitHub", "x", "vortex"))
                await asyncio.sleep(checkers.FALLBACK_HEDGE_DELAY + 0.2)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.sleep(0.05)
            return [t for t in asyncio.all_tasks()
                    if t is not asyncio.current_task() and not t.done()]

        pending = asyncio.run(run())
        self.assertEqual(pending, [], "hedged tasks leaked after cancel")


# ---------------------------------------------------------------------------
# on_ready must not double-start the one-shot services
# ---------------------------------------------------------------------------


class TestOnReadyStartsNothingTwice(unittest.TestCase):

    def test_keepalive_and_tasks_are_not_duplicated(self):
        port = free_port()

        async def run():
            bot = bot_module.SniperBot()
            try:
                with patch.multiple(
                    bot_module,
                    PROXY_LIST_URL="", PROXY_FILE="/nonexistent.txt",
                    PROXY_URL=None, PROXY_URLS_RAW="",
                    PREWARM_CONNECTIONS=True, INSTANTUSERNAME_FALLBACK=True,
                    KEEPALIVE_PORT=port, DISCORD_CHECK_MODE="off",
                ):
                    await bot.setup_hook()  # noqa: keep structure visible
                    runner1 = bot._keepalive_runner
                    prewarm1 = bot._prewarm_task
                    services1 = bot._services_task
                    self.assertIsNotNone(runner1)

                    await bot.on_ready()          # first ready: banner only
                    self.assertIs(bot._keepalive_runner, runner1)
                    self.assertIs(bot._prewarm_task, prewarm1)
                    self.assertIs(bot._services_task, services1)

                    await bot.on_ready()          # reconnect: no-op
                    self.assertIs(bot._keepalive_runner, runner1)

                    # The single keepalive server still answers.
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                                f"http://127.0.0.1:{port}/health") as resp:
                            return resp.status
            finally:
                await bot.close()

        status = asyncio.run(run())
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
