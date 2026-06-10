"""Discord delivery via a per-user incoming webhook URL."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.discord")


async def send(webhook_url: str | None, *, title: str, body: str, url: str | None) -> bool:
    if not webhook_url:
        return False
    content = f"**{title}**\n{body}" + (f"\n{url}" if url else "")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(webhook_url, json={"content": content[:1900]})
        return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discord send failed: %s", exc)
        return False
