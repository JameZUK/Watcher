"""Monitor groups — watch several monitors together (e.g. the same product
across retailers) for price comparison + a single group-level alert."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...auth.users import get_current_user
from ...db import get_session
from ...models import Group, Monitor, Snapshot, SnapshotStatus, User
from .. import templates

router = APIRouter()


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


async def _owned_group(session: AsyncSession, user: User, group_id: int) -> Group:
    g = (await session.execute(
        select(Group).where(Group.id == group_id, Group.user_id == user.id)
        .options(selectinload(Group.monitors))
    )).scalar_one_or_none()
    if g is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return g


async def _latest_value(session: AsyncSession, monitor_id: int):
    """(numeric_value, value_label, taken_at) of the newest valued OK snapshot, or None."""
    return (await session.execute(
        select(Snapshot.numeric_value, Snapshot.value_label, Snapshot.taken_at)
        .where(Snapshot.monitor_id == monitor_id, Snapshot.numeric_value.is_not(None),
               Snapshot.status == SnapshotStatus.ok)
        .order_by(Snapshot.taken_at.desc()).limit(1)
    )).first()


async def list_groups(session: AsyncSession, user: User) -> list[dict]:
    """Lightweight group summaries for the dashboard (name, count, best value)."""
    groups = (await session.execute(
        select(Group).where(Group.user_id == user.id)
        .options(selectinload(Group.monitors)).order_by(Group.name)
    )).scalars().all()
    out = []
    for g in groups:
        vals = []
        for m in g.monitors:
            lv = await _latest_value(session, m.id)
            if lv and lv[0] is not None:
                vals.append((lv[0], lv[1]))
        best = None
        if vals:
            best = (min(vals) if g.target_dir != "above" else max(vals))
        out.append({"group": g, "count": len(g.monitors),
                    "best_value": best[0] if best else None,
                    "best_label": best[1] if best else None})
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
    tv, td = _clean_target(form)
    g = Group(user_id=user.id, name=name, target_value=tv, target_dir=td)
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


@router.get("/groups/{group_id}")
async def group_detail(
    request: Request,
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    group = await _owned_group(session, user, group_id)

    rows, raw = [], []
    for m in group.monitors:
        lv = await _latest_value(session, m.id)
        rows.append({"monitor": m,
                     "value": lv[0] if lv else None,
                     "label": (lv[1] if lv else None) or (f"{lv[0]:g}" if lv and lv[0] is not None else None),
                     "at": lv[2] if lv else None})
        pts = (await session.execute(
            select(Snapshot.numeric_value, Snapshot.taken_at)
            .where(Snapshot.monitor_id == m.id, Snapshot.numeric_value.is_not(None),
                   Snapshot.status == SnapshotStatus.ok)
            .order_by(Snapshot.taken_at.desc()).limit(60)
        )).all()
        series_pts = [(p[1].timestamp(), p[0]) for p in reversed(pts) if p[1] is not None]
        if series_pts:
            raw.append((m.name or m.url, series_pts))

    # Build a static combined chart server-side (Alpine x-for doesn't work inside <svg>).
    chart = _build_chart(raw)

    vals = [r["value"] for r in rows if r["value"] is not None]
    best = (min(vals) if group.target_dir != "above" else max(vals)) if vals else None
    best_id = next((r["monitor"].id for r in rows if r["value"] == best), None) if best is not None else None

    # Candidate monitors to add (the user's monitors not already in this group).
    others = (await session.execute(
        select(Monitor).where(Monitor.user_id == user.id, Monitor.group_id.is_distinct_from(group.id))
        .order_by(Monitor.name)
    )).scalars().all()

    return templates.TemplateResponse(
        request, "group_detail.html",
        {"user": user, "group": group, "rows": rows, "chart": chart,
         "best": best, "best_id": best_id, "others": others},
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
    tv, td = _clean_target(form)
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
