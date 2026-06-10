"""Discord delivery via a per-user incoming webhook URL."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.discord")


async def send(webhook_url: str | None, *, title: str, body: str, url: str | None) -> None:
    if not webhook_url:
        return
    content = f"**{title}**\n{body}" + (f"\n{url}" if url else "")
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(webhook_url, json={"content": content[:1900]})
    except Exception as exc:  # noqa: BLE001
        logger.warning("Discord send failed: %s", exc)
