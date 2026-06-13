"""Tiny in-process fixed-window rate limiter.

Single-process only (state lives in memory) — adequate for the default
single-worker uvicorn deployment. For multi-worker / replicated setups, also
rate-limit at the reverse proxy. Keyed by an arbitrary string (e.g. client IP).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)
_MAX_KEYS = 50_000  # bound memory under a flood of distinct keys


def allow(key: str, *, limit: int, window: int) -> bool:
    """Record a hit and return True if it is within ``limit`` per ``window``
    seconds, else False (over the limit)."""
    now = time.monotonic()
    cutoff = now - window
    with _lock:
        if len(_hits) > _MAX_KEYS:
            # Shed expired/empty keys first…
            for k in [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff]:
                _hits.pop(k, None)
            # …then HARD-cap against a flood of distinct *live* keys (e.g. an
            # attacker rotating IPv6 addresses): evict the oldest entries.
            # Evicting a live key just resets that key's counter — far cheaper
            # than unbounded memory growth.
            if len(_hits) > _MAX_KEYS:
                for k in sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0.0
                                )[: len(_hits) - _MAX_KEYS]:
                    _hits.pop(k, None)
        dq = _hits[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def count(key: str, *, window: int) -> int:
    """Current hit count for ``key`` within ``window`` seconds, WITHOUT recording a
    hit (for read-only displays like the status page)."""
    cutoff = time.monotonic() - window
    with _lock:
        return sum(1 for t in _hits.get(key, ()) if t >= cutoff)


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)


def client_ip(request) -> str:
    peer = getattr(getattr(request, "client", None), "host", None) or "unknown"
    # Behind a trusted single proxy, the socket peer is the proxy (so every client
    # collapses to one rate-limit bucket → one attacker locks everyone out). When
    # explicitly enabled, key on the right-most X-Forwarded-For entry instead — the
    # IP the trusted proxy received from. Right-most (not left-most) because a client
    # can prepend arbitrary left-most values; the proxy appends the real peer last.
    from ..config import settings
    if settings.trust_proxy_headers:
        try:
            xff = request.headers.get("x-forwarded-for", "") or ""
        except Exception:
            xff = ""
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return peer
