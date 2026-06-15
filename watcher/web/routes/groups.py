"""Monitor groups — watch several monitors together (e.g. the same product
across retailers) for price comparison + a single group-level alert."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...ai import configure_group, suggest_goal
from ...app_settings import get_app_settings, get_openrouter_key
from ...auth.users import get_current_user
from ...config import settings
from ...db import get_session
from ...models import Change, DetectionMode, Group, Monitor, Snapshot, SnapshotStatus, User
from ...netsec import validate_monitor_url, validate_public_url
from ...scheduler import reschedule_monitor, trigger_now
from .. import templates

router = APIRouter()

_MAX_GROUP_URLS = 20


_KINDS = ("price", "stock", "change", "custom")


def _clean_target(form):
    """Parse (target_value, target_dir) from a group form, or (None, None)."""
    raw = (form.get("target_value") or "").strip()
    d = (form.get("target_dir") or "").strip()
    if not raw or d not in ("below", "above"):
        return None, None
    try:
        return float(raw), d
    except (ValueError, TypeError):
        return None, None


def _kind(form) -> str:
    k = (form.get("kind") or "price").strip()
    return k if k in _KINDS else "price"


def _intent(form) -> str | None:
    return (form.get("watch_intent") or "").strip()[:2000] or None


async def _group_cap_reached(session, user) -> bool:
    cap = settings.max_groups_per_user
    if not cap:
        return False
    n = (await session.execute(
        select(func.count()).select_from(Group).where(Group.user_id == user.id))).scalar_one()
    return n >= cap


async def _owned_group(session: AsyncSession, user: User, group_id: int) -> Group:
    g = (await session.execute(
        select(Group).where(Group.id == group_id, Group.user_id == user.id)
        .options(selectinload(Group.monitors))
    )).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return g


async def _latest_values(session: AsyncSession, monitor_ids: list[int]) -> dict:
    """{monitor_id: (numeric_value, value_label, taken_at)} for the newest valued OK
    snapshot of each monitor — one windowed query instead of one per member (was an
    N+1 on the dashboard + group pages)."""
    if not monitor_ids:
        return {}
    rn = func.row_number().over(
        partition_by=Snapshot.monitor_id,
        order_by=Snapshot.taken_at.desc()).label("rn")
    sub = (select(Snapshot.monitor_id, Snapshot.numeric_value, Snapshot.value_label,
                  Snapshot.taken_at, rn)
           .where(Snapshot.monitor_id.in_(monitor_ids),
                  Snapshot.numeric_value.is_not(None),
                  Snapshot.status == SnapshotStatus.ok)).subquery()
    rows = (await session.execute(
        select(sub.c.monitor_id, sub.c.numeric_value, sub.c.value_label, sub.c.taken_at)
        .where(sub.c.rn == 1))).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


async def _latest_changes(session: AsyncSession, monitor_ids: list[int]) -> dict:
    """{monitor_id: latest Change} in one windowed query instead of one per member."""
    if not monitor_ids:
        return {}
    rn = func.row_number().over(
        partition_by=Change.monitor_id,
        order_by=Change.detected_at.desc()).label("rn")
    sub = select(Change.id, rn).where(Change.monitor_id.in_(monitor_ids)).subquery()
    ids = (await session.execute(select(sub.c.id).where(sub.c.rn == 1))).scalars().all()
    if not ids:
        return {}
    changes = (await session.execute(select(Change).where(Change.id.in_(ids)))).scalars().all()
    return {c.monitor_id: c for c in changes}


async def _value_series(session: AsyncSession, monitor_ids: list[int], limit: int = 60) -> dict:
    """{monitor_id: [(epoch, value), …]} (ascending, ≤limit points) in one query."""
    if not monitor_ids:
        return {}
    rn = func.row_number().over(
        partition_by=Snapshot.monitor_id,
        order_by=Snapshot.taken_at.desc()).label("rn")
    sub = (select(Snapshot.monitor_id, Snapshot.numeric_value, Snapshot.taken_at, rn)
           .where(Snapshot.monitor_id.in_(monitor_ids),
                  Snapshot.numeric_value.is_not(None),
                  Snapshot.status == SnapshotStatus.ok)).subquery()
    rows = (await session.execute(
        select(sub.c.monitor_id, sub.c.numeric_value, sub.c.taken_at)
        .where(sub.c.rn <= limit)
        .order_by(sub.c.monitor_id, sub.c.taken_at))).all()
    out: dict = {}
    for mid, val, at in rows:
        if at is not None:
            out.setdefault(mid, []).append((at.timestamp(), val))
    return out


async def list_groups(session: AsyncSession, user: User) -> list[dict]:
    """Lightweight group summaries for the dashboard (name, count, best value)."""
    groups = (await session.execute(
        select(Group).where(Group.user_id == user.id)
        .options(selectinload(Group.monitors)).order_by(Group.name)
    )).scalars().all()
    # Batch the per-member lookups across ALL groups into two queries instead of
    # one-per-member (was an N+1 that ran on every dashboard load).
    price_ids = [m.id for g in groups if g.kind == "price" for m in g.monitors]
    other_ids = [m.id for g in groups if g.kind != "price" for m in g.monitors]
    values = await _latest_values(session, price_ids)
    unacked: dict = {}
    if other_ids:
        counts = (await session.execute(
            select(Change.monitor_id, func.count())
            .where(Change.monitor_id.in_(other_ids), Change.acknowledged.is_(False))
            .group_by(Change.monitor_id))).all()
        unacked = {mid: n for mid, n in counts}

    out = []
    for g in groups:
        best, recent = None, 0
        if g.kind == "price":
            vals = [(values[m.id][0], values[m.id][1]) for m in g.monitors
                    if m.id in values and values[m.id][0] is not None]
            if vals:
                # Compare on the numeric value only — never the whole tuple, or a
                # price tie where one member's label is None hits `None < str` and
                # TypeErrors the whole dashboard render.
                best = (min(vals, key=lambda t: t[0]) if g.target_dir != "above"
                        else max(vals, key=lambda t: t[0]))
        else:
            recent = sum(unacked.get(m.id, 0) for m in g.monitors)
        out.append({"group": g, "count": len(g.monitors),
                    "best_value": best[0] if best else None,
                    "best_label": best[1] if best else None,
                    "recent": recent})
    return out


@router.post("/groups")
async def create_group(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    name = (form.get("name") or "").strip()[:255]
    if not name:
        return RedirectResponse("/?group=noname", status_code=303)
    if await _group_cap_reached(session, user):
        return RedirectResponse("/?group=cap", status_code=303)
    kind = _kind(form)
    tv, td = _clean_target(form)
    g = Group(user_id=user.id, name=name, kind=kind, watch_intent=_intent(form),
              target_value=tv if kind == "price" else None,
              target_dir=td if kind == "price" else None)
    session.add(g)
    await session.flush()
    # Optionally attach an initial monitor (from the "new group" picker).
    mid = form.get("monitor_id")
    if mid and str(mid).isdigit():
        m = await session.get(Monitor, int(mid))
        if m and m.user_id == user.id:
            m.group_id = g.id
    await session.commit()
    return RedirectResponse(f"/groups/{g.id}", status_code=303)


@router.post("/groups/ai-suggest-goal")
async def ai_suggest_group_goal(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Read the FIRST pasted URL and draft a one-line group goal to pre-fill the box
    (the user reviews/edits it). Phrased for a group (cheapest-of / any-of). Shares its
    page fetch with the follow-up ai-create."""
    from ..ratelimit import allow
    if not allow(f"ai:{user.id}", limit=settings.ai_max_calls, window=settings.ai_window_seconds):
        return JSONResponse({"ok": False, "error": "Too many AI requests — wait a moment."}, status_code=429)
    data = await request.json()
    urls = [u.strip() for u in (data.get("urls") or "").splitlines() if u.strip()]
    if not urls:
        return JSONResponse({"ok": False, "error": "Add at least one page URL first."}, status_code=400)
    url = urls[0]
    if validate_monitor_url(url) or (not settings.allow_private_targets and validate_public_url(url)):
        return JSONResponse({"ok": False, "error": "The first URL isn’t a valid public web page."}, status_code=400)
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return JSONResponse(
            {"ok": False, "error": "AI isn’t configured — an admin must set an OpenRouter key in Settings."},
            status_code=400,
        )
    from .monitors import _page_text_for_suggest
    title, text = await _page_text_for_suggest(session, user, url, None)
    if not (text or "").strip():
        return JSONResponse(
            {"ok": False, "error": "Couldn’t read the first page — check the URL, then try again."},
            status_code=502,
        )
    goal = await suggest_goal(api_key=key, model=app.ai_model, base_url=app.ai_base_url,
                              url=url, title=title, page_text=text, for_group=True)
    if not goal:
        return JSONResponse({"ok": False, "error": "Couldn’t draft a goal — describe it yourself, or try again."}, status_code=502)
    return JSONResponse({"ok": True, "goal": goal})


