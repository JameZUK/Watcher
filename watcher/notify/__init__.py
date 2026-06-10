"""Notification dispatch across configured channels."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app_settings import get_app_settings, get_telegram_token
from ..models import Change, Monitor, PushSubscription, User
from . import discord, email, ntfy, push, telegram, webhook


def _in_quiet_hours(user: User | None) -> bool:
    if user is None or user.quiet_start is None or user.quiet_end is None:
        return False
    h = datetime.now(timezone.utc).hour
    s, e = user.quiet_start, user.quiet_end
    if s == e:
        return False
    return s <= h < e if s < e else (h >= s or h < e)


def _titles(monitor: Monitor, change: Change) -> tuple[str, str]:
    if change.ai_headline:
        return change.ai_headline, monitor.name
    return f"Change: {monitor.name}", change.summary


async def _deliver(session, app, monitor, user, change) -> None:
    """Fan a single change out to the monitor's channels + user destinations."""
    channels = set(monitor.notify_channels or ["inbox"])
    title, body = _titles(monitor, change)
    click = f"/monitors/{monitor.id}"

    if "webhook" in channels and monitor.webhook_url:
        await webhook.send(monitor.webhook_url, {
            "monitor_id": monitor.id, "monitor_name": monitor.name, "url": monitor.url,
            "change_id": change.id, "change_type": change.change_type.value,
            "summary": change.summary, "magnitude": round(change.magnitude, 4),
            "detected_at": change.detected_at.isoformat(),
            "ai_headline": change.ai_headline, "ai_category": change.ai_category,
            "ai_importance": change.ai_importance,
        })
    if "push" in channels:
        subs = (await session.execute(
            select(PushSubscription).where(PushSubscription.user_id == monitor.user_id)
        )).scalars().all()
        await push.send_to_all(subs, title=title, body=body, url=click)
    if user is not None:
        if "email" in channels:
            await email.send(app, user.email, subject=title, body=f"{body}\n\n{monitor.url}")
        if "telegram" in channels:
            await telegram.send(get_telegram_token(app), user.telegram_chat_id, title=title, body=f"{body}\n{monitor.url}")
        if "discord" in channels:
            await discord.send(user.discord_webhook, title=title, body=body, url=monitor.url)
        if "ntfy" in channels:
            await ntfy.send(app.ntfy_server, user.ntfy_topic, title=title, body=body, url=monitor.url)


async def dispatch(session: AsyncSession, monitor: Monitor, change: Change) -> None:
    """Deliver a detected change now, or defer it to the digest.

    High-importance changes always interrupt. Anything else is deferred (left
    un-notified for the digest job) when the user has digest mode on or is in
    their quiet hours. "inbox" needs no action — the Change *is* the inbox entry.
    """
    user = await session.get(User, monitor.user_id)
    app = await get_app_settings(session)
    high = (change.ai_importance or "medium") == "high"

    if not high and user is not None and (user.digest_enabled or _in_quiet_hours(user)):
        return  # change.notified stays False → swept into the next digest

    await _deliver(session, app, monitor, user, change)
    change.notified = True


async def run_digests(session: AsyncSession) -> int:
    """Send each user a batched digest of their un-notified changes (skipping
    those in quiet hours). Returns the number of users notified."""
    app = await get_app_settings(session)
    users = (await session.execute(select(User).where(User.is_active))).scalars().all()
    sent = 0
    for user in users:
        if _in_quiet_hours(user):
            continue
        rows = (await session.execute(
            select(Change, Monitor)
            .join(Monitor, Monitor.id == Change.monitor_id)
            .where(Monitor.user_id == user.id, Change.notified.is_(False))
            .order_by(Change.detected_at.desc()).limit(50)
        )).all()
        if not rows:
            continue
        lines = [f"• {m.name}: {c.ai_headline or c.summary}" for c, m in rows]
        body = f"{len(rows)} update(s) since your last digest:\n\n" + "\n".join(lines)
        subject = f"Watcher digest — {len(rows)} update(s)"
        if app.smtp_host:
            await email.send(app, user.email, subject=subject, body=body)
        await telegram.send(get_telegram_token(app), user.telegram_chat_id, title=subject, body=body)
        await ntfy.send(app.ntfy_server, user.ntfy_topic, title=subject, body=body, url=None)
        await discord.send(user.discord_webhook, title=subject, body=body, url=None)
        for c, _m in rows:
            c.notified = True
        sent += 1
    await session.commit()
    return sent
