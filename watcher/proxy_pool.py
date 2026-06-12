"""A small shared proxy pool with health checks.

Admins paste a list of proxy URLs (one per line) in Settings. A scheduled job
tests each through a known endpoint and tracks which are healthy. Monitors that
have no explicit proxy but opt into the pool (`use_proxy_pool`) get a healthy
proxy round-robin at render time. State is in-process (refreshed by the job), so
the render engines never touch the DB to resolve a proxy.
"""

from __future__ import annotations

import asyncio
import itertools

import httpx

# Probe endpoint: cheap, returns the egress IP, and is itself behind nothing.
_PROBE_URL = "https://api.ipify.org"
_PROBE_TIMEOUT = 12.0
_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")

_pool: list[str] = []                 # configured proxy URLs (valid scheme)
_healthy: dict[str, bool] = {}        # url -> last health result
_cycle = itertools.cycle([])          # round-robin over healthy proxies


def parse_pool(text: str | None) -> list[str]:
    """One proxy URL per line; keep only ones with a supported proxy scheme. (No
    DNS/SSRF resolution here — these are admin-configured, trusted, and parse_pool
    runs in the async health job where blocking getaddrinfo would stall the loop.)"""
    out: list[str] = []
    for line in (text or "").splitlines():
        url = line.strip()
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme in _SCHEMES:
            out.append(url)
    return out


def _rebuild_cycle() -> None:
    global _cycle
    healthy = [u for u in _pool if _healthy.get(u)]
    _cycle = itertools.cycle(healthy)


def effective_proxy(monitor) -> str | None:
    """The proxy a render should use: the monitor's own, else a healthy pooled one
    when it opted in, else None. Called by the engines (no DB access)."""
    if monitor.proxy:
        return monitor.proxy
    if not getattr(monitor, "use_proxy_pool", False):
        return None
    try:
        return next(_cycle)
    except StopIteration:
        return None                              # pool empty / none healthy


def healthy_count() -> tuple[int, int]:
    """(healthy, total) for the status display."""
    return sum(1 for u in _pool if _healthy.get(u)), len(_pool)


async def _probe(url: str) -> bool:
    try:
        async with httpx.AsyncClient(proxy=url, timeout=_PROBE_TIMEOUT) as c:
            r = await c.get(_PROBE_URL)
            return r.status_code == 200
    except Exception:
        return False


async def health_check(app) -> None:
    """Refresh the pool from the admin setting and re-test every proxy. Run on a
    schedule. Cheap when the pool is empty."""
    global _pool, _healthy
    _pool = parse_pool(getattr(app, "proxy_pool", None))
    if not _pool:
        _healthy = {}
        _rebuild_cycle()
        return
    results = await asyncio.gather(*(_probe(u) for u in _pool))
    _healthy = dict(zip(_pool, results))
    _rebuild_cycle()
