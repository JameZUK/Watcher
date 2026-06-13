"""APScheduler-based scheduling of monitor checks and retention pruning."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

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
_PROXY_JOB = "proxy-health"
_RESUME_JOB = "auto-resume"


async def _run_digests() -> None:
    try:
        async with SessionLocal() as session:
            n = await run_digests(session)
        if n:
            log.info("sent %d notification digest(s)", n)
    except Exception:  # noqa: BLE001
        log.exception("digest run failed")


async def _run_proxy_health() -> None:
    """Refresh + re-test the admin-configured proxy pool (no-op when empty)."""
    try:
        from ..app_settings import get_app_settings
        from ..proxy_pool import health_check, healthy_count
        async with SessionLocal() as session:
            app = await get_app_settings(session)
        await health_check(app)
        h, t = healthy_count()
        if t:
            log.info("proxy pool: %d/%d healthy", h, t)
    except Exception:  # noqa: BLE001
        log.exception("proxy health check failed")


def _job_id(monitor_id: int) -> str:
    return f"monitor-{monitor_id}"


def _effective_interval(monitor: Monitor) -> int:
    return max(monitor.interval_seconds, settings.min_interval_seconds)


# Monitors with a check currently queued or running, so the UI can show a live
# "Checking…" state (best-effort; a rare manual+scheduled overlap may clear early).
_inflight: set[int] = set()


def is_checking(monitor_id: int) -> bool:
    return monitor_id in _inflight


async def _run_check(monitor_id: int, manual: bool = False) -> None:
    _inflight.add(monitor_id)
    try:
        await check_monitor(monitor_id, manual=manual)
    except Exception:  # noqa: BLE001
        log.exception("check failed for monitor %s", monitor_id)
    finally:
        _inflight.discard(monitor_id)


def _spread_jitter(interval: int) -> int:
    """Jitter sized to the interval so monitors sharing an interval DEPHASE across the
    whole period instead of all firing on the same boundary (a thundering herd that
    starves the small render pool). A fixed ±60s on a 900s interval barely spread them."""
    return max(60, min(interval // 2, 1800))


def reschedule_monitor(monitor: Monitor) -> None:
    """Add/update/remove a monitor's job to match its current state."""
    jid = _job_id(monitor.id)
    if not monitor.enabled:
        try:
            scheduler.remove_job(jid)
        except JobLookupError:
            pass
        return
    interval = _effective_interval(monitor)
    # add_job(replace_existing=True) handles both the add and update cases.
    scheduler.add_job(
        _run_check,
        trigger=IntervalTrigger(seconds=interval),
        args=[monitor.id],
        id=jid,
        jitter=_spread_jitter(interval),
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
                                    jitter=_spread_jitter(interval_seconds)),
        )
    except JobLookupError:
        pass


def trigger_now(monitor_id: int, manual: bool = False) -> None:
    """Fire a one-off immediate check (does not disturb the recurring job). `manual`
    marks a user-initiated check, which runs even on a paused monitor and, on success,
    resumes it."""
    _inflight.add(monitor_id)   # reflect "checking" immediately, before the job starts
    scheduler.add_job(_run_check, args=[monitor_id, manual], id=f"now-{monitor_id}",
                      replace_existing=True, max_instances=1)


async def _run_auto_resume() -> None:
    """Re-enable monitors that were AUTO-paused long enough ago, with a fresh failure
    count, so a transient outage (a site down for a while, a since-fixed bug) doesn't
    leave them disabled forever. If still broken they simply auto-pause again after a
    new failure streak — no human babysitting required."""
    from datetime import timedelta
    hours = settings.auto_resume_after_hours
    if not hours:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Monitor).where(Monitor.enabled.is_(False),
                                  Monitor.auto_paused_at.is_not(None),
                                  Monitor.auto_paused_at < cutoff))).scalars().all()
        for m in rows:
            m.enabled = True
            m.consecutive_failures = 0
            m.auto_paused_at = None
        if rows:
            await session.commit()
    for m in rows:
        reschedule_monitor(m)
    if rows:
        log.info("auto-resumed %d auto-paused monitor(s)", len(rows))


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
        # Proxy-pool health: re-test configured proxies every 10 min (cheap/no-op
        # when the pool is empty), and once now so they're usable from the start.
        scheduler.add_job(
            _run_proxy_health, trigger=IntervalTrigger(minutes=10), id=_PROXY_JOB,
            replace_existing=True, max_instances=1, coalesce=True,
            next_run_time=datetime.now(timezone.utc),
        )
        # Hourly: auto-resume monitors that were auto-paused long enough ago.
        scheduler.add_job(
            _run_auto_resume, trigger=IntervalTrigger(hours=1), id=_RESUME_JOB,
            replace_existing=True, max_instances=1, coalesce=True,
        )
        scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
