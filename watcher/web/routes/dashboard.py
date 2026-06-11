"""Dashboard: overview of all monitors."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import get_current_user
from ...db import get_session
from ...models import Change, Group, Monitor, Snapshot, SnapshotStatus, User
from .. import templates

router = APIRouter()


async def card_data(session, monitors):
    """Dashboard-card metadata for a list of monitors: (thumbs, blocked, unacked).
    Shared by the dashboard and the group view, so members render identically.
    Uses window functions so the DB returns one row per monitor."""
    thumbs: dict[int, int] = {}
    blocked: set[int] = set()
    unacked: dict[int, int] = {}
    if not monitors:
        return thumbs, blocked, unacked
    mids = [m.id for m in monitors]

    thumb_rn = func.row_number().over(
        partition_by=Snapshot.monitor_id, order_by=Snapshot.taken_at.desc()).label("rn")
    thumb_sub = (
        select(Snapshot.monitor_id.label("mid"), Snapshot.id.label("sid"), thumb_rn)
        .where(Snapshot.monitor_id.in_(mids), Snapshot.status == SnapshotStatus.ok,
               Snapshot.screenshot_blob.is_not(None)).subquery()
    )
    for mid, sid in (await session.execute(
            select(thumb_sub.c.mid, thumb_sub.c.sid).where(thumb_sub.c.rn == 1))).all():
        thumbs[mid] = sid

    latest_rn = func.row_number().over(
        partition_by=Snapshot.monitor_id, order_by=Snapshot.taken_at.desc()).label("rn")
    latest_sub = (
        select(Snapshot.monitor_id.label("mid"), Snapshot.status.label("status"), latest_rn)
        .where(Snapshot.monitor_id.in_(mids)).subquery()
    )
    for mid, status in (await session.execute(
            select(latest_sub.c.mid, latest_sub.c.status).where(latest_sub.c.rn == 1))).all():
        if status == SnapshotStatus.error:
            blocked.add(mid)

    for mid, count in (await session.execute(
            select(Change.monitor_id, func.count(Change.id))
            .where(Change.monitor_id.in_(mids), Change.acknowledged.is_(False))
            .group_by(Change.monitor_id))).all():
        unacked[mid] = count
    return thumbs, blocked, unacked


@router.get("/")
async def dashboard(
    request: Request,
    tag: str | None = None,
    q: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    all_monitors = (
        await session.execute(
            select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.name)
        )
    ).scalars().all()
    all_tags = sorted({t for m in all_monitors for t in (m.tags or [])})

    # Members of groups marked "hide from dashboard" are collapsed off the grid
    # (still visible inside the group).
    hidden_gids = set((await session.execute(
        select(Group.id).where(Group.user_id == user.id, Group.hide_members.is_(True)))
    ).scalars().all())

    # Apply tag + text filters + hidden-group exclusion (in Python).
    ql = (q or "").strip().lower()
    monitors = [
        m for m in all_monitors
        if m.group_id not in hidden_gids
        and (not tag or tag in (m.tags or []))
        and (not ql or ql in (m.name or "").lower() or ql in (m.url or "").lower())
    ]

    thumbs, blocked, unacked = await card_data(session, monitors)

    # Inbox total counts ALL unacked changes (incl. hidden-group monitors).
    total_unacked = (await session.execute(
        select(func.count(Change.id)).join(Monitor, Monitor.id == Change.monitor_id)
        .where(Monitor.user_id == user.id, Change.acknowledged.is_(False)))).scalar_one()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "monitors": monitors,
            "unacked": unacked,
            "total_unacked": total_unacked,
            "thumbs": thumbs,
            "blocked": blocked,
            "all_tags": all_tags,
            "active_tag": tag,
            "query": q or "",
            "groups": await _group_summaries(session, user),
        },
    )


async def _group_summaries(session, user):
    from .groups import list_groups
    return await list_groups(session, user)
