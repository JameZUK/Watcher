"""Snapshot retention / pruning.

Keeps the most recent N snapshots and anything within M days, but ALWAYS
preserves snapshots referenced by a Change (change-point snapshots), so the
diff history stays intact. Orphaned blobs are garbage-collected afterwards.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, union

from ..config import settings
from ..db import SessionLocal
from ..models import Change, Snapshot, utcnow
from . import blobs  # noqa: F401  (kept for callers/back-compat)

# Don't GC a blob written in the last hour — guards against deleting a blob a
# concurrent check just stored but whose referencing row we read before it.
_GC_GRACE_SECONDS = 3600


async def prune() -> int:
    """Prune old snapshots across all monitors. Returns count removed."""
    removed = 0
    cutoff = utcnow() - timedelta(days=settings.retention_max_days)

    async with SessionLocal() as session:
        # Cap retained Changes per monitor FIRST, so the snapshots they pinned
        # can then be pruned below (bounds disk for always-changing pages).
        cap = settings.max_changes_per_monitor
        if cap:
            # One statement (like the snapshot prune below): rank each monitor's changes
            # newest-first and delete those beyond the cap — no per-monitor query loop.
            crn = func.row_number().over(
                partition_by=Change.monitor_id, order_by=Change.detected_at.desc()).label("crn")
            cranked = select(Change.id, crn).subquery()
            await session.execute(
                delete(Change).where(Change.id.in_(
                    select(cranked.c.id).where(cranked.c.crn > cap))))
            await session.commit()

        # Prune snapshots in ONE statement: rank each monitor's snapshots newest-
        # first and delete those beyond the keep-N cap OR older than the cutoff,
        # EXCEPT any still referenced by a Change (change-point snapshots). The DB
        # does the filtering — no per-monitor full scan into Python.
        referenced = union(
            select(Change.from_snapshot_id).where(Change.from_snapshot_id.is_not(None)),
            select(Change.to_snapshot_id).where(Change.to_snapshot_id.is_not(None)),
        )
        rn = func.row_number().over(
            partition_by=Snapshot.monitor_id, order_by=Snapshot.taken_at.desc()).label("rn")
        ranked = select(Snapshot.id, Snapshot.taken_at, rn).subquery()
        victims = select(ranked.c.id).where(
            or_(ranked.c.rn > settings.retention_max_snapshots, ranked.c.taken_at < cutoff),
            ranked.c.id.not_in(select(referenced.subquery())),
        )
        result = await session.execute(delete(Snapshot).where(Snapshot.id.in_(victims)))
        removed = result.rowcount or 0
        await session.commit()

    await _gc_blobs()
    return removed


async def _gc_blobs() -> None:
    """Delete blob files no longer referenced by any snapshot or change."""
    async with SessionLocal() as session:
        live: set[str] = set()
        blob_cols = (
            Snapshot.html_blob, Snapshot.screenshot_blob, Snapshot.screenshot_mobile_blob,
            Change.diff_blob, Change.visual_blob, Change.visual_mobile_blob,
        )
        for col in blob_cols:
            live.update(
                k for k in (await session.execute(select(col))).scalars().all() if k
            )
        # Whole-page captures store one blob per SECTION in these JSON arrays; only
        # the first section is also mirrored into screenshot_blob, so sections 2..N
        # are referenced ONLY here. Flatten them into the live set or the GC would
        # delete still-referenced section images (silent screenshot-history loss).
        section_cols = (Snapshot.screenshot_sections, Snapshot.screenshot_mobile_sections)
        for col in section_cols:
            for arr in (await session.execute(select(col))).scalars().all():
                if arr:
                    live.update(k for k in arr if k)

    def _sweep() -> None:
        now = time.time()
        root = settings.blobs_dir
        if not root.exists():
            return
        for shard in root.iterdir():
            if not shard.is_dir():
                continue
            for f in shard.iterdir():
                if f.name in live:
                    continue
                try:
                    if now - f.stat().st_mtime < _GC_GRACE_SECONDS:
                        continue  # too fresh — may be a just-written, referenced blob
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    # Filesystem walk + unlinks are blocking — run off the event loop.
    await asyncio.to_thread(_sweep)