@router.post("/groups/ai-create")
async def ai_create_group(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Build a price-comparison group from a goal + a list of retailer URLs: the
    AI names it + sets a target, and a price-tracking monitor is created per URL."""
    form = await request.form()
    goal = (form.get("goal") or "").strip()
    urls = [u.strip() for u in (form.get("urls") or "").splitlines() if u.strip()][:_MAX_GROUP_URLS]
    if not goal or not urls:
        return RedirectResponse("/?group=ai_missing", status_code=303)
    if await _group_cap_reached(session, user):
        return RedirectResponse("/?group=cap", status_code=303)
    from ..ratelimit import allow
    if not allow(f"ai:{user.id}", limit=settings.ai_max_calls, window=settings.ai_window_seconds):
        return RedirectResponse("/?group=ai_rate", status_code=303)

    # Keep only http(s) + public URLs (SSRF).
    valid = []
    for u in urls:
        if validate_monitor_url(u):
            continue
        if not settings.allow_private_targets and validate_public_url(u):
            continue
        valid.append(u[:2048])
    if not valid:
        return RedirectResponse("/?group=ai_badurls", status_code=303)

    # Respect the per-user monitor cap.
    cap = settings.max_monitors_per_user
    if cap:
        have = (await session.execute(
            select(func.count()).select_from(Monitor).where(Monitor.user_id == user.id))).scalar_one()
        valid = valid[:max(0, cap - have)]
        if not valid:
            return RedirectResponse("/?group=cap", status_code=303)

    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    cfg = await configure_group(api_key=key, model=app.ai_model, base_url=app.ai_base_url,
                                goal=goal, urls=valid) if key else None
    name = ((cfg or {}).get("name") or goal)[:255] or "New group"
    kind = (cfg or {}).get("kind")
    kind = kind if kind in _KINDS else ("price" if not cfg else "change")
    tv = (cfg or {}).get("target_value") or 0
    td = (cfg or {}).get("target_dir")
    group = Group(user_id=user.id, name=name, kind=kind,
                  watch_intent=((cfg or {}).get("watch_intent") or "").strip() or None,
                  target_value=float(tv) if (tv and kind == "price") else None,
                  target_dir=td if (td in ("below", "above") and kind == "price") else None)
    session.add(group)
    await session.flush()

    track = kind == "price"
    for u in valid:
        m = Monitor(user_id=user.id, url=u, name="", detection_mode=DetectionMode.auto,
                    interval_seconds=max(3600, settings.min_interval_seconds),
                    track_value=track, ai_enabled=True, enabled=True, group_id=group.id)
        session.add(m)
        await session.flush()
        reschedule_monitor(m)
        trigger_now(m.id)
    await session.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


@router.get("/groups/{group_id}")
async def group_detail(
    request: Request,
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    group = await _owned_group(session, user, group_id)
    is_price = group.kind == "price"

    # Batch all per-member lookups (values, latest change, price series) into a
    # few windowed queries instead of ~3 queries per member.
    member_ids = [m.id for m in group.monitors]
    values = await _latest_values(session, member_ids) if is_price else {}
    changes = await _latest_changes(session, member_ids)
    series = await _value_series(session, member_ids) if is_price else {}

    rows, raw = [], []
    for m in group.monitors:
        lv = values.get(m.id) if is_price else None
        rows.append({"monitor": m,
                     "value": lv[0] if lv else None,
                     "label": (lv[1] if lv else None) or (f"{lv[0]:g}" if lv and lv[0] is not None else None),
                     "at": lv[2] if lv else None,
                     "change": changes.get(m.id)})
        if is_price and series.get(m.id):
            raw.append((m.name or m.url, series[m.id]))

    chart = _build_chart(raw) if is_price else []
    vals = [r["value"] for r in rows if r["value"] is not None]
    best = (min(vals) if group.target_dir != "above" else max(vals)) if vals else None
    best_id = next((r["monitor"].id for r in rows if r["value"] == best), None) if best is not None else None

    # Combined recent-changes feed (non-price groups).
    feed = []
    if not is_price:
        feed = (await session.execute(
            select(Change, Monitor).join(Monitor, Monitor.id == Change.monitor_id)
            .where(Monitor.group_id == group.id)
            .order_by(Change.detected_at.desc()).limit(20)
        )).all()

    others = (await session.execute(
        select(Monitor).where(Monitor.user_id == user.id, Monitor.group_id.is_distinct_from(group.id))
        .order_by(Monitor.name)
    )).scalars().all()

    # Dashboard-style card metadata for the members, so they can be viewed inside
    # the group exactly like the main dashboard.
    from .dashboard import card_data
    members = sorted(group.monitors, key=lambda m: (m.name or m.url or "").lower())
    thumbs, blocked, unacked = await card_data(session, members)

    return templates.TemplateResponse(
        request, "group_detail.html",
        {"user": user, "group": group, "is_price": is_price, "rows": rows, "chart": chart,
         "best": best, "best_id": best_id, "others": others, "feed": feed,
         "monitors": members, "thumbs": thumbs, "blocked": blocked, "unacked": unacked},
    )


_PALETTE = ["#22d3ee", "#a78bfa", "#f472b6", "#34d399", "#fbbf24", "#60a5fa", "#fb7185"]
_CW, _CH, _CP = 800.0, 220.0, 12.0


def _build_chart(raw: list) -> list[dict]:
    """Map (name, [(epoch, value)]) series into SVG-ready polyline points."""
    if not raw:
        return []
    all_t = [t for _, pts in raw for t, _ in pts]
    all_v = [v for _, pts in raw for _, v in pts]
    tmin, tmax = min(all_t), max(all_t)
    vmin, vmax = min(all_v), max(all_v)
    tspan, vspan = (tmax - tmin) or 1.0, (vmax - vmin) or 1.0
    out = []
    for i, (name, pts) in enumerate(raw):
        coords = []
        for t, v in pts:
            x = round(_CP + (t - tmin) / tspan * (_CW - 2 * _CP), 1)
            y = round(_CP + (1 - (v - vmin) / vspan) * (_CH - 2 * _CP), 1)
            coords.append((x, y))
        out.append({"name": name, "color": _PALETTE[i % len(_PALETTE)],
                    "points": " ".join(f"{x},{y}" for x, y in coords), "dots": coords})
    return out


@router.post("/groups/{group_id}")
async def update_group(
    request: Request,
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    group = await _owned_group(session, user, group_id)
    form = await request.form()
    name = (form.get("name") or "").strip()[:255]
    if name:
        group.name = name
    group.kind = _kind(form)
    group.watch_intent = _intent(form)
    group.hide_members = (form.get("hide_members") or "").lower() in ("1", "true", "on", "yes")
    tv, td = _clean_target(form)
    if group.kind != "price":
        tv, td = None, None
    if (tv, td) != (group.target_value, group.target_dir):
        group.target_value, group.target_dir = tv, td
        group.alert_active = False  # re-arm on a target change
    await session.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


@router.post("/groups/{group_id}/add")
async def add_member(
    request: Request,
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    group = await _owned_group(session, user, group_id)
    form = await request.form()
    mid = form.get("monitor_id")
    if mid and str(mid).isdigit():
        m = await session.get(Monitor, int(mid))
        if m and m.user_id == user.id:
            m.group_id = group.id
            await session.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


@router.post("/groups/{group_id}/remove")
async def remove_member(
    request: Request,
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    group = await _owned_group(session, user, group_id)
    form = await request.form()
    mid = form.get("monitor_id")
    if mid and str(mid).isdigit():
        m = await session.get(Monitor, int(mid))
        if m and m.user_id == user.id and m.group_id == group.id:
            m.group_id = None
            await session.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


@router.post("/groups/{group_id}/delete")
async def delete_group(
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    group = await _owned_group(session, user, group_id)
    await session.delete(group)   # members' group_id is SET NULL by the FK
    await session.commit()
    return RedirectResponse("/", status_code=303)
