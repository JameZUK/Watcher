"""Notification dispatch across configured channels."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..app_settings import (
    get_app_settings, get_pushover_token, get_telegram_token, get_user_ha_token,
)
from ..config import settings
from ..models import Change, Monitor, PushSubscription, User
from . import discord, email, homeassistant, ntfy, push, pushover, telegram, webhook


def _links(monitor: Monitor, change: Change | None = None) -> tuple[str, str]:
    """(relative, external) links for an alert. The relative path is for Web Push (the
    browser resolves the origin); the external link is the ABSOLUTE Watcher change page
    when our public URL is known — so tapping a Telegram/HA/etc. alert opens the diff in
    Watcher — else it falls back to the watched page."""
    cid = getattr(change, "id", None) if change is not None else None
    rel = f"/monitors/{monitor.id}" + (f"?change={cid}" if cid else "")
    base = settings.public_base
    return rel, (base + rel) if base else monitor.url


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


async def _deliver(session, app, monitor, user, change) -> bool:
    """Fan a single change out to the monitor's channels. Returns True if it was
    delivered somewhere — "inbox" always counts (the Change IS the inbox entry)
    so it can't be lost; external channels count only on success."""
    channels = set(monitor.notify_channels or ["inbox"])
    title, body = _titles(monitor, change)
    click, ext_link = _links(monitor, change)
    delivered = "inbox" in channels

    if "webhook" in channels and monitor.webhook_url:
        delivered |= await webhook.send(monitor.webhook_url, {
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
        delivered |= await push.send_to_all(subs, title=title, body=body, url=click)
    if user is not None:
        if "email" in channels:
            delivered |= await email.send(app, user.email, subject=title, body=f"{body}\n\n{ext_link}")
        if "telegram" in channels:
            delivered |= await telegram.send(get_telegram_token(app), user.telegram_chat_id, title=title, body=f"{body}\n{ext_link}")
        if "discord" in channels:
            delivered |= await discord.send(user.discord_webhook, title=title, body=body, url=ext_link)
        if "ntfy" in channels:
            delivered |= await ntfy.send(app.ntfy_server, user.ntfy_topic, title=title, body=body, url=ext_link)
        if "pushover" in channels:
            delivered |= await pushover.send(get_pushover_token(app), user.pushover_key, title=title, body=body, url=ext_link)
        if "homeassistant" in channels:
            delivered |= await homeassistant.send(user.ha_url, get_user_ha_token(user), user.ha_service, title=title, body=body, url=ext_link)
    return delivered


async def notify_monitor_alert(session: AsyncSession, monitor: Monitor, title: str, body: str) -> None:
    """Send an operational alert (failure/recovery/expiry) — not tied to a Change.
    Delivered immediately via push + the user's personal channels."""
    user = await session.get(User, monitor.user_id)
    app = await get_app_settings(session)
    channels = set(monitor.notify_channels or ["inbox"])
    click, ext_link = _links(monitor)
    if "push" in channels:
        subs = (await session.execute(
            select(PushSubscription).where(PushSubscription.user_id == monitor.user_id)
        )).scalars().all()
        await push.send_to_all(subs, title=title, body=body, url=click)
    if user is not None:
        if "email" in channels:
            await email.send(app, user.email, subject=title, body=f"{body}\n\n{ext_link}")
        if "telegram" in channels:
            await telegram.send(get_telegram_token(app), user.telegram_chat_id, title=title, body=f"{body}\n{ext_link}")
        if "discord" in channels:
            await discord.send(user.discord_webhook, title=title, body=body, url=ext_link)
        if "ntfy" in channels:
            await ntfy.send(app.ntfy_server, user.ntfy_topic, title=title, body=body, url=ext_link)
        if "pushover" in channels:
            await pushover.send(get_pushover_token(app), user.pushover_key, title=title, body=body, url=ext_link)
        if "homeassistant" in channels:
            await homeassistant.send(user.ha_url, get_user_ha_token(user), user.ha_service, title=title, body=body, url=ext_link)


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

    # Only mark notified if actually delivered; otherwise leave it for the
    # digest sweep to retry (so a transient channel outage can't drop alerts).
    if await _deliver(session, app, monitor, user, change):
        change.notified = True


async def run_digests(session: AsyncSession) -> int:
    """Send each user a batched digest of their un-notified changes (skipping
    those in quiet hours). Returns the number of users with a digest processed.

    Each user runs in its OWN fresh session so a failure (or a rollback) can't
    poison the others via expired ORM state."""
    from ..db import SessionLocal

    user_ids = (await session.execute(select(User.id).where(User.is_active))).scalars().all()
    sent = 0
    for uid in user_ids:
        try:
            async with SessionLocal() as s:
                user = await s.get(User, uid)
                if user is None or _in_quiet_hours(user):
                    continue
                app = await get_app_settings(s)
                rows = (await s.execute(
                    select(Change, Monitor)
                    .join(Monitor, Monitor.id == Change.monitor_id)
                    .where(Monitor.user_id == uid, Change.notified.is_(False))
                    .order_by(Change.detected_at.asc()).limit(200)
                )).all()
                if not rows:
                    continue
                lines = [f"• {m.name}: {c.ai_headline or c.summary}" for c, m in rows]
                body = f"{len(rows)} update(s) since your last digest:\n\n" + "\n".join(lines)
                subject = f"Watcher digest — {len(rows)} update(s)"

                # "Deliverable" must mirror the actual send guards (both the
                # user destination AND the app-side transport config) — otherwise
                # a partially-configured transport leaves changes un-consumed forever.
                has_external = bool(
                    (app.smtp_host and app.smtp_from and user.email)
                    or (get_telegram_token(app) and user.telegram_chat_id)
                    or (app.ntfy_server and user.ntfy_topic)
                    or user.discord_webhook
                    or (get_pushover_token(app) and user.pushover_key)
                    or (user.ha_url and user.ha_token_enc)
                )
                delivered = False
                if app.smtp_host:
                    delivered |= await email.send(app, user.email, subject=subject, body=body)
                delivered |= await telegram.send(get_telegram_token(app), user.telegram_chat_id, title=subject, body=body)
                delivered |= await ntfy.send(app.ntfy_server, user.ntfy_topic, title=subject, body=body, url=None)
                delivered |= await discord.send(user.discord_webhook, title=subject, body=body, url=None)
                delivered |= await pushover.send(get_pushover_token(app), user.pushover_key, title=subject, body=body)
                delivered |= await homeassistant.send(user.ha_url, get_user_ha_token(user), user.ha_service, title=subject, body=body)

                # Mark consumed when delivered OR when the user has no external
                # transport at all (they rely on the inbox — don't accumulate a
                # backlog and re-list the same items every hour).
                if delivered or not has_external:
                    for c, _m in rows:
                        c.notified = True
                    await s.commit()
                    if delivered:
                        sent += 1
                # else: leave un-notified to retry next sweep (transient outage).
        except Exception:  # noqa: BLE001
            logging.getLogger("watcher.notify").exception("digest failed for user %s", uid)
    return sent
