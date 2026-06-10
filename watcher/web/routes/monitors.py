"""Monitor CRUD, detail/diff views, image serving, and manual checks."""

from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import defer, selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ...ai import configure_monitor, suggest_watch_items, summarize_history
from ...app_settings import get_app_settings, get_openrouter_key
from ...auth.login_flows import build_secret_map, cookie_editor_to_storage_state
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
    SnapshotStatus,
    User,
    utcnow,
)
from ...netsec import validate_monitor_url, validate_proxy, validate_public_url

# How long a manually pasted cookie session is trusted before re-prompting.
# Successful checks roll this forward via mark_session(), so it self-maintains.
COOKIE_SESSION_TTL = timedelta(days=7)

# Cap bulk import to avoid mass-creation / render-pool exhaustion.
_IMPORT_CAP = 500
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


def _safe_patterns(patterns: list[str]) -> list[str]:
    """Keep only valid, bounded regexes — drop ones that don't compile or are
    pathologically long, to limit ReDoS risk from user-supplied ignore_patterns."""
    out: list[str] = []
    for p in patterns[:25]:
        if len(p) > 200:
            continue
        try:
            re.compile(p)
        except re.error:
            continue
        out.append(p)
    return out


def _clamp_int(form, key: str, default: int, lo: int, hi: int) -> int:
    """Parse a form int, falling back to default, clamped to [lo, hi].

    Guards against both crashes (non-numeric input) and resource abuse
    (absurd viewports/timeouts wedging a render slot)."""
    try:
        v = int(form.get(key) or default)
    except (ValueError, TypeError):
        v = default
    return max(lo, min(hi, v))


def _apply_form(monitor: Monitor, form) -> None:
    # Blank name → auto-populated from the page <title> on first render.
    monitor.name = (form.get("name") or "").strip()
    monitor.url = (form.get("url") or "").strip()
    monitor.engine = Engine(form.get("engine") or "chromium")
    monitor.detection_mode = DetectionMode(form.get("detection_mode") or "text")
    monitor.selector = (form.get("selector") or "").strip() or None
    monitor.selector_attr = (form.get("selector_attr") or "").strip() or None
    monitor.ignore_selectors = _lines(form.get("ignore_selectors", ""))[:50]
    monitor.ignore_patterns = _safe_patterns(_lines(form.get("ignore_patterns", "")))
    try:
        monitor.min_change_threshold = max(0.0, min(1.0, float(form.get("min_change_threshold") or 0) / 100.0))
    except (ValueError, TypeError):
        monitor.min_change_threshold = 0.0
    monitor.normalize_whitespace = _bool(form, "normalize_whitespace")
    monitor.normalize_numbers = _bool(form, "normalize_numbers")
    monitor.wait_until = form.get("wait_until") if form.get("wait_until") in (
        "load", "domcontentloaded", "networkidle", "commit") else "networkidle"
    monitor.wait_selector = (form.get("wait_selector") or "").strip() or None
    # Cap at the render ceiling so a slow-loris page can't pin a render slot
    # longer than the outer _render timeout anyway.
    monitor.wait_timeout_ms = _clamp_int(form, "wait_timeout_ms", 15000, 1000,
                                         settings.render_timeout_seconds * 1000)
    monitor.viewport_width = _clamp_int(form, "viewport_width", 1280, 320, 3840)
    monitor.viewport_height = _clamp_int(form, "viewport_height", 800, 320, 4320)
    proxy = (form.get("proxy") or "").strip()
    proxy_scheme = proxy.split("://", 1)[0].lower() if "://" in proxy else ""
    monitor.proxy = proxy if proxy_scheme in ("http", "https", "socks5", "socks5h", "socks4") else None

    minutes = _clamp_int(form, "interval_minutes", 60, 1, 60 * 24 * 30)
    monitor.interval_seconds = max(minutes * 60, settings.min_interval_seconds)
    monitor.enabled = _bool(form, "enabled")

    channels = form.getlist("notify_channels") if hasattr(form, "getlist") else []
    monitor.notify_channels = channels or ["inbox"]
    monitor.webhook_url = (form.get("webhook_url") or "").strip() or None

    # AI triage (per-monitor)
    monitor.ai_enabled = _bool(form, "ai_enabled")
    monitor.ai_watch_intent = (form.get("ai_watch_intent") or "").strip() or None
    policy = (form.get("ai_policy") or "").strip()
    monitor.ai_policy = policy if policy in ("silent", "label", "drop") else None

    # Value tracking
    monitor.track_value = _bool(form, "track_value")
    try:
        monitor.value_threshold = float(form["value_threshold"]) if (form.get("value_threshold") or "").strip() else None
    except (ValueError, TypeError):
        monitor.value_threshold = None
    vdir = (form.get("value_threshold_dir") or "").strip()
    monitor.value_threshold_dir = vdir if vdir in ("below", "above") else None

    # Organization
    monitor.tags = [t.strip() for t in (form.get("tags") or "").split(",") if t.strip()][:10]
    monitor.adaptive_interval = _bool(form, "adaptive_interval")
    gid = form.get("group_id")
    monitor.group_id = int(gid) if gid and str(gid).isdigit() else None


