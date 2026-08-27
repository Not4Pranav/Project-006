"""
Proxy pool with rotation, health checking, and automatic failover.

Supports:
  - Multiple proxies via comma-separated PROXY_URLS in .env (or a file)
  - Round-robin rotation across healthy proxies
  - Automatic health checking via lightweight HEAD requests
  - Dead-proxy cooldown (skip unhealthy proxies for a window)
  - Backward compatibility with a single PROXY_URL
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp

log = logging.getLogger(__name__)


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
        """A proxy is considered alive if failures are below the threshold."""
        return self.consecutive_failures < 3

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

    Usage:
        pool = ProxyPool(["http://proxy1:8080", "http://proxy2:8080"])
        proxy_url = pool.next()          # get the next healthy proxy
        pool.report_success(proxy_url)   # or pool.report_failure(proxy_url)
    """

    def __init__(
        self,
        proxies: list[str] | None = None,
        health_check_interval: float = 30.0,
        recovery_cooldown: float = 60.0,
    ) -> None:
        self._proxies: list[ProxyHealth] = []
        self._index: int = 0
        self._lock = asyncio.Lock()
        self._health_check_interval = health_check_interval
        self._recovery_cooldown = recovery_cooldown
        self._last_health_check: float = 0.0
        self._initialized: bool = False

        if proxies:
            for url in proxies:
                url = url.strip()
                if url:
                    self._proxies.append(ProxyHealth(url=url))

    @property
    def size(self) -> int:
        return len(self._proxies)

    @property
    def alive_count(self) -> int:
        return sum(1 for p in self._proxies if p.is_alive)

    @property
    def urls(self) -> list[str]:
        return [p.url for p in self._proxies]

    def next(self) -> str | None:
        """Return the next healthy proxy URL, or None if no proxies."""
        if not self._proxies:
            return None

        now = time.monotonic()
        # Try to recover proxies that have been cooling down
        for proxy in self._proxies:
            if (not proxy.is_alive
                    and now - proxy.last_failure_time > self._recovery_cooldown):
                proxy.consecutive_failures = 0

        # Round-robin through alive proxies
        alive = [p for p in self._proxies if p.is_alive]
        if not alive:
            # All proxies are dead; reset and try any of them
            for p in self._proxies:
                p.consecutive_failures = 0
            alive = self._proxies

        if not alive:
            return None

        idx = self._index % len(alive)
        self._index = idx + 1
        return alive[idx].url

    def report_success(self, url: str) -> None:
        for proxy in self._proxies:
            if proxy.url == url:
                proxy.record_success()
                return

    def report_failure(self, url: str) -> None:
        for proxy in self._proxies:
            if proxy.url == url:
                proxy.record_failure()
                return

    def status_summary(self) -> str:
        if not self._proxies:
            return "no proxies configured (direct connection)"
        parts = []
        for p in self._proxies:
            state = "alive" if p.is_alive else "down"
            parts.append(f"{_short_url(p.url)} ({state}, {p.failure_rate:.0%} fail)")
        return ", ".join(parts)

    async def health_check(
        self,
        session: aiohttp.ClientSession,
        target_url: str = "https://api.mojang.com",
        timeout: float = 3.0,
    ) -> None:
        """Probe each proxy with a lightweight request to check connectivity."""
        if not self._proxies:
            return

        request_timeout = aiohttp.ClientTimeout(total=timeout)
        for proxy in self._proxies:
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
            except Exception:
                proxy.record_failure()

    async def periodic_health_check(
        self,
        session: aiohttp.ClientSession,
        target_url: str = "https://api.mojang.com",
    ) -> None:
        """Background task that periodically checks proxy health."""
        while True:
            try:
                await asyncio.sleep(self._health_check_interval)
                now = time.monotonic()
                if now - self._last_health_check >= self._health_check_interval:
                    self._last_health_check = now
                    await self.health_check(session, target_url)
                    log.debug(
                        "Proxy health check: %d/%d alive",
                        self.alive_count, self.size,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Proxy health check error: %s", exc)


def parse_proxy_list(raw: str) -> list[str]:
    """Parse a comma-or-newline separated proxy list from environment config."""
    if not raw:
        return []
    proxies = []
    for item in raw.replace("\n", ",").split(","):
        item = item.strip()
        if item:
            proxies.append(item)
    return proxies


def _short_url(url: str) -> str:
    """Shorten a proxy URL for display."""
    try:
        parsed = urlsplit(url)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}"
    except Exception:
        return url[:40]
