"""Web Push (VAPID) delivery via pywebpush."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable

from ..config import settings
from ..models import PushSubscription


def enabled() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


async def send_to_all(
    subs: Iterable[PushSubscription], *, title: str, body: str, url: str
) -> bool:
    """Push to all subscriptions; return True if at least one was delivered."""
    if not enabled():
        return False
    from pywebpush import WebPushException, webpush

    data = json.dumps({"title": title, "body": body, "url": url})

    def _one(sub) -> bool:
        try:
            webpush(
                subscription_info={"endpoint": sub.endpoint,
                                   "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                data=data,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
            return True
        except (WebPushException, Exception):
            return False  # expired/invalid subscription — ignore

    # webpush() is a blocking network call — run off the event loop.
    delivered = False
    for sub in subs:
        if await asyncio.to_thread(_one, sub):
            delivered = True
    return delivered