def _validate_targets(monitor: Monitor) -> str | None:
    """Validate every network destination on a monitor against the SSRF policy.
    Always enforces the http(s) scheme; additionally requires public IPs for the
    URL, webhook, and proxy unless ``allow_private_targets`` is set."""
    err = validate_monitor_url(monitor.url)
    if err:
        return err
    if settings.allow_private_targets:
        return None
    if (e := validate_public_url(monitor.url)):
        return e
    if monitor.webhook_url and (e := validate_public_url(monitor.webhook_url)):
        return f"Webhook URL — {e}"
    if (e := validate_proxy(monitor.proxy)):
        return f"Proxy — {e}"
    return None


def _validate_group(monitor: Monitor, user: User, groups) -> None:
    """Null out group_id unless it's one of the user's own groups (anti-IDOR)."""
    if monitor.group_id is not None and monitor.group_id not in {g.id for g in groups}:
        monitor.group_id = None


def _build_login_flow(monitor: Monitor, form) -> LoginFlow | None:
    """Reconcile a monitor's LoginFlow from the form.

    Two orthogonal capabilities live on a flow: replayed login *steps* (the
    "requires login" section) and an injected/captured *session* (pasted Cookie
    Editor JSON). Either, both, or neither may be present. Returns the flow to
    persist, or None when nothing remains (caller deletes any existing flow).

    Raises ValueError if the pasted cookie JSON is malformed.
    """
    # --- Step-based login (only when the "requires login" box is ticked) ---
    steps: list[dict] = []
    secrets_plain: dict[str, str] = {}
    login_enabled = _bool(form, "login_enabled")
    if login_enabled:
        if form.get("login_url"):
            login_url = form["login_url"].strip()
            lerr = validate_monitor_url(login_url) or (
                None if settings.allow_private_targets else validate_public_url(login_url))
            if lerr:
                raise ValueError(f"Login URL — {lerr}")
            steps.append({"action": "goto", "url": login_url})
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

    # --- Pasted Cookie Editor JSON (independent of the login checkbox) ---
    cookies = cookie_editor_to_storage_state(   # may raise; cookies scoped to the monitor host
        form.get("session_cookies_json", ""), allowed_host=urlparse(monitor.url or "").hostname,
    )
    clear_session = _bool(form, "session_cookies_clear")

    flow = monitor.login_flow or LoginFlow(monitor_id=monitor.id)

    # Steps mirror the login section exactly: unticking the box removes them.
    flow.steps = steps
    if login_enabled:
        new_secrets = build_secret_map({k: v for k, v in secrets_plain.items() if v})
        flow.encrypted_secrets = {**(flow.encrypted_secrets or {}), **new_secrets}
        if steps:
            # Newly configured steps invalidate any stale captured session,
            # unless the same submit also pastes a fresh cookie set (below).
            flow.session_state = None
            flow.session_valid_until = None
    else:
        flow.encrypted_secrets = {}

    # Session cookies take precedence and are applied last.
    if cookies is not None:
        flow.session_state = cookies
        flow.session_valid_until = utcnow() + COOKIE_SESSION_TTL
    if clear_session:
        flow.session_state = None
        flow.session_valid_until = None

    # Nothing left to do → drop the flow entirely.
    if not flow.steps and not flow.session_state:
        return None
    return flow


