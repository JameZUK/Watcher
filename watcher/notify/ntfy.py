"""ntfy.sh (or self-hosted) push delivery via a per-user topic."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.ntfy")


async def send(server: str | None, topic: str | None, *, title: str, body: str, url: str | None) -> bool:
    if not (server and topic):
        return False
    # Header values must be latin-1 safe (ntfy reads Title/Click from headers).
    headers = {"Title": title.encode("ascii", "replace").decode()}
    if url:
        headers["Click"] = url.encode("ascii", "replace").decode()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{server.rstrip('/')}/{topic}", content=body.encode("utf-8"), headers=headers)
        return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("ntfy send failed: %s", exc)
        return False
