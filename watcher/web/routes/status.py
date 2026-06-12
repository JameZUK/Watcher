"""Status / observability page: per-monitor health, render times, AI usage."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, Request
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import get_current_user
from ...config import settings
from ...db import get_session
from ...models import Monitor, Snapshot, SnapshotStatus, User
from .. import templates
from ..ratelimit import count as ratelimit_count

router = APIRouter()


def _blob_store_stats() -> tuple[int, int]:
    """(file count, total bytes) of the on-disk blob store. Walks the dir, so it's
    run off the event loop by the caller."""
    root = settings.blobs_dir
    n = total = 0
    if root.exists():
        for shard in root.iterdir():
            if shard.is_dir():
                for f in shard.iterdir():
                    try:
                        total += f.stat().st_size
                        n += 1
                    except OSError:
                        pass
    return n, total


# The whole-store walk is expensive; cache it so /status doesn't re-walk per load.
_BLOB_TTL = 300.0
_blob_cache: dict = {"at": 0.0, "count": 0, "bytes": 0}


async def _cached_blob_stats() -> tuple[int, int]:
    now = time.monotonic()
    if now - _blob_cache["at"] > _BLOB_TTL:
        count, total = await asyncio.to_thread(_blob_store_stats)
        _blob_cache.update(at=now, count=count, bytes=total)
    return _blob_cache["count"], _blob_cache["bytes"]


@router.get("/status")
async def status_page(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitors = (await session.execute(
        select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.name)
    )).scalars().all()
    mids = [m.id for m in monitors]

    # Per-monitor aggregates in ONE grouped query: total checks, successes, and
    # render-time avg/max (no N+1).
    agg: dict = {}
    if mids:
        ok_case = case((Snapshot.status == SnapshotStatus.ok, 1), else_=0)
        rows = (await session.execute(
            select(Snapshot.monitor_id, func.count(Snapshot.id), func.sum(ok_case),
                   func.avg(Snapshot.render_ms), func.max(Snapshot.render_ms))
            .where(Snapshot.monitor_id.in_(mids))
            .group_by(Snapshot.monitor_id)
        )).all()
        agg = {mid: (total, ok or 0, int(avg or 0), int(mx or 0))
               for mid, total, ok, avg, mx in rows}

    monitor_rows = []
    for m in monitors:
        total, ok, avg_ms, max_ms = agg.get(m.id, (0, 0, 0, 0))
        monitor_rows.append({
            "monitor": m,
            "checks": total,
            "ok": ok,
            "success_rate": (round(100 * ok / total) if total else None),
            "avg_ms": avg_ms,
            "max_ms": max_ms,
        })

    ai_used = ratelimit_count(f"ai:{user.id}", window=settings.ai_window_seconds)

    summary = {
        "monitors": len(monitors),
        "enabled": sum(1 for m in monitors if m.enabled),
        "paused": sum(1 for m in monitors if not m.enabled),
        "failing": sum(1 for m in monitors if (m.consecutive_failures or 0) > 0),
        "ai_used": ai_used,
        "ai_limit": settings.ai_max_calls,
        "ai_window_min": settings.ai_window_seconds // 60,
    }
    # The blob store is shared across all users, so only show its (cached) size to
    # admins — a normal user shouldn't see the deployment's total disk footprint.
    if user.is_admin:
        blob_count, blob_bytes = await _cached_blob_stats()
        summary["blob_count"] = blob_count
        summary["blob_mb"] = round(blob_bytes / 1_048_576, 1)
    return templates.TemplateResponse(
        request, "status.html",
        {"user": user, "rows": monitor_rows, "summary": summary},
    )