_TAG_BLOCK_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _html_to_text(html: str) -> str:
    """Crude HTML → visible text for feeding the suggestion model."""
    html = _TAG_BLOCK_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(a, b)
    return _WS_RE.sub(" ", text).strip()


def _html_title(html: str) -> str | None:
    """Extract the page <title> (for prefilling a new monitor's name)."""
    import html as _html
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return title[:255] or None


async def _page_text_for_suggest(session, user, url, monitor_id):
    """(title, text) for AI suggestions: prefer a recent capture, else fetch raw."""
    if monitor_id:
        snap = (
            await session.execute(
                select(Snapshot)
                .join(Monitor, Monitor.id == Snapshot.monitor_id)
                .where(
                    Snapshot.monitor_id == monitor_id,
                    Monitor.user_id == user.id,
                    Snapshot.status == SnapshotStatus.ok,
                )
                .order_by(Snapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if snap and (snap.rendered_text or "").strip():
            return snap.title, snap.rendered_text
    # Server-side fetch of a user URL → guard against SSRF (private/metadata
    # targets) and validate every redirect hop manually.
    if validate_public_url(url):
        return None, ""
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (compatible; WatcherBot/1.0)"},
        ) as c:
            cur, r = url, None
            for _ in range(4):
                r = await c.get(cur)
                loc = r.headers.get("location")
                if r.status_code in (301, 302, 303, 307, 308) and loc:
                    cur = urljoin(cur, loc)
                    if validate_public_url(cur):
                        return None, ""
                    continue
                break
        if r is None or r.status_code >= 400:
            return None, ""
        return _html_title(r.text), _html_to_text(r.text)[:12000]
    except Exception:
        return None, ""


# --- routes ----------------------------------------------------------------


def _form_response(request, user, monitor, *, error=None, cookies_json="", status=200, groups=()):
    return templates.TemplateResponse(
        request, "monitor_form.html",
        {"user": user, "monitor": monitor, "engines": list(Engine),
         "modes": list(DetectionMode), "error": error, "cookies_json": cookies_json,
         "groups": groups},
        status_code=status,
    )


async def _user_groups(session, user):
    from ...models import Group
    return (await session.execute(
        select(Group).where(Group.user_id == user.id).order_by(Group.name)
    )).scalars().all()


@router.get("/monitors/new")
async def new_monitor(request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    return _form_response(request, user, None, groups=await _user_groups(session, user))


@router.post("/monitors/ai-suggest")
async def ai_suggest_watch(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Suggest 'what to watch for' items by reviewing the page with the AI."""
    data = await request.json()
    url = (data.get("url") or "").strip()
    raw_id = data.get("monitor_id")
    monitor_id = int(raw_id) if str(raw_id).isdigit() else None
    if not url:
        return JSONResponse({"ok": False, "error": "Enter a URL to watch first."}, status_code=400)
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return JSONResponse(
            {"ok": False, "error": "AI isn’t configured — an admin must set an OpenRouter key in Settings."},
            status_code=400,
        )
    title, text = await _page_text_for_suggest(session, user, url, monitor_id)
    if not (text or "").strip():
        return JSONResponse(
            {"ok": False, "error": "Couldn’t read this page. Save the monitor and run a check, then try again."},
            status_code=502,
        )
    suggestions = await suggest_watch_items(api_key=key, model=app.ai_model, base_url=app.ai_base_url, url=url, title=title, page_text=text)
    if not suggestions:
        return JSONResponse({"ok": False, "error": "No suggestions came back — try again."}, status_code=502)
    return JSONResponse({"ok": True, "suggestions": suggestions, "title": (title or "").strip()})


@router.post("/monitors/ai-create")
async def ai_create_monitor(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Configure + create a monitor from a plain-English goal."""
    form = await request.form()
    url = (form.get("url") or "").strip()
    goal = (form.get("goal") or "").strip()
    if not (url and goal):
        return _form_response(request, user, None, error="Enter a URL and a goal for AI setup.", status=400)
    url_err = validate_monitor_url(url)
    if url_err:
        return _form_response(request, user, None, error=url_err, status=400)
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return _form_response(request, user, None, error="AI isn’t configured — an admin must set an OpenRouter key in Settings.", status=400)
    title, text = await _page_text_for_suggest(session, user, url, None)
    if not (text or "").strip():
        return _form_response(request, user, None, error="Couldn’t read that page — check the URL.", status=400)
    cfg = await configure_monitor(api_key=key, model=app.ai_model, base_url=app.ai_base_url, url=url, title=title, page_text=text, goal=goal)
    if not cfg:
        return _form_response(request, user, None, error="AI setup failed — try the manual form below.", status=502)

    try:
        mode = DetectionMode(cfg.get("detection_mode") or "auto")
    except ValueError:
        mode = DetectionMode.auto
    minutes = max(int(cfg.get("interval_minutes") or 60), 1)
    vdir = cfg.get("value_threshold_dir")
    vthr = cfg.get("value_threshold") or 0
    monitor = Monitor(
        user_id=user.id, url=url, name=(cfg.get("name") or "").strip()[:255] or url,
        detection_mode=mode, selector=(cfg.get("selector") or "").strip() or None,
        interval_seconds=max(minutes * 60, settings.min_interval_seconds),
        ai_enabled=True, ai_watch_intent=(cfg.get("ai_watch_intent") or "").strip() or None,
        track_value=bool(cfg.get("track_value")),
        value_threshold=float(vthr) if vthr else None,
        value_threshold_dir=vdir if vdir in ("below", "above") else None,
        enabled=True,
    )
    session.add(monitor)
    await session.commit()
    await session.refresh(monitor)
    reschedule_monitor(monitor)
    trigger_now(monitor.id)
    return RedirectResponse(f"/monitors/{monitor.id}", status_code=303)


@router.post("/monitors/{monitor_id}/ai-summary")
async def ai_summary(
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Summarise a monitor's recent activity with the AI."""
    monitor = await _owned_monitor(session, user, monitor_id)
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return JSONResponse({"ok": False, "error": "AI isn’t configured."}, status_code=400)
    rows = (await session.execute(
        select(Change).where(Change.monitor_id == monitor.id)
        .order_by(Change.detected_at.desc()).limit(40)
    )).scalars().all()
    snaps = (await session.execute(
        select(Snapshot).where(Snapshot.monitor_id == monitor.id, Snapshot.numeric_value.is_not(None))
        .order_by(Snapshot.taken_at.desc()).limit(40)
    )).scalars().all()
    lines = [f"{c.detected_at:%Y-%m-%d %H:%M}: {c.ai_headline or c.summary}" for c in rows]
    if snaps:
        vals = [s.value_label or f"{s.numeric_value:g}" for s in snaps]
        lines.append("Recent tracked values: " + ", ".join(vals[:20]))
    if not lines:
        return JSONResponse({"ok": False, "error": "Nothing to summarise yet."}, status_code=400)
    text = await summarize_history(api_key=key, model=app.ai_model, base_url=app.ai_base_url, name=monitor.name, lines=lines)
    if not text:
        return JSONResponse({"ok": False, "error": "Summary failed — try again."}, status_code=502)
    return JSONResponse({"ok": True, "summary": text})


@router.get("/monitors/export")
async def export_monitors(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    import json as _json
    mons = (await session.execute(
        select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.id)
    )).scalars().all()
    data = [{
        "name": m.name, "url": m.url, "engine": m.engine.value,
        "detection_mode": m.detection_mode.value, "interval_seconds": m.interval_seconds,
        "selector": m.selector, "selector_attr": m.selector_attr, "tags": m.tags or [],
        "ai_watch_intent": m.ai_watch_intent, "track_value": m.track_value,
        "value_threshold": m.value_threshold, "value_threshold_dir": m.value_threshold_dir,
        "notify_channels": m.notify_channels, "enabled": m.enabled,
    } for m in mons]
    return Response(
        _json.dumps({"monitors": data}, indent=2), media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=watcher-monitors.json"},
    )


@router.post("/monitors/import")
async def import_monitors(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    import json as _json
    form = await request.form()
    raw = (form.get("data") or "").strip()
    try:
        parsed = _json.loads(raw)
    except Exception:
        return RedirectResponse("/?import=error", status_code=303)
    items = parsed.get("monitors") if isinstance(parsed, dict) else parsed
    # Respect the per-user cap across the whole account, not just per request.
    cap = settings.max_monitors_per_user
    remaining = _IMPORT_CAP
    if cap:
        existing = (await session.execute(
            select(func.count()).select_from(Monitor).where(Monitor.user_id == user.id)
        )).scalar_one()
        remaining = max(0, min(_IMPORT_CAP, cap - existing))
    count = 0
    for it in (items or [])[:remaining]:   # bound mass-creation / render-pool abuse
        if not isinstance(it, dict) or not (it.get("url") or "").strip():
            continue
        url = it["url"].strip()
        if validate_monitor_url(url):   # skip non-http(s) URLs
            continue
        if not settings.allow_private_targets and validate_public_url(url):  # skip internal targets
            continue
        try:
            eng = Engine(it.get("engine") or "chromium")
        except ValueError:
            eng = Engine.chromium
        try:
            mode = DetectionMode(it.get("detection_mode") or "auto")
        except ValueError:
            mode = DetectionMode.auto
        try:
            interval = max(int(it.get("interval_seconds") or 3600), settings.min_interval_seconds)
        except (ValueError, TypeError):
            interval = 3600
        vdir = it.get("value_threshold_dir")
        m = Monitor(
            user_id=user.id, url=it["url"].strip()[:2048], name=(it.get("name") or it["url"])[:255],
            engine=eng, detection_mode=mode, interval_seconds=interval,
            selector=it.get("selector") or None, selector_attr=it.get("selector_attr") or None,
            tags=it.get("tags") or [], ai_watch_intent=it.get("ai_watch_intent") or None,
            track_value=bool(it.get("track_value")), value_threshold=it.get("value_threshold"),
            value_threshold_dir=vdir if vdir in ("below", "above") else None,
            notify_channels=it.get("notify_channels") or ["inbox"], enabled=bool(it.get("enabled", True)),
        )
        session.add(m)
        await session.flush()
        reschedule_monitor(m)
        trigger_now(m.id)
        count += 1
    await session.commit()
    return RedirectResponse(f"/?imported={count}", status_code=303)


@router.post("/monitors")
async def create_monitor(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    groups = await _user_groups(session, user)
    cap = settings.max_monitors_per_user
    if cap:
        count = (await session.execute(
            select(func.count()).select_from(Monitor).where(Monitor.user_id == user.id)
        )).scalar_one()
        if count >= cap:
            return _form_response(request, user, None, groups=groups,
                                  error=f"Monitor limit reached ({cap}). Delete some first.", status=400)
    monitor = Monitor(user_id=user.id)
    _apply_form(monitor, form)
    _validate_group(monitor, user, groups)
    url_err = _validate_targets(monitor)
    if url_err:
        return _form_response(request, user, None, error=url_err, status=400, groups=groups)
    try:
        flow = _build_login_flow(monitor, form)
    except ValueError as exc:
        return _form_response(request, user, None, error=str(exc), groups=groups,
                              cookies_json=form.get("session_cookies_json", ""), status=400)
    session.add(monitor)
    await session.flush()
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
    return _form_response(request, user, monitor, groups=await _user_groups(session, user))


@router.post("/monitors/{monitor_id}")
async def update_monitor(
    request: Request,
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    form = await request.form()
    groups = await _user_groups(session, user)
    _apply_form(monitor, form)
    _validate_group(monitor, user, groups)
    url_err = _validate_targets(monitor)
    if url_err:
        return _form_response(request, user, monitor, error=url_err, status=400, groups=groups)
    try:
        flow = _build_login_flow(monitor, form)
    except ValueError as exc:
        # Don't commit the in-memory edits; re-render with the submitted values.
        return _form_response(request, user, monitor, error=str(exc), groups=groups,
                              cookies_json=form.get("session_cookies_json", ""), status=400)
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
    # Cooldown: stop a user spamming immediate checks to flood the render pool.
    cooldown = settings.manual_check_cooldown_seconds
    if cooldown and monitor.last_checked_at is not None:
        from datetime import timezone

        from ...models import utcnow
        lc = monitor.last_checked_at
        if lc.tzinfo is None:                    # SQLite hands back naive UTC
            lc = lc.replace(tzinfo=timezone.utc)
        age = (utcnow() - lc).total_seconds()
        if 0 <= age < cooldown:
            return RedirectResponse(f"/monitors/{monitor.id}?checked=cooldown", status_code=303)
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
            # rendered_text is large and unused by the detail template — don't load it.
            .options(defer(Snapshot.rendered_text))
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

    # Tracked-value time series (chronological) for the history chart.
    value_series = [
        {"v": s.numeric_value, "label": s.value_label or f"{s.numeric_value:g}",
         "at": s.taken_at.isoformat() if s.taken_at else ""}
        for s in reversed(snapshots)
        if s.status == SnapshotStatus.ok and s.numeric_value is not None
    ]
    value_current = value_series[-1]["label"] if value_series else None

    return templates.TemplateResponse(
        request, "monitor_detail.html",
        {
            "user": user, "monitor": monitor, "changes": changes,
            "snapshots": snapshots, "selected": selected, "diff": diff_payload,
            "latest": latest, "value_series": value_series, "value_current": value_current,
        },
    )


def _diff_payload(change: Change) -> dict:
    """Prepare diff display data: text lines and/or a visual overlay."""
    has_text = bool(change.diff_blob)
    has_visual = bool(change.visual_blob)
    has_visual_mobile = bool(change.visual_mobile_blob)
    lines: list[str] = []
    if has_text:
        lines = (blobs.get_text(change.diff_blob) or "").splitlines()
    return {"has_text": has_text, "has_visual": has_visual,
            "has_visual_mobile": has_visual_mobile, "lines": lines}


_IMG_CACHE = "public, max-age=31536000, immutable"


def _serve_blob_image(request: Request, key: str | None):
    """Serve a content-addressed image with an immutable ETag (key=sha256).
    Streams off-thread via FileResponse; returns 304 on a matching If-None-Match."""
    if not key:
        raise HTTPException(404)
    p = blobs.path(key)
    if not p.exists():
        raise HTTPException(404)
    etag = f'"{key}"'
    headers = {"ETag": etag, "Cache-Control": _IMG_CACHE}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(p, media_type="image/png", headers=headers)


@router.get("/monitors/{monitor_id}/snapshots/{snapshot_id}/image")
async def snapshot_image(
    request: Request,
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
    return _serve_blob_image(request, key)


@router.get("/monitors/{monitor_id}/changes/{change_id}/overlay")
async def change_overlay(
    request: Request,
    monitor_id: int,
    change_id: int,
    v: str | None = None,  # "mobile" for the mobile-viewport overlay
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _owned_monitor(session, user, monitor_id)
    change = await session.get(Change, change_id)
    if not change or change.monitor_id != monitor_id:
        raise HTTPException(404)
    # Prefer the requested variant; fall back to the desktop overlay.
    key = change.visual_mobile_blob if v == "mobile" else change.visual_blob
    key = key or change.visual_blob or change.visual_mobile_blob
    return _serve_blob_image(request, key)
