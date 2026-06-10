"""APScheduler-based scheduling of monitor checks and retention pruning."""

from __future__ import annotations

import logging

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import Monitor
from ..notify import run_digests
from ..runner import check_monitor
from ..storage.retention import prune

log = logging.getLogger("watcher.scheduler")

scheduler = AsyncIOScheduler(timezone="UTC")

_PRUNE_JOB = "retention-prune"
_DIGEST_JOB = "notification-digest"


async def _run_digests() -> None:
    try:
        async with SessionLocal() as session:
            n = await run_digests(session)
        if n:
            log.info("sent %d notification digest(s)", n)
    except Exception:  # noqa: BLE001
        log.exception("digest run failed")


def _job_id(monitor_id: int) -> str:
    return f"monitor-{monitor_id}"


def _effective_interval(monitor: Monitor) -> int:
    return max(monitor.interval_seconds, settings.min_interval_seconds)


async def _run_check(monitor_id: int) -> None:
    try:
        await check_monitor(monitor_id)
    except Exception:  # noqa: BLE001
        log.exception("check failed for monitor %s", monitor_id)


def reschedule_monitor(monitor: Monitor) -> None:
    """Add/update/remove a monitor's job to match its current state."""
    jid = _job_id(monitor.id)
    if not monitor.enabled:
        try:
            scheduler.remove_job(jid)
        except JobLookupError:
            pass
        return
    # add_job(replace_existing=True) handles both the add and update cases.
    scheduler.add_job(
        _run_check,
        trigger=IntervalTrigger(seconds=_effective_interval(monitor)),
        args=[monitor.id],
        id=jid,
        jitter=settings.schedule_jitter_seconds,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )


def unschedule_monitor(monitor_id: int) -> None:
    try:
        scheduler.remove_job(_job_id(monitor_id))
    except JobLookupError:
        pass


def retune_interval(monitor_id: int, interval_seconds: int) -> None:
    """Change a job's interval in place (safe to call from within the running
    job — unlike remove+add)."""
    try:
        scheduler.reschedule_job(
            _job_id(monitor_id),
            trigger=IntervalTrigger(seconds=interval_seconds,
                                    jitter=settings.schedule_jitter_seconds),
        )
    except JobLookupError:
        pass


def trigger_now(monitor_id: int) -> None:
    """Fire a one-off immediate check (does not disturb the recurring job)."""
    scheduler.add_job(_run_check, args=[monitor_id], id=f"now-{monitor_id}",
                      replace_existing=True, max_instances=1)


async def schedule_all() -> None:
    """Load all enabled monitors and register their jobs."""
    async with SessionLocal() as session:
        monitors = (
            await session.execute(select(Monitor).where(Monitor.enabled.is_(True)))
        ).scalars().all()
    for m in monitors:
        reschedule_monitor(m)
    log.info("scheduled %d monitor(s)", len(monitors))


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.add_job(
            prune, trigger=IntervalTrigger(hours=24), id=_PRUNE_JOB,
            replace_existing=True, max_instances=1,
        )
        # Hourly digest sweep: batches deferred (non-high) changes per user,
        # skipping users currently in their quiet hours.
        scheduler.add_job(
            _run_digests, trigger=IntervalTrigger(hours=1), id=_DIGEST_JOB,
            replace_existing=True, max_instances=1, coalesce=True,
        )
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
