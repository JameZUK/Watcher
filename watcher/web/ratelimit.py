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
            for k in [k for k, dq in _hits.items() if not dq or dq[-1] < cutoff]:
                _hits.pop(k, None)
        dq = _hits[key]
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


def reset(key: str) -> None:
    with _lock:
        _hits.pop(key, None)


def client_ip(request) -> str:
    return getattr(getattr(request, "client", None), "host", None) or "unknown"
