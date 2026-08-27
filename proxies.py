"""
Proxy rotation, health tracking, and failover for the Multi-Sniper bot.

Supports:
  - Multiple proxies via comma-or-newline separated ``PROXY_URLS``
  - Round-robin rotation across healthy proxies
  - Live success/failure reporting from real checker traffic
  - Concurrent background health checks (lightweight GET probes)
  - Dead-proxy cooldown with automatic recovery
  - Backward compatibility with a single ``PROXY_URL``

Two objects matter to callers:

``ProxyPool``
    Owns the rotation and health state.

``ProxyProvider``
    A *callable* passed straight to the checkers. Calling it yields the next
    proxy URL (or ``None`` for a direct connection); the checkers then call
    ``report_success`` / ``report_failure`` with that same URL, so a failing
    proxy is benched within one request instead of waiting for the next
    periodic health sweep.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import aiohttp

log = logging.getLogger(__name__)

# A proxy is benched after this many consecutive failures.
FAILURE_THRESHOLD = 3


@dataclass
class ProxyHealth:
    """Track health statistics for one proxy."""

    url: str
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    total_requests: int = 0
    total_failures: int = 0

    @property
    def is_alive(self) -> bool:
        """A proxy is considered alive while failures stay below the threshold."""
        return self.consecutive_failures < FAILURE_THRESHOLD

    @property
    def failure_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_failures / self.total_requests

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.last_success_time = time.monotonic()
        self.total_requests += 1

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_time = time.monotonic()
        self.total_requests += 1
        self.total_failures += 1


class ProxyPool:
    """Round-robin proxy pool with health tracking and failover.

    Usage::

        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])
        proxy_url = pool.next()          # next healthy proxy
        pool.report_success(proxy_url)   # or pool.report_failure(proxy_url)

    ``allow_direct_fallback`` decides what happens when *every* proxy is
    benched. It defaults to ``False`` on purpose: someone who configured
    proxies usually did so to keep their real IP off the target sites, and
    silently switching to a direct connection would leak it. With the default,
    the pool resets its failure counters and keeps using proxies instead.
    """

    def __init__(
        self,
        proxies: list[str] | None = None,
        health_check_interval: float = 30.0,
        recovery_cooldown: float = 60.0,
        allow_direct_fallback: bool = False,
    ) -> None:
        self._proxies: list[ProxyHealth] = []
        self._by_url: dict[str, ProxyHealth] = {}
        self._index: int = 0
        self._health_check_interval = max(1.0, health_check_interval)
        self._recovery_cooldown = max(0.0, recovery_cooldown)
        self._allow_direct_fallback = allow_direct_fallback
        self._last_health_check: float = 0.0

        for url in proxies or []:
            url = url.strip()
            if url and url not in self._by_url:
                health = ProxyHealth(url=url)
                self._proxies.append(health)
                self._by_url[url] = health

    # -- introspection ------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def alive_count(self) -> int:
        return sum(1 for p in self._proxies if p.is_alive)

    @property
    def urls(self) -> list[str]:
        return [p.url for p in self._proxies]

    # -- rotation -----------------------------------------------------------

    def _recover_cooled_down(self) -> None:
        """Un-bench proxies whose cooldown window has elapsed."""

        now = time.monotonic()
        for proxy in self._proxies:
            if (not proxy.is_alive
                    and now - proxy.last_failure_time > self._recovery_cooldown):
                proxy.consecutive_failures = 0

    def next(self) -> str | None:
        """Return the next healthy proxy URL, or ``None`` for a direct call."""

        if not self._proxies:
            return None

        self._recover_cooled_down()
        alive = [p for p in self._proxies if p.is_alive]

        if not alive:
            if self._allow_direct_fallback:
                return None
            # Every proxy is benched. Rather than leak the real IP, reset the
            # counters and keep cycling; the health sweep will re-bench the
            # ones that are genuinely dead.
            log.warning("All %d proxies are down; resetting and retrying them",
                        self.size)
            for proxy in self._proxies:
                proxy.consecutive_failures = 0
            alive = list(self._proxies)

        idx = self._index % len(alive)
        self._index = idx + 1
        return alive[idx].url

    # Allows the pool itself to be handed to the checkers as a proxy factory.
    __call__ = next

    # -- live reporting -----------------------------------------------------

    def report_success(self, url: str | None) -> None:
        proxy = self._by_url.get(url) if url else None
        if proxy is not None:
            proxy.record_success()

    def report_failure(self, url: str | None) -> None:
        proxy = self._by_url.get(url) if url else None
        if proxy is None:
            return
        was_alive = proxy.is_alive
        proxy.record_failure()
        if was_alive and not proxy.is_alive:
            log.warning(
                "Proxy %s benched after %d consecutive failures",
                _short_url(proxy.url), proxy.consecutive_failures,
            )

    def status_summary(self) -> str:
        if not self._proxies:
            return "no proxies configured (direct connection)"
        parts = []
        for p in self._proxies:
            state = "alive" if p.is_alive else "down"
            parts.append(f"{_short_url(p.url)} ({state}, {p.failure_rate:.0%} fail)")
        return ", ".join(parts)

    # -- health checking ----------------------------------------------------

    async def health_check(
        self,
        session: aiohttp.ClientSession,
        target_url: str = "https://api.mojang.com",
        timeout: float = 3.0,
        concurrency: int = 100,
    ) -> None:
        """Probe every proxy with one lightweight request each.

        Probes run in parallel but *bounded*: a large public list can hold
        thousands of proxies, and firing thousands of sockets at once would
        exhaust the connector (and the host's file descriptors) rather than
        finish faster.
        """

        if not self._proxies:
            return

        request_timeout = aiohttp.ClientTimeout(total=max(0.1, timeout))
        gate = asyncio.Semaphore(max(1, concurrency))

        async def probe(proxy: ProxyHealth) -> None:
            async with gate:
                try:
                    async with session.get(
                        target_url,
                        proxy=proxy.url,
                        timeout=request_timeout,
                        allow_redirects=False,
                    ) as response:
                        if response.status < 500:
                            proxy.record_success()
                        else:
                            proxy.record_failure()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - any failure means "unusable"
                    proxy.record_failure()

        await asyncio.gather(*(probe(p) for p in self._proxies))
        self._last_health_check = time.monotonic()

    def add(self, urls: list[str]) -> int:
        """Add proxies to the rotation, skipping ones already present."""

        added = 0
        for url in urls:
            url = url.strip()
            if url and url not in self._by_url:
                health = ProxyHealth(url=url)
                self._proxies.append(health)
                self._by_url[url] = health
                added += 1
        return added

    def keep_only(self, urls: list[str]) -> int:
        """Restrict the pool to ``urls`` (order preserved). Returns removals."""

        wanted = [u for u in urls if u in self._by_url]
        if not wanted:
            return 0
        removed = len(self._proxies) - len(wanted)
        self._proxies = [self._by_url[u] for u in wanted]
        self._by_url = {p.url: p for p in self._proxies}
        self._index = 0
        return max(0, removed)

    async def verify(
        self,
        session: aiohttp.ClientSession,
        target_url: str = "https://api.mojang.com",
        timeout: float = 6.0,
        concurrency: int = 100,
        keep_minimum: int = 1,
    ) -> tuple[int, int]:
        """Probe every proxy once and keep only the ones that answered.

        Unlike the running health rules - where a proxy is only benched after
        three consecutive failures, so a blip does not lose it - a startup
        probe is pass/fail: one chance, and a public list's corpses are gone
        before they can slow a single lookup.

        Returns ``(alive, removed)``. If nothing answers, nothing is removed:
        an empty pool would quietly become direct, unproxied traffic.
        """

        if not self._proxies:
            return 0, 0

        working = set(await probe_proxies(
            session, self.urls, target_url, timeout, concurrency))
        alive = [proxy for proxy in self._proxies if proxy.url in working]
        self._last_health_check = time.monotonic()

        if len(alive) < max(1, keep_minimum):
            for proxy in self._proxies:
                proxy.consecutive_failures = 0
            return len(alive), 0

        removed = len(self._proxies) - len(alive)
        for proxy in alive:
            proxy.record_success()
        self._proxies = alive
        self._by_url = {p.url: p for p in alive}
        self._index = 0
        return len(alive), removed

    def drop_dead(self, keep_minimum: int = 1) -> int:
        """Remove proxies that failed their probe. Returns how many were cut.

        Public proxy lists are mostly dead on arrival. Carrying thousands of
        corpses makes every rotation step walk past them, so after the first
        verification sweep the dead ones are dropped for good. If *nothing*
        answered, the pool is left untouched: an empty pool would silently
        turn into direct, unproxied traffic.
        """

        alive = [p for p in self._proxies if p.is_alive]
        if len(alive) < max(1, keep_minimum):
            return 0
        removed = len(self._proxies) - len(alive)
        if removed:
            self._proxies = alive
            self._by_url = {p.url: p for p in alive}
            self._index = 0
        return removed

    async def periodic_health_check(
        self,
        session: aiohttp.ClientSession,
        target_url: str = "https://api.mojang.com",
        concurrency: int = 100,
    ) -> None:
        """Background task that periodically refreshes proxy health."""

        while True:
            try:
                await asyncio.sleep(self._health_check_interval)
                now = time.monotonic()
                if now - self._last_health_check >= self._health_check_interval:
                    await self.health_check(
                        session, target_url, concurrency=concurrency)
                    log.debug("Proxy health check: %d/%d alive",
                              self.alive_count, self.size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Proxy health check error: %s", exc)


class ProxyProvider:
    """Callable proxy source handed to the checkers.

    Calling the instance returns the proxy URL to use for the next request
    (``None`` means "go direct"). The checkers report the outcome back through
    ``report_success`` / ``report_failure`` so pool health reflects real
    traffic, not just the periodic probe.
    """

    __slots__ = ("pool", "static_url")

    def __init__(
        self,
        pool: ProxyPool | None = None,
        static_url: str | None = None,
    ) -> None:
        self.pool = pool
        self.static_url = static_url

    def __call__(self) -> str | None:
        if self.pool is not None:
            return self.pool.next()
        return self.static_url

    def report_success(self, url: str | None) -> None:
        if self.pool is not None:
            self.pool.report_success(url)

    def report_failure(self, url: str | None) -> None:
        if self.pool is not None:
            self.pool.report_failure(url)

    @property
    def enabled(self) -> bool:
        return self.pool is not None or self.static_url is not None


# ---------------------------------------------------------------------------
# Proxy list parsing
# ---------------------------------------------------------------------------
#
# Proxy vendors hand out lists in half a dozen shapes. All of these mean the
# same thing and are all accepted:
#
#     http://user:pass@1.2.3.4:8080     already a URL
#     1.2.3.4:8080                      bare host:port
#     1.2.3.4:8080:user:pass            host:port:user:pass  (most common)
#     user:pass@1.2.3.4:8080            credentials, no scheme
#     user:pass:1.2.3.4:8080            credentials first
#
# Everything is normalised to a URL aiohttp and Playwright both accept.

# The file read at startup when no proxies are configured in the environment.
DEFAULT_PROXY_FILE = "proxies.txt"

_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_PORT_PATTERN = re.compile(r"^\d{1,5}$")
_COLON_FORM_PATTERN = re.compile(r"^([^:@/]+):(\d{1,5}):(.+)$")
# Characters that are already safe inside a URL's userinfo section.
_USERINFO_SAFE = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=-]*$")


def _encode_userinfo(value: str) -> str:
    """Percent-encode a username or password unless it already is."""

    if _USERINFO_SAFE.fullmatch(value):
        return value
    return quote(value, safe="")


def normalize_proxy(raw: str, default_scheme: str = "http") -> str | None:
    """Turn one line from a vendor's proxy list into a usable proxy URL.

    Returns ``None`` for blanks, comments, and anything that cannot be read
    as a proxy - callers log and skip those rather than crashing on one bad
    line in an otherwise good list.
    """

    if not isinstance(raw, str):
        return None
    item = raw.strip().strip(",")
    if not item or item.startswith("#"):
        return None

    explicit_scheme = bool(_SCHEME_PATTERN.match(item))
    if explicit_scheme:
        scheme, _, rest = item.partition("://")
        scheme = scheme.lower()
    else:
        scheme, rest = default_scheme, item
    if not rest:
        return None
    # SOCKS is deliberately NOT dropped here: silently ignoring it would
    # start the bot with no proxy at all and leak the host IP. It is kept so
    # startup validation can reject it loudly.

    credentials = ""
    # host:port:user:pass - the shape most vendors ship. Matched first because
    # the password may itself contain '@' or ':'.
    colon_form = _COLON_FORM_PATTERN.match(rest)
    if colon_form:
        host_port = f"{colon_form.group(1)}:{colon_form.group(2)}"
        credentials = colon_form.group(3)
        if ":" not in credentials:
            return None          # "host:port:something" is not user:pass
    elif "@" in rest:
        # A password may contain '@', so split on the LAST one.
        credentials, _, host_port = rest.rpartition("@")
    else:
        parts = rest.split(":")
        if len(parts) == 4 and _PORT_PATTERN.fullmatch(parts[3]):
            host_port = f"{parts[2]}:{parts[3]}"          # user:pass:host:port
            credentials = f"{parts[0]}:{parts[1]}"
        elif len(parts) <= 2:
            host_port = rest
        else:
            return None

    host, _, port = host_port.partition(":")
    if not host or (port and not _PORT_PATTERN.fullmatch(port)):
        return None
    if not port and not explicit_scheme:
        # "garbage" is a typo, not a proxy. A port-less entry is only honoured
        # when the scheme was spelled out (http://proxy.example.com).
        return None

    if credentials:
        user, _, password = credentials.partition(":")
        if not user:
            return None
        encoded = _encode_userinfo(user)
        if password:
            encoded += ":" + _encode_userinfo(password)
        return f"{scheme}://{encoded}@{host_port}"
    return f"{scheme}://{host_port}"


def parse_proxy_list(raw: str) -> list[str]:
    """Parse a comma-or-newline separated proxy list, dropping duplicates.

    Entries are normalised (see ``normalize_proxy``), so a raw vendor list
    can be pasted into ``PROXY_URLS`` or ``proxies.txt`` unchanged.
    """

    if not raw:
        return []
    proxies: list[str] = []
    seen: set[str] = set()
    skipped = 0
    for item in raw.replace("\r", "\n").replace("\n", ",").split(","):
        stripped = item.strip()
        if not stripped or stripped.startswith("#"):
            continue
        normalized = normalize_proxy(stripped)
        if normalized is None:
            skipped += 1
            # Show the entry without whatever precedes an '@', so a malformed
            # line never spills credentials into the log.
            log.warning("Ignoring unreadable proxy entry: %s",
                        stripped.rpartition("@")[2][:40])
            continue
        if normalized not in seen:
            seen.add(normalized)
            proxies.append(normalized)
    if skipped:
        log.warning("%d proxy entr%s could not be parsed and %s skipped",
                    skipped, "y" if skipped == 1 else "ies",
                    "was" if skipped == 1 else "were")
    return proxies


def load_proxy_file(path: str = DEFAULT_PROXY_FILE) -> list[str]:
    """Read a proxy list from a file, one per line (or comma separated).

    A missing file is not an error: it simply means no proxies are configured
    this way. Blank lines and ``#`` comments are ignored.
    """

    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.warning("Could not read proxy file %s: %s", path, exc)
        return []

    proxies = parse_proxy_list(raw)
    if proxies:
        log.info("Loaded %d prox%s from %s",
                 len(proxies), "y" if len(proxies) == 1 else "ies",
                 os.path.basename(path))
    return proxies


def _short_url(url: str) -> str:
    """Shorten a proxy URL for display, never revealing credentials."""

    try:
        parsed = urlsplit(url)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except ValueError:
        return url[:40]


# Public alias: other modules render proxies for logs and errors with this,
# which never reveals the credentials embedded in a proxy URL.
short_proxy_url = _short_url


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


async def probe_proxies(
    session: aiohttp.ClientSession,
    urls: list[str],
    target_url: str = "https://api.mojang.com",
    timeout: float = 6.0,
    concurrency: int = 100,
) -> list[str]:
    """Probe a batch of proxies and return the ones that answered.

    Bounded by ``concurrency``: a public list can hold tens of thousands of
    entries, and firing them all at once exhausts sockets rather than
    finishing sooner. Never raises - a proxy that errors simply did not pass.
    """

    if not urls:
        return []

    request_timeout = aiohttp.ClientTimeout(total=max(0.1, timeout))
    gate = asyncio.Semaphore(max(1, concurrency))

    async def probe(url: str) -> bool:
        async with gate:
            try:
                async with session.get(
                    target_url,
                    proxy=url,
                    timeout=request_timeout,
                    allow_redirects=False,
                ) as response:
                    return response.status < 500
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - any failure means "unusable"
                return False

    outcomes = await asyncio.gather(*(probe(url) for url in urls))
    return [url for url, ok in zip(urls, outcomes) if ok]


# ---------------------------------------------------------------------------
# Remote proxy lists
# ---------------------------------------------------------------------------
#
# Public lists are far too large to keep in the repository (the one this bot
# ships with is ~169,000 entries), and they go stale within hours. So the list
# is fetched from a URL at startup, cached locally, sampled down to a workable
# size, and verified before use.

# Where a cached copy of the remote list is kept, so a restart still has
# proxies when the download is slow or the host is offline.
DEFAULT_PROXY_CACHE = ".proxy-cache.txt"

# Google Drive "view" links serve an HTML page, not the file. This turns one
# into the direct-download endpoint automatically.
_DRIVE_FILE_PATTERN = re.compile(
    r"https?://drive\.google\.com/file/d/([\w-]+)")
_DRIVE_OPEN_PATTERN = re.compile(
    r"https?://drive\.google\.com/(?:open|uc)\?(?:[^#]*&)?id=([\w-]+)")


def direct_download_url(url: str) -> str:
    """Rewrite share links that serve HTML into direct file downloads."""

    for pattern in (_DRIVE_FILE_PATTERN, _DRIVE_OPEN_PATTERN):
        match = pattern.match(url.strip())
        if match:
            return ("https://drive.usercontent.google.com/download"
                    f"?id={match.group(1)}&export=download")
    # GitHub blob pages have a raw equivalent.
    if "github.com/" in url and "/blob/" in url:
        return url.replace("github.com/", "raw.githubusercontent.com/", 1) \
                  .replace("/blob/", "/", 1)
    return url


async def fetch_proxy_list(
    session: aiohttp.ClientSession,
    url: str,
    timeout: float = 20.0,
    max_bytes: int = 32 * 1024 * 1024,
) -> list[str]:
    """Download a proxy list. Returns [] on any failure - never raises."""

    if not url:
        return []
    target = direct_download_url(url)
    request_timeout = aiohttp.ClientTimeout(total=max(1.0, timeout))
    try:
        async with session.get(target, timeout=request_timeout) as response:
            if response.status != 200:
                log.warning("Proxy list download failed: HTTP %s",
                            response.status)
                return []
            # StreamReader.read(n) returns whatever is buffered, not n
            # bytes, so a multi-megabyte list must be drained in a loop.
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.content.iter_chunked(64 * 1024):
                chunks.append(chunk)
                received += len(chunk)
                if received >= max_bytes:
                    log.warning("Proxy list exceeded %d MB; using the first "
                                "part only", max_bytes // (1024 * 1024))
                    break
            body = b"".join(chunks)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not download the proxy list: %s", exc)
        return []

    text = body.decode("utf-8", errors="replace")
    if "<html" in text[:2048].lower():
        log.warning("The proxy list URL returned a web page, not a text file. "
                    "Use a direct/raw download link.")
        return []
    proxies = parse_proxy_list(text)
    log.info("Downloaded %d proxies from the remote list", len(proxies))
    return proxies


def read_proxy_cache(path: str = DEFAULT_PROXY_CACHE) -> tuple[list[str], float]:
    """Return cached proxies and the cache age in seconds (inf if missing)."""

    if not path or not os.path.exists(path):
        return [], float("inf")
    try:
        age = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        age = float("inf")
    return load_proxy_file(path), age


def write_proxy_cache(proxies: list[str],
                      path: str = DEFAULT_PROXY_CACHE) -> None:
    """Persist a downloaded list so the next start does not have to wait."""

    if not path or not proxies:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Cached proxy list - regenerated automatically.\n")
            handle.write("\n".join(proxies) + "\n")
    except OSError as exc:
        log.debug("Could not write the proxy cache: %s", exc)


def sample_proxies(proxies: list[str], limit: int,
                   seed: int | None = None) -> list[str]:
    """Take at most ``limit`` proxies, sampled evenly across the whole list.

    Public lists are ordered by whatever scraper produced them, so taking the
    first N would over-sample one source. Sampling spreads the choice across
    the file, which spreads it across countries and providers too.
    """

    if limit <= 0 or len(proxies) <= limit:
        return list(proxies)
    rng = random.Random(seed)
    return rng.sample(proxies, limit)


# Ports that are almost always SOCKS4/SOCKS5 rather than HTTP. aiohttp cannot
# speak SOCKS, so on a huge scraped list these entries are just probe budget
# thrown away. They are only skipped when the entry had no explicit scheme.
SOCKS_LIKELY_PORTS = frozenset({
    1080, 1081, 1082, 1085, 1090, 4145, 4153, 5678, 9050, 9051, 9150,
    10808, 10809, 32650, 61234,
})


def drop_socks_ports(proxies: list[str]) -> tuple[list[str], int]:
    """Split out entries whose port is a well-known SOCKS port."""

    kept, dropped = [], 0
    for url in proxies:
        try:
            port = urlsplit(url).port
        except ValueError:
            port = None
        if port in SOCKS_LIKELY_PORTS:
            dropped += 1
            continue
        kept.append(url)
    return kept, dropped


# ---------------------------------------------------------------------------
# CLI: verify a proxy list without starting the bot
# ---------------------------------------------------------------------------


async def _probe_one(session, url: str, target: str, timeout: float):
    """Time one proxy against a real target. Returns (ok, detail)."""

    request_timeout = aiohttp.ClientTimeout(total=max(0.5, timeout))
    started = time.monotonic()
    try:
        async with session.get(
            target, proxy=url, timeout=request_timeout, allow_redirects=False,
        ) as response:
            elapsed = (time.monotonic() - started) * 1000
            if response.status < 500:
                return True, f"HTTP {response.status} in {elapsed:.0f} ms"
            return False, f"HTTP {response.status} (proxy or target failing)"
    except asyncio.TimeoutError:
        return False, f"timed out after {timeout:.1f}s"
    except aiohttp.ClientHttpProxyError as exc:
        return False, f"proxy rejected the request: HTTP {exc.status}"
    except aiohttp.ClientProxyConnectionError:
        return False, "could not connect to the proxy"
    except aiohttp.ClientError as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _load_source(source: str, timeout: float) -> list[str]:
    """Load a proxy list from a file path or an http(s) URL."""

    if source.lower().startswith(("http://", "https://")):
        async with aiohttp.ClientSession() as session:
            return await fetch_proxy_list(session, source, timeout=timeout)
    return load_proxy_file(source)


async def _check_list(path: str, target: str, timeout: float,
                      limit: int = 0, concurrency: int = 100,
                      keep: str = "", skip_socks: bool = False,
                      want: int = 0) -> int:
    """Load a proxy list, validate it, and probe every entry concurrently."""

    import checkers  # local import: the CLI is not on the bot's hot path

    proxies = await _load_source(path, timeout=max(timeout, 20.0))
    if skip_socks:
        proxies, dropped = drop_socks_ports(proxies)
        if dropped:
            print(f"Skipped {dropped} entries on SOCKS-only ports.")
    reserve: list[str] = []
    if want and len(proxies) > want:
        # Keep the rest in reserve: a public list is mostly dead, so reaching
        # the target usually means testing far more than the target.
        proxies = sample_proxies(proxies, len(proxies))
        reserve = proxies[max(want * 20, concurrency):]
        proxies = proxies[:max(want * 20, concurrency)]
    elif limit and len(proxies) > limit:
        print(f"Sampling {limit} of {len(proxies)} proxies.")
        proxies = sample_proxies(proxies, limit)
    if not proxies:
        print(f"No usable proxies found in {path}.")
        print("Expected one proxy per line, for example:")
        print("    1.2.3.4:8080")
        print("    1.2.3.4:8080:user:pass")
        return 1

    print(f"{len(proxies)} prox{'y' if len(proxies) == 1 else 'ies'} "
          f"loaded from {path}\n")

    invalid = []
    for url in proxies:
        error = checkers.validate_proxy_url(url)
        if error:
            invalid.append((url, error))
    if invalid:
        print("Rejected before probing:")
        for url, error in invalid:
            print(f"  ✗ {_short_url(url)}  {error}")
        print()
    usable = [u for u in proxies if not checkers.validate_proxy_url(u)]
    if not usable:
        return 1

    print(f"Probing {target} through each proxy "
          f"(timeout {timeout:.0f}s, {concurrency} at a time)...\n")
    gate = asyncio.Semaphore(max(1, concurrency))

    async def probe(session, url):
        async with gate:
            return await _probe_one(session, url, target, timeout)

    started = time.monotonic()
    connector = aiohttp.TCPConnector(
        limit=max(1, concurrency) * 2, force_close=True)
    async with aiohttp.ClientSession(connector=connector) as session:
        outcomes = await asyncio.gather(*(
            probe(session, url) for url in usable))

    alive_urls = [url for url, (ok, _d) in zip(usable, outcomes) if ok]
    tested = len(usable)
    while want and len(alive_urls) < want and reserve:
        hit = max(len(alive_urls) / tested, 0.005)
        batch_size = min(len(reserve),
                         max(concurrency, int((want - len(alive_urls)) / hit)))
        batch, reserve = reserve[:batch_size], reserve[batch_size:]
        print(f"  ... {len(alive_urls)}/{want} found, testing "
              f"{len(batch)} more")
        async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=max(1, concurrency) * 2, force_close=True)) as extra:
            more = await asyncio.gather(*(probe(extra, url) for url in batch))
        tested += len(batch)
        alive_urls += [url for url, (ok, _d) in zip(batch, more) if ok]
        usable = usable + batch
        outcomes = list(outcomes) + list(more)
    elapsed = time.monotonic() - started

    alive_urls = []
    quiet = len(usable) > 60
    for url, (ok, detail) in zip(usable, outcomes):
        if ok:
            alive_urls.append(url)
        if ok or not quiet:
            print(f"  {'✓' if ok else '✗'} {_short_url(url):<40} {detail}")
    if quiet:
        print(f"  ({len(usable) - len(alive_urls)} dead entries not listed)")

    alive = len(alive_urls)
    print(f"\n{alive}/{len(usable)} alive, checked in {elapsed:.1f}s.")

    if keep and alive_urls:
        with open(keep, "w", encoding="utf-8") as handle:
            handle.write("# Verified working proxies, written by "
                         "python proxies.py --keep\n")
            handle.write("\n".join(alive_urls) + "\n")
        print(f"Wrote {alive} working prox"
              f"{'y' if alive == 1 else 'ies'} to {keep}")
    if alive == 0:
        print("None of them answered. Check the credentials, the ports, and "
              "whether your vendor allows this machine's IP.")
        return 1
    if alive < len(usable):
        print("The dead ones are benched automatically at runtime after 3 "
              "consecutive failures, so the bot will still work.")
    return 0


def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Check the proxy list the bot would use.")
    parser.add_argument(
        "path", nargs="?", default=DEFAULT_PROXY_FILE,
        help=f"proxy list file or http(s) URL (default: {DEFAULT_PROXY_FILE})")
    parser.add_argument(
        "--target", default="https://api.mojang.com",
        help="URL to fetch through each proxy")
    parser.add_argument(
        "--timeout", type=float, default=8.0, help="seconds per probe")
    parser.add_argument(
        "--limit", type=int, default=0,
        help="probe at most N proxies, sampled across the list")
    parser.add_argument(
        "--concurrency", type=int, default=100,
        help="how many proxies to probe at once (default: 100)")
    parser.add_argument(
        "--keep", default="",
        help="write the proxies that answered to this file")
    parser.add_argument(
        "--want", type=int, default=0,
        help="keep testing until this many proxies work (for a big list)")
    parser.add_argument(
        "--skip-socks", action="store_true",
        help="ignore entries on well-known SOCKS ports")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    try:
        return asyncio.run(_check_list(
            args.path, args.target, args.timeout, limit=args.limit,
            concurrency=args.concurrency, keep=args.keep,
            skip_socks=args.skip_socks, want=args.want))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(_main())
