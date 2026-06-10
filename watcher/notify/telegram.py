"""Telegram delivery via a shared bot token + per-user chat id."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.telegram")


async def send(token: str | None, chat_id: str | None, *, title: str, body: str) -> None:
    if not (token and chat_id):
        return
    text = f"*{_esc(title)}*\n{_esc(body)}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram send failed: %s", exc)


def _esc(s: str) -> str:
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s
