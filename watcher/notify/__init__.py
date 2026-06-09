"""Notification dispatch across configured channels."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Change, Monitor, PushSubscription
from . import push, webhook


async def dispatch(session: AsyncSession, monitor: Monitor, change: Change) -> None:
    """Fan out a detected change to the monitor's configured channels.

    "inbox" needs no action — the persisted Change *is* the inbox entry.
    """
    channels = set(monitor.notify_channels or ["inbox"])

    payload = {
        "monitor_id": monitor.id,
        "monitor_name": monitor.name,
        "url": monitor.url,
        "change_id": change.id,
        "change_type": change.change_type.value,
        "summary": change.summary,
        "magnitude": round(change.magnitude, 4),
        "detected_at": change.detected_at.isoformat(),
    }

    if "webhook" in channels and monitor.webhook_url:
        await webhook.send(monitor.webhook_url, payload)

    if "push" in channels:
        subs = (
            await session.execute(
                select(PushSubscription).where(PushSubscription.user_id == monitor.user_id)
            )
        ).scalars().all()
        await push.send_to_all(
            subs,
            title=f"Change: {monitor.name}",
            body=change.summary,
            url=f"/monitors/{monitor.id}",
        )
