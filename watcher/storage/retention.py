"""Snapshot retention / pruning.

Keeps the most recent N snapshots and anything within M days, but ALWAYS
preserves snapshots referenced by a Change (change-point snapshots), so the
diff history stays intact. Orphaned blobs are garbage-collected afterwards.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta, timezone

from sqlalchemy import delete, select

from ..config import settings
from ..db import SessionLocal
from ..models import Change, Monitor, Snapshot, utcnow
from . import blobs  # noqa: F401  (kept for callers/back-compat)

# Don't GC a blob written in the last hour — guards against deleting a blob a
# concurrent check just stored but whose referencing row we read before it.
_GC_GRACE_SECONDS = 3600


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
                k for k in (await session.execute(select(col))).scalars().all() if k is not None
            )

        to_delete: list[int] = []
        for mid in monitor_ids:
            # Only id + taken_at — avoid materialising large text/blob columns.
            rows = (
                await session.execute(
                    select(Snapshot.id, Snapshot.taken_at)
                    .where(Snapshot.monitor_id == mid)
                    .order_by(Snapshot.taken_at.desc())
                )
            ).all()
            for idx, (sid, taken_at) in enumerate(rows):
                if sid in referenced:
                    continue
                # SQLite can hand back naive datetimes — treat as UTC so the
                # comparison against the tz-aware cutoff doesn't raise.
                if taken_at is not None and taken_at.tzinfo is None:
                    taken_at = taken_at.replace(tzinfo=timezone.utc)
                if (taken_at is not None and taken_at < cutoff) or idx >= settings.retention_max_snapshots:
                    to_delete.append(sid)

        if to_delete:
            # Bulk delete in chunks (SQLite caps bound parameters).
            for i in range(0, len(to_delete), 500):
                chunk = to_delete[i:i + 500]
                await session.execute(delete(Snapshot).where(Snapshot.id.in_(chunk)))
                removed += len(chunk)
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
