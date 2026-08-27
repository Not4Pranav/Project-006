"""
Integration tests for Multi-Sniper - real sockets, no mocks in the path.

Every scenario here runs against local aiohttp servers standing in for the
outside world: real HTTP proxies, a real proxy-list download, a real
instantusername-style API. Nothing reaches the internet, so these are safe to
run anywhere, but the code under test uses its normal request layer, its
normal session and its normal fan-out.

Run with plain Python:     python test_integration.py
"""

import asyncio
import os
import random
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import aiohttp
from aiohttp import web

import bot as bot_module
import checkers
import proxies as proxies_module
from test_bot import final_reply, make_message


# ---------------------------------------------------------------------------
# Local servers
# ---------------------------------------------------------------------------


async def start_server(app: web.Application) -> tuple[web.AppRunner, int]:
    """Start an app on a free loopback port."""

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def start_proxy(seen: list | None = None) -> tuple[web.AppRunner, str]:
    """A forward proxy that answers every absolute-form request itself."""

    async def handler(request: web.Request) -> web.Response:
        if seen is not None:
            seen.append(str(request.url))
        return web.json_response({"id": "uuid", "name": "Notch"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner, port = await start_server(app)
    return runner, f"127.0.0.1:{port}"


async def start_list_server(text: str) -> tuple[web.AppRunner, str]:
    """Serves a proxy list at /list.txt."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text=text)

    app = web.Application()
    app.router.add_get("/list.txt", handler)
    runner, port = await start_server(app)
    return runner, f"http://127.0.0.1:{port}/list.txt"


class IntegrationCase(unittest.IsolatedAsyncioTestCase):
    """Tracks servers and temp files so every test cleans up after itself."""

    async def asyncSetUp(self) -> None:
        self._runners: list[web.AppRunner] = []
        self.workdir = tempfile.mkdtemp()

    async def asyncTearDown(self) -> None:
        for runner in self._runners:
            await runner.cleanup()

    async def proxy(self, seen: list | None = None) -> str:
        runner, url = await start_proxy(seen)
        self._runners.append(runner)
        return url

    async def list_server(self, text: str) -> str:
        runner, url = await start_list_server(text)
        self._runners.append(runner)
        return url

    def path(self, name: str) -> str:
        return os.path.join(self.workdir, name)


# ---------------------------------------------------------------------------
# proxies.txt -> pool -> live traffic
# ---------------------------------------------------------------------------


class TestProxyFileEndToEnd(IntegrationCase):
    async def test_every_request_goes_through_a_proxy_from_the_file(self):
        """The whole path: file formats -> pool -> per-request rotation."""

        seen: list[str] = []
        first = await self.proxy(seen)
        second = await self.proxy(seen)
        third = await self.proxy(seen)

        path = self.path("proxies.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# vendor list, three different formats\n")
            handle.write(f"{first}\n")                       # host:port
            handle.write(f"{second}:bob:s3cret\n")           # host:port:user:pass
            handle.write(f"http://{third}\n")                # full URL
            handle.write("\n")

        loaded = proxies_module.load_proxy_file(path)
        self.assertEqual(loaded, [
            f"http://{first}",
            f"http://bob:s3cret@{second}",
            f"http://{third}",
        ])

        pool = proxies_module.ProxyPool(loaded, allow_direct_fallback=False)
        provider = proxies_module.ProxyProvider(pool=pool)
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(
                checkers._fetch_status(
                    session, f"http://example.invalid/{i}", proxy=provider)
                for i in range(9)))

        # All nine requests were delivered by the proxies, spread evenly.
        self.assertEqual(len(seen), 9)
        self.assertEqual(pool.alive_count, 3)
        self.assertNotIn("s3cret", pool.status_summary())   # never leaked

    async def test_a_dead_proxy_is_benched_and_traffic_continues(self):
        seen: list[str] = []
        good = await self.proxy(seen)
        pool = proxies_module.ProxyPool(
            [f"http://{good}", "http://127.0.0.1:1"],
            allow_direct_fallback=False)
        provider = proxies_module.ProxyProvider(pool=pool)

        async with aiohttp.ClientSession() as session:
            for i in range(8):
                try:
                    await checkers._fetch_status(
                        session, f"http://example.invalid/{i}", proxy=provider)
                except aiohttp.ClientError:
                    pass                      # the dead one, reported below

        self.assertTrue(seen, "the working proxy was never used")
        self.assertEqual(pool.alive_count, 1)


# ---------------------------------------------------------------------------
# Remote list -> cache -> filter -> sample -> verify
# ---------------------------------------------------------------------------


class TestRemoteListPipeline(IntegrationCase):
    async def test_download_cache_filter_sample_and_verify(self):
        random.seed(11)
        working = [await self.proxy() for _ in range(6)]
        dead = [f"10.{i // 256 % 256}.{i % 256}.1:8080" for i in range(1200)]
        socks = [f"10.9.{i}.1:1080" for i in range(200)]
        entries = dead + socks + working
        random.shuffle(entries)
        url = await self.list_server("# public list\n" + "\n".join(entries))

        async with aiohttp.ClientSession() as session:
            downloaded = await proxies_module.fetch_proxy_list(
                session, url, timeout=10)
        self.assertEqual(len(downloaded), len(entries))

        cache = self.path(".proxy-cache.txt")
        proxies_module.write_proxy_cache(downloaded, cache)
        cached, age = proxies_module.read_proxy_cache(cache)
        self.assertEqual(cached, downloaded)
        self.assertLess(age, 60)

        filtered, dropped = proxies_module.drop_socks_ports(downloaded)
        self.assertEqual(dropped, len(socks))

        pool = proxies_module.ProxyPool(filtered, allow_direct_fallback=False)
        connector = aiohttp.TCPConnector(limit=200, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            alive, removed = await pool.verify(
                session, "http://example.invalid/", timeout=2,
                concurrency=200)

        self.assertEqual(alive, len(working))
        self.assertEqual(removed, len(filtered) - len(working))
        self.assertEqual(sorted(pool.urls),
                         sorted(f"http://{u}" for u in working))

    async def test_html_signin_page_is_not_parsed_as_proxies(self):
        app = web.Application()

        async def handler(_request):
            return web.Response(
                text="<!DOCTYPE html><html><body>Sign in</body></html>",
                content_type="text/html")

        app.router.add_get("/list.txt", handler)
        runner, port = await start_server(app)
        self._runners.append(runner)

        async with aiohttp.ClientSession() as session:
            got = await proxies_module.fetch_proxy_list(
                session, f"http://127.0.0.1:{port}/list.txt", timeout=5)
        self.assertEqual(got, [])


# ---------------------------------------------------------------------------
# The "at least N working" search
# ---------------------------------------------------------------------------


class TestMinimumPoolSearch(IntegrationCase):
    async def test_search_reaches_the_floor_from_a_mostly_dead_list(self):
        """Hardly any of the list works: keep hunting until N do."""

        random.seed(5)
        working = [f"http://{await self.proxy()}" for _ in range(12)]
        dead = [f"http://10.{i // 256 % 256}.{i % 256}.1:8080"
                for i in range(900)]
        everything = working + dead
        random.shuffle(everything)

        bot = bot_module.SniperBot.__new__(bot_module.SniperBot)
        bot.http_sniper = MagicMock()
        bot.proxy_provider = proxies_module.ProxyProvider(static_url=None)
        bot.proxy_provider.pool = proxies_module.ProxyPool(everything[:50])
        bot._proxy_reserve = everything[50:]
        bot._curated_proxies = set()

        with patch.multiple(
            bot_module,
            PROXY_MIN_POOL=10, PROXY_MAX_POOL=50,
            PROXY_VERIFY_MAX_SECONDS=60.0, PROXY_VERIFY_CONCURRENCY=200,
            PROXY_VERIFY_TIMEOUT=2.0,
            PROXY_PROBE_URL="http://example.invalid/",
        ):
            await bot._verify_proxies()

        pool = bot.proxy_provider.pool
        self.assertGreaterEqual(pool.size, 10)
        self.assertTrue(set(pool.urls) <= set(working),
                        "a dead proxy survived verification")

    async def test_curated_proxies_survive_a_failed_probe(self):
        """A proxy the operator configured is never dropped on one miss."""

        working = f"http://{await self.proxy()}"
        curated_dead = "http://127.0.0.1:2"

        bot = bot_module.SniperBot.__new__(bot_module.SniperBot)
        bot.http_sniper = MagicMock()
        bot.proxy_provider = proxies_module.ProxyProvider(static_url=None)
        bot.proxy_provider.pool = proxies_module.ProxyPool(
            [curated_dead, working, "http://127.0.0.1:3"])
        bot._proxy_reserve = []
        bot._curated_proxies = {curated_dead}

        with patch.multiple(
            bot_module,
            PROXY_MIN_POOL=1, PROXY_MAX_POOL=50,
            PROXY_VERIFY_MAX_SECONDS=30.0, PROXY_VERIFY_CONCURRENCY=10,
            PROXY_VERIFY_TIMEOUT=1.0,
            PROXY_PROBE_URL="http://example.invalid/",
        ):
            await bot._verify_proxies()

        urls = bot.proxy_provider.pool.urls
        self.assertIn(curated_dead, urls, "a configured proxy was dropped")
        self.assertIn(working, urls)
        self.assertNotIn("http://127.0.0.1:3", urls)   # remote, dead, dropped


# ---------------------------------------------------------------------------
# instantusername fallback
# ---------------------------------------------------------------------------


class TestFallbackEndToEnd(IntegrationCase):
    async def test_blocked_platform_is_rescued_over_a_real_socket(self):
        hits: list[str] = []

        async def check(request: web.Request) -> web.Response:
            hits.append(request.match_info["username"])
            return web.json_response({
                "available": request.match_info["username"].startswith("free"),
                "url": "https://example.invalid/",
            })

        app = web.Application()
        app.router.add_get("/check/{service}/{username}", check)
        runner, port = await start_server(app)
        self._runners.append(runner)
        base = f"http://127.0.0.1:{port}"

        async def blocked(*_args, **_kwargs):
            return checkers.Result(
                "Instagram", "\U0001F4F8", checkers.BLOCKED, "login wall")

        with patch.object(checkers, "INSTANTUSERNAME_BASE_URL", base), \
                patch.object(checkers, "check_instagram", blocked):
            async with aiohttp.ClientSession() as session:
                workers = checkers.build_check_workers(
                    session, "freename42", timeout=5.0)
                results = await asyncio.gather(*workers)

        instagram = next(r for r in results if r.platform == "Instagram")
        self.assertEqual(instagram.status, checkers.AVAILABLE)
        self.assertIn("freename42", hits)


# ---------------------------------------------------------------------------
# Whole-bot boot
# ---------------------------------------------------------------------------


class TestBotBoot(IntegrationCase):
    async def test_setup_hook_builds_a_verified_pool_and_answers(self):
        """Boot the bot for real, then answer a message through the pool."""

        seen: list[str] = []
        working = [await self.proxy(seen) for _ in range(3)]
        dead = [f"10.{i}.0.1:8080" for i in range(100)]
        entries = working + dead
        random.shuffle(entries)
        list_url = await self.list_server("\n".join(entries))

        with patch.multiple(
            bot_module,
            PROXY_LIST_URL=list_url,
            PROXY_CACHE_FILE=self.path(".proxy-cache.txt"),
            PROXY_FILE=self.path("missing.txt"),
            PROXY_URL=None, PROXY_URLS_RAW="",
            PROXY_MIN_POOL=3, PROXY_MAX_POOL=50,
            PROXY_VERIFY_CONCURRENCY=100, PROXY_VERIFY_TIMEOUT=2.0,
            PROXY_VERIFY_MAX_SECONDS=30.0,
            PROXY_PROBE_URL="http://example.invalid/",
            PREWARM_CONNECTIONS=False,
            INSTANTUSERNAME_FALLBACK=False,
            KEEPALIVE_PORT=0,
            DISCORD_CHECK_MODE="off",
            RESPONSE_MODE="reply", REPLY_ENABLED=True, REACT_ENABLED=False,
        ):
            bot = bot_module.SniperBot()
            try:
                await bot.setup_hook()
                # Verification runs in the background; wait for it.
                self.assertIsNotNone(bot._initial_health_task)
                await bot._initial_health_task

                pool = bot.proxy_pool
                self.assertIsNotNone(pool)
                self.assertEqual(
                    sorted(pool.urls),
                    sorted(f"http://{u}" for u in working),
                    "the pool should hold exactly the working proxies")

                # A real lookup, answered as a reply, checks routed via pool.
                message = make_message("Notch")
                async def one(platform, emoji):
                    await checkers._fetch_status(
                        bot.http_sniper, "http://example.invalid/x",
                        proxy=bot.proxy_provider)
                    return checkers.Result(
                        platform, emoji, checkers.AVAILABLE)

                with patch.object(
                        checkers, "build_check_workers",
                        lambda *a, **k: [one(p, e)
                                         for p, e in checkers.PLATFORMS]):
                    await bot.on_message(message)

                text = final_reply(message)
                self.assertIn("Minecraft: Available", text)
                self.assertGreaterEqual(
                    len(seen), len(checkers.PLATFORMS),
                    "checks did not go through the proxies")
            finally:
                await bot.close()

    async def test_boot_without_any_proxy_source_still_works(self):
        with patch.multiple(
            bot_module,
            PROXY_LIST_URL="", PROXY_FILE=self.path("missing.txt"),
            PROXY_URL=None, PROXY_URLS_RAW="",
            PREWARM_CONNECTIONS=False, INSTANTUSERNAME_FALLBACK=False,
            KEEPALIVE_PORT=0, DISCORD_CHECK_MODE="off",
            RESPONSE_MODE="reply", REPLY_ENABLED=True, REACT_ENABLED=False,
        ):
            bot = bot_module.SniperBot()
            try:
                await bot.setup_hook()
                self.assertIsNone(bot.proxy_pool)

                message = make_message("Notch")
                results = [checkers.Result(p, e, checkers.TAKEN)
                           for p, e in checkers.PLATFORMS]

                async def done(result):
                    return result

                with patch.object(checkers, "build_check_workers",
                                  lambda *a, **k: [done(r) for r in results]):
                    await bot.on_message(message)
                self.assertIn("Minecraft: Unavailable", final_reply(message))
            finally:
                await bot.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCommandLine(IntegrationCase):
    async def test_want_keeps_probing_until_the_target_is_met(self):
        random.seed(2)
        working = [await self.proxy() for _ in range(8)]
        dead = [f"10.{i // 256 % 256}.{i % 256}.1:8080" for i in range(600)]
        entries = working + dead
        random.shuffle(entries)
        source = self.path("big.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("\n".join(entries) + "\n")
        out = self.path("verified.txt")

        code = await proxies_module._check_list(
            source, "http://example.invalid/", 1.5,
            concurrency=200, keep=out, want=5)

        self.assertEqual(code, 0)
        kept = proxies_module.load_proxy_file(out)
        self.assertGreaterEqual(len(kept), 5)
        self.assertTrue(set(kept) <= {f"http://{u}" for u in working})

    async def test_url_source_and_socks_filter(self):
        working = await self.proxy()
        text = "\n".join([working, "10.0.0.1:1080", "10.0.0.2:4145"])
        url = await self.list_server(text)
        out = self.path("verified.txt")

        code = await proxies_module._check_list(
            url, "http://example.invalid/", 1.5, concurrency=10,
            keep=out, skip_socks=True)

        self.assertEqual(code, 0)
        self.assertEqual(proxies_module.load_proxy_file(out),
                         [f"http://{working}"])

    async def test_no_usable_entries_reports_failure(self):
        source = self.path("junk.txt")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write("# nothing here\nnot-a-proxy\n")
        code = await proxies_module._check_list(
            source, "http://example.invalid/", 1.0)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
