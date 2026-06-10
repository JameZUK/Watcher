"""Dashboard: overview of all monitors."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import get_current_user
from ...db import get_session
from ...models import Change, Monitor, Snapshot, SnapshotStatus, User
from .. import templates

router = APIRouter()


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

    # Apply tag + text filters (in Python — simpler than JSON SQL).
    ql = (q or "").strip().lower()
    monitors = [
        m for m in all_monitors
        if (not tag or tag in (m.tags or []))
        and (not ql or ql in (m.name or "").lower() or ql in (m.url or "").lower())
    ]

    # Unacknowledged change counts per monitor.
    rows = (
        await session.execute(
            select(Change.monitor_id, func.count(Change.id))
            .join(Monitor, Monitor.id == Change.monitor_id)
            .where(Monitor.user_id == user.id, Change.acknowledged.is_(False))
            .group_by(Change.monitor_id)
        )
    ).all()
    unacked = {mid: count for mid, count in rows}
    total_unacked = sum(unacked.values())

    # Latest thumbnail + blocked-status per monitor, via window functions so the
    # DB returns one row per monitor instead of scanning full snapshot history.
    thumbs: dict[int, int] = {}
    blocked: set[int] = set()
    if monitors:
        mids = [m.id for m in monitors]

        # Most recent ok snapshot with a screenshot (the thumbnail).
        thumb_rn = func.row_number().over(
            partition_by=Snapshot.monitor_id, order_by=Snapshot.taken_at.desc()
        ).label("rn")
        thumb_sub = (
            select(Snapshot.monitor_id.label("mid"), Snapshot.id.label("sid"), thumb_rn)
            .where(Snapshot.monitor_id.in_(mids),
                   Snapshot.status == SnapshotStatus.ok,
                   Snapshot.screenshot_blob.is_not(None))
            .subquery()
        )
        for mid, sid in (await session.execute(
            select(thumb_sub.c.mid, thumb_sub.c.sid).where(thumb_sub.c.rn == 1)
        )).all():
            thumbs[mid] = sid

        # Whether the absolute latest snapshot is an error.
        latest_rn = func.row_number().over(
            partition_by=Snapshot.monitor_id, order_by=Snapshot.taken_at.desc()
        ).label("rn")
        latest_sub = (
            select(Snapshot.monitor_id.label("mid"), Snapshot.status.label("status"), latest_rn)
            .where(Snapshot.monitor_id.in_(mids))
            .subquery()
        )
        for mid, status in (await session.execute(
            select(latest_sub.c.mid, latest_sub.c.status).where(latest_sub.c.rn == 1)
        )).all():
            if status == SnapshotStatus.error:
                blocked.add(mid)

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
