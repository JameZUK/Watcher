"""Snapshot retention / pruning.

Keeps the most recent N snapshots and anything within M days, but ALWAYS
preserves snapshots referenced by a Change (change-point snapshots), so the
diff history stays intact. Orphaned blobs are garbage-collected afterwards.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import Change, Monitor, Snapshot, utcnow
from . import blobs


async def prune() -> int:
    """Prune old snapshots across all monitors. Returns count removed."""
    removed = 0
    cutoff = utcnow() - timedelta(days=settings.retention_max_days)

    async with SessionLocal() as session:
        monitor_ids = (await session.execute(select(Monitor.id))).scalars().all()

        # Snapshot ids referenced by changes must be preserved.
        referenced: set[int] = set()
        for col in (Change.from_snapshot_id, Change.to_snapshot_id):
            referenced.update(
                (await session.execute(select(col))).scalars().all()
            )

        for mid in monitor_ids:
            snaps = (
                await session.execute(
                    select(Snapshot)
                    .where(Snapshot.monitor_id == mid)
                    .order_by(Snapshot.taken_at.desc())
                )
            ).scalars().all()

            for idx, snap in enumerate(snaps):
                if snap.id in referenced:
                    continue
                too_old = snap.taken_at < cutoff
                over_count = idx >= settings.retention_max_snapshots
                if too_old or over_count:
                    await session.delete(snap)
                    removed += 1

        await session.commit()

    await _gc_blobs()
    return removed


async def _gc_blobs() -> None:
    """Delete blob files no longer referenced by any snapshot or change."""
    async with SessionLocal() as session:
        live: set[str] = set()
        for col in (Snapshot.html_blob, Snapshot.screenshot_blob, Change.diff_blob):
            live.update(
                k for k in (await session.execute(select(col))).scalars().all() if k
            )

    for shard in settings.blobs_dir.iterdir() if settings.blobs_dir.exists() else []:
        if not shard.is_dir():
            continue
        for f in shard.iterdir():
            if f.name not in live:
                f.unlink(missing_ok=True)
