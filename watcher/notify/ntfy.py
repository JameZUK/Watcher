"""ntfy.sh (or self-hosted) push delivery via a per-user topic."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.ntfy")


async def send(server: str | None, topic: str | None, *, title: str, body: str, url: str | None) -> None:
    if not (server and topic):
        return
    headers = {"Title": title}
    if url:
        headers["Click"] = url
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{server.rstrip('/')}/{topic}", content=body.encode("utf-8"), headers=headers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ntfy send failed: %s", exc)
