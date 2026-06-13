"""Dashboard: overview of all monitors."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import get_current_user
from ...config import settings
from ...db import SessionLocal, get_session
from ...models import Change, Group, Monitor, Snapshot, SnapshotStatus, User, utcnow
from .. import templates

router = APIRouter()

# How far back the dashboard summary headline looks.
_SUMMARY_DAYS = 7

# Cached AI summary paragraph per user (segments), keyed by a change fingerprint so
# it's regenerated only when something new arrives. In-process + best-effort.
_fleet_cache: dict[int, dict] = {}
_fleet_inflight: set[int] = set()
_fleet_tasks: set = set()


def _summary_days(user) -> int:
    """The user's chosen lookback window, clamped to a sane range."""
    try:
        return max(1, min(30, int(user.summary_days or _SUMMARY_DAYS)))
    except (TypeError, ValueError):
        return _SUMMARY_DAYS


async def _fleet_summary(session, user, monitors) -> dict:
    """A one-glance summary of what's NEW across ALL the user's sites: the count of
    *unreviewed* (unacknowledged) changes + the sites/importance breakdown + the top
    few headlines + fleet health. Acknowledged changes are excluded so the summary
    doesn't re-state what the user has already seen. No AI call, no per-monitor work."""
    days = _summary_days(user)
    since = utcnow() - timedelta(days=days)
    # New = within the window AND not yet acknowledged (reviewed).
    where = (Monitor.user_id == user.id, Change.detected_at >= since,
             Change.acknowledged.is_(False))
    recent = select(Change).join(Monitor, Monitor.id == Change.monitor_id).where(*where)
    changes = (await session.execute(
        select(func.count()).select_from(recent.subquery()))).scalar_one()
    sites = (await session.execute(
        select(func.count(func.distinct(Change.monitor_id)))
        .join(Monitor, Monitor.id == Change.monitor_id).where(*where))).scalar_one()
    imp = dict((await session.execute(
        select(Change.ai_importance, func.count(Change.id))
        .join(Monitor, Monitor.id == Change.monitor_id).where(*where)
        .group_by(Change.ai_importance))).all())
    top_rows = (await session.execute(
        select(Change, Monitor.name, Monitor.id)
        .join(Monitor, Monitor.id == Change.monitor_id).where(*where)
        .order_by(Change.detected_at.desc()).limit(5))).all()
    top = [{"headline": ch.ai_headline or ch.summary or "Change detected",
            "monitor": mname or "", "monitor_id": mid,
            "importance": ch.ai_importance, "at": ch.detected_at} for ch, mname, mid in top_rows]
    max_id = (await session.execute(
        select(func.max(Change.id)).join(Monitor, Monitor.id == Change.monitor_id)
        .where(*where))).scalar() or 0
    # The fingerprint folds in the user's summary settings AND the unreviewed set, so the
    # paragraph regenerates when a new change arrives OR when the user reviews some.
    prompt_sig = hash((user.summary_prompt or "").strip()) & 0xFFFFFFFF
    return {
        "days": days,
        "changes": changes,
        "sites": sites,
        "high": imp.get("high", 0),
        "medium": imp.get("medium", 0),
        "top": top,
        "total": len(monitors),
        "failing": sum(1 for m in monitors if (m.consecutive_failures or 0) > 0),
        "paused": sum(1 for m in monitors if not m.enabled),
        "fingerprint": f"{max_id}:{changes}:{days}:{prompt_sig}",
    }


