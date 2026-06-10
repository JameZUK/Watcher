"""Telegram delivery via a shared bot token + per-user chat id."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.telegram")


async def send(token: str | None, chat_id: str | None, *, title: str, body: str) -> bool:
    if not (token and chat_id):
        return False
    # Plain text (no parse_mode): title/body carry adversary-influenced page
    # content, so Markdown/HTML parsing would be an injection vector.
    text = f"{title}\n{body}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram send failed: %s", exc)
        return False
