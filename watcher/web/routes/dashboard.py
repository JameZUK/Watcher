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
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitors = (
        await session.execute(
            select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.name)
        )
    ).scalars().all()

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

    # Latest screenshot per monitor, for card thumbnails ("rendered" previews).
    thumbs: dict[int, int] = {}
    blocked: set[int] = set()
    if monitors:
        mids = [m.id for m in monitors]
        snap_rows = (
            await session.execute(
                select(Snapshot.monitor_id, Snapshot.id)
                .where(
                    Snapshot.monitor_id.in_(mids),
                    Snapshot.status == SnapshotStatus.ok,
                    Snapshot.screenshot_blob.is_not(None),
                )
                .order_by(Snapshot.monitor_id, Snapshot.taken_at.desc())
            )
        ).all()
        for mid, sid in snap_rows:
            thumbs.setdefault(mid, sid)  # first row per monitor = most recent

        # Monitors whose most recent snapshot is an error (e.g. blocked).
        status_rows = (
            await session.execute(
                select(Snapshot.monitor_id, Snapshot.status)
                .where(Snapshot.monitor_id.in_(mids))
                .order_by(Snapshot.monitor_id, Snapshot.taken_at.desc())
            )
        ).all()
        seen: set[int] = set()
        for mid, status in status_rows:
            if mid in seen:
                continue
            seen.add(mid)
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
        },
    )