def _domain(url: str | None) -> str:
    """The bare host of a URL (e.g. 'amazon.co.uk') for site-aware summaries."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url or "").netloc or ""
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


async def _generate_fleet_paragraph(user_id, fp, model, base_url, key, days, instruction) -> None:
    """Background: write the AI summary paragraph for a user's recent changes and
    cache it. Re-queries its own session; never raises into the caller."""
    from ...ai import summarize_fleet
    try:
        since = utcnow() - timedelta(days=days)
        async with SessionLocal() as s:
            rows = (await s.execute(
                select(Change, Monitor.name, Monitor.id, Monitor.url, Snapshot.title)
                .join(Monitor, Monitor.id == Change.monitor_id)
                .outerjoin(Snapshot, Snapshot.id == Change.to_snapshot_id)
                .where(Monitor.user_id == user_id, Change.detected_at >= since,
                       Change.acknowledged.is_(False))           # only what's unreviewed
                .order_by(Change.detected_at.desc()).limit(40))).all()
        changes = [{"monitor_id": mid, "monitor": mname or "", "domain": _domain(murl),
                    "title": stitle or "", "importance": ch.ai_importance,
                    "headline": ch.ai_headline or ch.summary or "Change detected"}
                   for ch, mname, mid, murl, stitle in rows]
        segs = await summarize_fleet(api_key=key, model=model, base_url=base_url,
                                     changes=changes, instruction=instruction)
        if segs:
            _fleet_cache[user_id] = {"fp": fp, "segments": segs}
    except Exception:  # noqa: BLE001
        logging.getLogger("watcher").warning("fleet summary generation failed for user %s", user_id)
    finally:
        _fleet_inflight.discard(user_id)


async def _fleet_paragraph(session, user, summary):
    """The cached AI paragraph (segments) for the summary, or None. Never blocks the
    dashboard: a stale/missing paragraph triggers a BACKGROUND regeneration and we
    return whatever we have (the template falls back to the headline list when None)."""
    from ...app_settings import get_app_settings, get_openrouter_key
    from ..ratelimit import allow
    # The user can switch the AI paragraph off — then we never call the model and
    # the template falls back to the headline list.
    if not user.summary_enabled:
        return None
    fp = summary["fingerprint"]
    cached = _fleet_cache.get(user.id)
    if (cached and cached["fp"] == fp) or not summary["changes"] or user.id in _fleet_inflight:
        return cached["segments"] if cached else None
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not (app.ai_enabled and key) or not allow(
            f"ai:{user.id}", limit=settings.ai_max_calls, window=settings.ai_window_seconds):
        return cached["segments"] if cached else None
    _fleet_inflight.add(user.id)
    task = asyncio.create_task(
        _generate_fleet_paragraph(user.id, fp, app.ai_model, app.ai_base_url, key,
                                  summary["days"], (user.summary_prompt or "").strip() or None))
    _fleet_tasks.add(task)
    task.add_done_callback(_fleet_tasks.discard)
    return cached["segments"] if cached else None      # show old (or fall back) while regenerating


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

    summary = await _fleet_summary(session, user, all_monitors)
    segments = await _fleet_paragraph(session, user, summary)

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
            "summary": summary,
            "summary_segments": segments,
            # The AI paragraph generates in the background; when it's not ready yet but
            # IS expected, the page polls for it (so it appears without a manual reload).
            "summary_pending": await _summary_pending(session, user, summary, segments),
        },
    )


async def _summary_pending(session, user, summary, segments) -> bool:
    """True when the AI paragraph isn't ready yet but is expected to generate — so the
    client should poll /dashboard/summary for it instead of waiting for a reload."""
    if segments is not None or not (summary["changes"] and user.summary_enabled):
        return False
    from ...app_settings import get_app_settings, get_openrouter_key
    app = await get_app_settings(session)
    return bool(app.ai_enabled and get_openrouter_key(app))


@router.get("/dashboard/summary")
async def dashboard_summary_fragment(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Polled by the dashboard: returns the AI fleet-summary paragraph once the
    background generation finishes, so it can swap in without a page reload."""
    from fastapi.responses import JSONResponse
    all_monitors = (await session.execute(
        select(Monitor).where(Monitor.user_id == user.id))).scalars().all()
    summary = await _fleet_summary(session, user, all_monitors)
    segments = await _fleet_paragraph(session, user, summary)
    return JSONResponse({"ready": segments is not None, "segments": segments or []})


async def _group_summaries(session, user):
    from .groups import list_groups
    return await list_groups(session, user)
