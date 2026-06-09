"""Web Push (VAPID) delivery via pywebpush."""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..config import settings
from ..models import PushSubscription


def enabled() -> bool:
    return bool(settings.vapid_private_key and settings.vapid_public_key)


async def send_to_all(
    subs: Iterable[PushSubscription], *, title: str, body: str, url: str
) -> None:
    if not enabled():
        return
    from pywebpush import WebPushException, webpush

    data = json.dumps({"title": title, "body": body, "url": url})
    for sub in subs:
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=data,
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException:
            # Subscription may be expired/invalid; ignore for now.
            continue
        except Exception:
            continue
