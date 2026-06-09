"""Monitor CRUD, detail/diff views, image serving, and manual checks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.login_flows import build_secret_map
from ...auth.users import get_current_user
from ...config import settings
from ...db import get_session
from ...models import (
    Change,
    DetectionMode,
    Engine,
    LoginFlow,
    Monitor,
    Snapshot,
    User,
)
from ...scheduler import reschedule_monitor, trigger_now, unschedule_monitor
from ...storage import blobs
from .. import templates

router = APIRouter()


# --- helpers ---------------------------------------------------------------


async def _owned_monitor(session: AsyncSession, user: User, monitor_id: int) -> Monitor:
    monitor = (
        await session.execute(
            select(Monitor)
            .where(Monitor.id == monitor_id, Monitor.user_id == user.id)
            .options(selectinload(Monitor.login_flow))
        )
    ).scalar_one_or_none()
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    return monitor


def _lines(value: str) -> list[str]:
    return [ln.strip() for ln in (value or "").splitlines() if ln.strip()]


def _bool(form, key: str) -> bool:
    return form.get(key) in ("on", "true", "1", "yes")


def _apply_form(monitor: Monitor, form) -> None:
    # Blank name → auto-populated from the page <title> on first render.
    monitor.name = (form.get("name") or "").strip()
    monitor.url = (form.get("url") or "").strip()
    monitor.engine = Engine(form.get("engine") or "chromium")
    monitor.detection_mode = DetectionMode(form.get("detection_mode") or "text")
    monitor.selector = (form.get("selector") or "").strip() or None
    monitor.selector_attr = (form.get("selector_attr") or "").strip() or None
    monitor.ignore_selectors = _lines(form.get("ignore_selectors", ""))
    monitor.ignore_patterns = _lines(form.get("ignore_patterns", ""))
    monitor.min_change_threshold = float(form.get("min_change_threshold") or 0) / 100.0
    monitor.normalize_whitespace = _bool(form, "normalize_whitespace")
    monitor.normalize_numbers = _bool(form, "normalize_numbers")
    monitor.wait_until = form.get("wait_until") or "networkidle"
    monitor.wait_selector = (form.get("wait_selector") or "").strip() or None
    monitor.wait_timeout_ms = int(form.get("wait_timeout_ms") or 15000)
    monitor.viewport_width = int(form.get("viewport_width") or 1280)
    monitor.viewport_height = int(form.get("viewport_height") or 800)
    monitor.proxy = (form.get("proxy") or "").strip() or None

    minutes = max(int(form.get("interval_minutes") or 60), 1)
    monitor.interval_seconds = max(minutes * 60, settings.min_interval_seconds)
    monitor.enabled = _bool(form, "enabled")

    channels = form.getlist("notify_channels") if hasattr(form, "getlist") else []
    monitor.notify_channels = channels or ["inbox"]
    monitor.webhook_url = (form.get("webhook_url") or "").strip() or None


def _build_login_flow(monitor: Monitor, form) -> LoginFlow | None:
    if not _bool(form, "login_enabled"):
        return None
    steps = []
    secrets_plain: dict[str, str] = {}
    if form.get("login_url"):
        steps.append({"action": "goto", "url": form["login_url"].strip()})
    if form.get("login_user_selector"):
        secrets_plain["username"] = form.get("login_username", "")
        steps.append({"action": "fill", "selector": form["login_user_selector"].strip(),
                      "secret": "username"})
    if form.get("login_pass_selector"):
        secrets_plain["password"] = form.get("login_password", "")
        steps.append({"action": "fill", "selector": form["login_pass_selector"].strip(),
                      "secret": "password"})
    if form.get("login_submit_selector"):
        steps.append({"action": "click", "selector": form["login_submit_selector"].strip()})
    if form.get("login_success_selector"):
        steps.append({"action": "wait", "selector": form["login_success_selector"].strip()})
    if not steps:
        return None
    flow = monitor.login_flow or LoginFlow(monitor_id=monitor.id)
    flow.steps = steps
    # Preserve previously stored secrets if a password field was left blank.
    new_secrets = build_secret_map({k: v for k, v in secrets_plain.items() if v})
    flow.encrypted_secrets = {**(flow.encrypted_secrets or {}), **new_secrets}
    flow.session_state = None  # force re-login after a flow change
    flow.session_valid_until = None
    return flow


# --- routes ----------------------------------------------------------------


@router.get("/monitors/new")
async def new_monitor(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        request, "monitor_form.html",
        {"user": user, "monitor": None, "engines": list(Engine), "modes": list(DetectionMode)},
    )


@router.post("/monitors")
async def create_monitor(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    monitor = Monitor(user_id=user.id)
    _apply_form(monitor, form)
    session.add(monitor)
    await session.flush()
    flow = _build_login_flow(monitor, form)
    if flow is not None:
        flow.monitor_id = monitor.id
        session.add(flow)
    await session.commit()
    await session.refresh(monitor)

    reschedule_monitor(monitor)
    trigger_now(monitor.id)  # baseline immediately
    return RedirectResponse(f"/monitors/{monitor.id}", status_code=303)


@router.get("/monitors/{monitor_id}/edit")
async def edit_monitor(
    request: Request,
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    return templates.TemplateResponse(
        request, "monitor_form.html",
        {"user": user, "monitor": monitor, "engines": list(Engine), "modes": list(DetectionMode)},
    )


@router.post("/monitors/{monitor_id}")
async def update_monitor(
    request: Request,
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    form = await request.form()
    _apply_form(monitor, form)
    flow = _build_login_flow(monitor, form)
    if flow is not None and flow.monitor_id is None:
        flow.monitor_id = monitor.id
        session.add(flow)
    elif flow is None and monitor.login_flow is not None:
        await session.delete(monitor.login_flow)
    await session.commit()
    await session.refresh(monitor)

    if monitor.enabled:
        reschedule_monitor(monitor)
    else:
        unschedule_monitor(monitor.id)
    return RedirectResponse(f"/monitors/{monitor.id}", status_code=303)


@router.post("/monitors/{monitor_id}/delete")
async def delete_monitor(
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    unschedule_monitor(monitor.id)
    await session.delete(monitor)
    await session.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/monitors/{monitor_id}/toggle")
async def toggle_monitor(
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    monitor.enabled = not monitor.enabled
    await session.commit()
    await session.refresh(monitor)
    if monitor.enabled:
        reschedule_monitor(monitor)
    else:
        unschedule_monitor(monitor.id)
    return RedirectResponse(f"/monitors/{monitor.id}", status_code=303)


@router.post("/monitors/{monitor_id}/check")
async def check_monitor_now(
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    trigger_now(monitor.id)
    return RedirectResponse(f"/monitors/{monitor.id}", status_code=303)


@router.get("/monitors/{monitor_id}")
async def monitor_detail(
    request: Request,
    monitor_id: int,
    change: int | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    changes = (
        await session.execute(
            select(Change).where(Change.monitor_id == monitor.id)
            .order_by(Change.detected_at.desc()).limit(50)
        )
    ).scalars().all()
    snapshots = (
        await session.execute(
            select(Snapshot).where(Snapshot.monitor_id == monitor.id)
            .order_by(Snapshot.taken_at.desc()).limit(50)
        )
    ).scalars().all()

    selected = None
    if change is not None:
        selected = next((c for c in changes if c.id == change), None)
    elif changes:
        selected = changes[0]

    diff_payload = _diff_payload(selected) if selected else None
    latest = snapshots[0] if snapshots else None

    return templates.TemplateResponse(
        request, "monitor_detail.html",
        {
            "user": user, "monitor": monitor, "changes": changes,
            "snapshots": snapshots, "selected": selected, "diff": diff_payload,
            "latest": latest,
        },
    )


def _diff_payload(change: Change) -> dict:
    """Prepare diff display data: text lines and/or a visual overlay."""
    has_text = bool(change.diff_blob)
    has_visual = bool(change.visual_blob)
    lines: list[str] = []
    if has_text:
        lines = (blobs.get_text(change.diff_blob) or "").splitlines()
    return {"has_text": has_text, "has_visual": has_visual, "lines": lines}


@router.get("/monitors/{monitor_id}/snapshots/{snapshot_id}/image")
async def snapshot_image(
    monitor_id: int,
    snapshot_id: int,
    v: str | None = None,  # "mobile" for the mobile-viewport capture
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _owned_monitor(session, user, monitor_id)
    snap = await session.get(Snapshot, snapshot_id)
    if not snap or snap.monitor_id != monitor_id:
        raise HTTPException(404)
    # Prefer the requested variant; fall back to the desktop capture.
    key = snap.screenshot_mobile_blob if v == "mobile" else snap.screenshot_blob
    key = key or snap.screenshot_blob or snap.screenshot_mobile_blob
    data = blobs.get_bytes(key) if key else None
    if data is None:
        raise HTTPException(404)
    return Response(content=data, media_type="image/png")


@router.get("/monitors/{monitor_id}/changes/{change_id}/overlay")
async def change_overlay(
    monitor_id: int,
    change_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _owned_monitor(session, user, monitor_id)
    change = await session.get(Change, change_id)
    if not change or change.monitor_id != monitor_id or not change.visual_blob:
        raise HTTPException(404)
    data = blobs.get_bytes(change.visual_blob)
    if data is None:
        raise HTTPException(404)
    return Response(content=data, media_type="image/png")
