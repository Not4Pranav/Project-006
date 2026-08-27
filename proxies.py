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
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

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
    ) -> None:
        """Probe every proxy concurrently with one lightweight request each."""

        if not self._proxies:
            return

        request_timeout = aiohttp.ClientTimeout(total=max(0.1, timeout))

        async def probe(proxy: ProxyHealth) -> None:
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

        # Concurrent: N proxies cost one timeout, not N timeouts.
        await asyncio.gather(*(probe(p) for p in self._proxies))
        self._last_health_check = time.monotonic()

    async def periodic_health_check(
        self,
        session: aiohttp.ClientSession,
        target_url: str = "https://api.mojang.com",
    ) -> None:
        """Background task that periodically refreshes proxy health."""

        while True:
            try:
                await asyncio.sleep(self._health_check_interval)
                now = time.monotonic()
                if now - self._last_health_check >= self._health_check_interval:
                    await self.health_check(session, target_url)
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


def parse_proxy_list(raw: str) -> list[str]:
    """Parse a comma-or-newline separated proxy list, dropping duplicates."""

    if not raw:
        return []
    proxies: list[str] = []
    seen: set[str] = set()
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            proxies.append(item)
    return proxies


def _short_url(url: str) -> str:
    """Shorten a proxy URL for display, never revealing credentials."""

    try:
        parsed = urlsplit(url)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except ValueError:
        return url[:40]
