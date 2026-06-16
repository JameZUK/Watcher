"""Pushover delivery via a shared application token + per-user user/group key."""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.pushover")

_API = "https://api.pushover.net/1/messages.json"


async def send(token: str | None, user_key: str | None, *, title: str, body: str,
               url: str | None = None) -> bool:
    """Send a Pushover message. `token` is the app API token (shared, admin-set);
    `user_key` is the recipient's user or group key. Returns False on missing config
    or a non-2xx response."""
    if not (token and user_key):
        return False
    # Pushover limits: title ≤ 250, message ≤ 1024, supplementary url ≤ 512.
    data = {"token": token, "user": user_key, "title": title[:250], "message": (body or " ")[:1024]}
    if url:
        data["url"] = url[:512]
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_API, data=data)
        return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pushover send failed: %s", exc)
        return False
