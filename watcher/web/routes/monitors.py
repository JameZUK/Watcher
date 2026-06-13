"""Monitor CRUD, detail/diff views, image serving, and manual checks."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import defer, selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ...ai import (ai_login_action, configure_monitor, solve_captcha_grid,
                   suggest_watch_items, summarize_history)
from ...app_settings import get_app_settings, get_openrouter_key
from ...auth import ai_login
from ...auth.login_flows import (
    apply_cookie_edits,
    build_secret_map,
    cookie_editor_to_storage_state,
    cookie_rows,
    cookie_summary,
    mark_session,
    merge_storage_state,
    resolve_secrets,
)
from ...auth.users import get_current_user
from ...config import settings
from ...db import SessionLocal, get_session
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
from ...scheduler import is_checking, reschedule_monitor, trigger_now, unschedule_monitor
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
    monitor.use_proxy_pool = _bool(form, "use_proxy_pool")
    monitor.block_annoyances = _bool(form, "block_annoyances")
    # Optional manual override: extra selectors to click after load (accept a
    # cookie wall, close a modal, tick a captcha box). Applied in order, best-effort.
    monitor.consent_clicks = _lines(form.get("consent_clicks", ""))[:15]

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

    # Auto re-login when the stored session expires (credential logins only)
    monitor.auto_relogin_enabled = _bool(form, "auto_relogin_enabled")

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
    # Normally cookies are scoped to the monitor host. Federated/SSO logins (e.g.
    # Glassdoor signs in via indeed.com) need the auth domain's cookies too, so an
    # opt-in keeps cookies for ALL domains in the paste.
    _all_domains = _bool(form, "session_cookies_all_domains")
    cookies = cookie_editor_to_storage_state(   # may raise
        form.get("session_cookies_json", ""),
        allowed_host=None if _all_domains else urlparse(monitor.url or "").hostname,
    )
    clear_session = _bool(form, "session_cookies_clear")
    replace_session = _bool(form, "session_cookies_replace")

    flow = monitor.login_flow or LoginFlow(monitor_id=monitor.id)

    # Steps mirror the login section exactly: unticking the box removes them.
    old_steps = flow.steps
    flow.steps = steps
    if login_enabled:
        new_secrets = build_secret_map({k: v for k, v in secrets_plain.items() if v})
        flow.encrypted_secrets = {**(flow.encrypted_secrets or {}), **new_secrets}
        if steps and steps != old_steps:
            # Only a CHANGE to the login steps invalidates a captured session —
            # re-saving the same config must NOT wipe a session the AI login (or a
            # cookie paste) stored. A fresh cookie paste below can still override.
            flow.session_state = None
            flow.session_valid_until = None
    else:
        flow.encrypted_secrets = {}

    # Session cookies, applied last. A fresh paste MERGES into the stored set by
    # default (so you can add an auth domain without re-pasting everything);
    # "replace" overrides, "clear" wipes.
    if cookies is not None:
        if replace_session or not flow.session_state:
            flow.session_state = cookies
        else:
            flow.session_state = merge_storage_state(flow.session_state, cookies)
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


def _ai_quota_ok(user) -> bool:
    """Per-user cap on calls that spend the shared OpenRouter key."""
    from ..ratelimit import allow
    return allow(f"ai:{user.id}", limit=settings.ai_max_calls, window=settings.ai_window_seconds)


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
    if not _ai_quota_ok(user):
        return JSONResponse({"ok": False, "error": "Too many AI requests — wait a moment."}, status_code=429)
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
    if not _ai_quota_ok(user):
        return _form_response(request, user, None, error="Too many AI requests — wait a moment.", status=429)
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
    if not _ai_quota_ok(user):
        return JSONResponse({"ok": False, "error": "Too many AI requests — wait a moment."}, status_code=429)
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


@router.post("/monitors/{monitor_id}/analyze-page")
async def analyze_page(
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """(Re)derive the AI's understanding of this page — which regions matter vs which
    are incidental churn — from the latest capture, and store it for triage."""
    if not _ai_quota_ok(user):
        return JSONResponse({"ok": False, "error": "Too many AI requests — wait a moment."}, status_code=429)
    monitor = await _owned_monitor(session, user, monitor_id)
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return JSONResponse({"ok": False, "error": "AI isn’t configured."}, status_code=400)
    snap = (await session.execute(
        select(Snapshot).where(Snapshot.monitor_id == monitor.id, Snapshot.html_blob.is_not(None))
        .order_by(Snapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
    text = None
    if snap is not None:
        text = snap.rendered_text or (blobs.get_text(snap.html_blob) if snap.html_blob else None)
    if not (text or "").strip():
        return JSONResponse({"ok": False, "error": "No captured page text yet — run a check first."}, status_code=400)
    from ...ai import profile_page
    from ...detection import churn
    profile = await profile_page(
        api_key=key, model=app.ai_model, base_url=app.ai_base_url,
        url=monitor.url, title=monitor.name, intent=monitor.ai_watch_intent, page_text=text,
        churn_samples=churn.churny_texts(monitor.churn_lines))
    if not profile:
        return JSONResponse({"ok": False, "error": "Analysis failed — try again."}, status_code=502)
    monitor.ai_page_profile = profile
    await session.commit()
    return JSONResponse({"ok": True, "profile": profile})


async def _regenerate_profile_bg(session, app, monitor) -> None:
    """Kick off a background page-profile regeneration from the latest captured text —
    used when the watch intent changed, so the understanding stays in sync. Falls back
    to the runner's lazy generation (next check) if there's no capture yet."""
    key = get_openrouter_key(app)
    if not (monitor.ai_enabled and app.ai_enabled and key and monitor.ai_watch_intent):
        return
    snap = (await session.execute(
        select(Snapshot).where(Snapshot.monitor_id == monitor.id, Snapshot.html_blob.is_not(None))
        .order_by(Snapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
    text = (snap.rendered_text or (blobs.get_text(snap.html_blob) if snap and snap.html_blob else None)) if snap else None
    if not (text or "").strip():
        return  # no capture yet → the runner regenerates on the next check
    from ...detection import churn
    from ...runner import _profile_inflight, _profile_tasks, _run_profile_page
    if monitor.id in _profile_inflight:
        return
    _profile_inflight.add(monitor.id)
    task = asyncio.create_task(_run_profile_page(
        monitor.id, app.ai_model, app.ai_base_url, key, monitor.url, monitor.name,
        monitor.ai_watch_intent, text, churn.churny_texts(monitor.churn_lines)))
    _profile_tasks.add(task)
    task.add_done_callback(_profile_tasks.discard)


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
    url_err = await asyncio.to_thread(_validate_targets, monitor)
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
    old_intent = monitor.ai_watch_intent
    _apply_form(monitor, form)
    _validate_group(monitor, user, groups)
    url_err = await asyncio.to_thread(_validate_targets, monitor)
    if url_err:
        return _form_response(request, user, monitor, error=url_err, status=400, groups=groups)
    # The page understanding is built from the watch intent — if the intent changed,
    # the stored profile is stale. Clear it and regenerate (below, after commit).
    intent_changed = (monitor.ai_watch_intent or None) != (old_intent or None)
    if intent_changed:
        monitor.ai_page_profile = None
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

    # Intent changed → refresh the page understanding in the background (best-effort).
    if intent_changed:
        await _regenerate_profile_bg(session, await get_app_settings(session), monitor)

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


@router.post("/monitors/{monitor_id}/history/delete-before")
async def delete_history_before(
    monitor_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete all history (snapshots + changes) OLDER than a pivot snapshot —
    the render the user is viewing in the history scrubber. The pivot itself and
    everything newer is kept, so the viewed render becomes the new oldest entry.
    Orphaned screenshot/HTML blobs are reclaimed by the scheduled retention GC.
    """
    monitor = await _owned_monitor(session, user, monitor_id)
    form = await request.form()
    try:
        pivot_id = int(form.get("before_snapshot_id") or 0)
    except (TypeError, ValueError):
        pivot_id = 0
    pivot = await session.get(Snapshot, pivot_id)
    if pivot is None or pivot.monitor_id != monitor.id:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    cutoff = pivot.taken_at

    # Detach any surviving change (>= cutoff) from a pre-cutoff baseline snapshot
    # we're about to remove, so the delete can't trip an FK constraint regardless
    # of the connection's foreign_keys pragma. The change keeps its own diff blobs.
    old_ids = select(Snapshot.id).where(
        Snapshot.monitor_id == monitor.id, Snapshot.taken_at < cutoff)
    await session.execute(
        update(Change)
        .where(Change.monitor_id == monitor.id, Change.from_snapshot_id.in_(old_ids))
        .values(from_snapshot_id=None))
    # Remove old changes, then old snapshots.
    await session.execute(
        delete(Change).where(Change.monitor_id == monitor.id, Change.detected_at < cutoff))
    result = await session.execute(
        delete(Snapshot).where(Snapshot.monitor_id == monitor.id, Snapshot.taken_at < cutoff))
    await session.commit()
    logging.getLogger("watcher").info(
        "deleted %s snapshot(s) before %s for monitor %s",
        result.rowcount, cutoff, monitor.id)
    return RedirectResponse(f"/monitors/{monitor.id}", status_code=303)


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


async def _latest_snapshot_id(session, monitor_id: int) -> int:
    return (await session.execute(
        select(Snapshot.id).where(Snapshot.monitor_id == monitor_id)
        .order_by(Snapshot.id.desc()).limit(1))).scalar_one_or_none() or 0


@router.post("/monitors/{monitor_id}/check")
async def check_monitor_now(
    monitor_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Queue an immediate check. Returns JSON so the UI can show a live
    "Checking…" state and poll /check-status for the new capture."""
    monitor = await _owned_monitor(session, user, monitor_id)
    # baseline = the newest snapshot right now, so the client can spot the new one.
    baseline = await _latest_snapshot_id(session, monitor.id)
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
            return JSONResponse({"ok": False, "cooldown": int(cooldown - age) + 1})
    trigger_now(monitor.id)
    return JSONResponse({"ok": True, "queued": True, "baseline_id": baseline})


@router.get("/monitors/{monitor_id}/check-status")
async def check_status(
    monitor_id: int,
    since: int = 0,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Poll target for an in-progress check. `done` flips true once a snapshot
    newer than `since` exists (covers both success and failure — both write a
    snapshot). `checking` reflects the queued/running state for the spinner."""
    monitor = await _owned_monitor(session, user, monitor_id)
    latest = (await session.execute(
        select(Snapshot).where(Snapshot.monitor_id == monitor.id)
        .options(defer(Snapshot.rendered_text))
        .order_by(Snapshot.id.desc()).limit(1))).scalar_one_or_none()
    if latest is not None and latest.id > since:
        return JSONResponse({
            "done": True, "id": latest.id, "status": latest.status.value,
            "error": latest.error, "checking": is_checking(monitor.id),
        })
    return JSONResponse({"done": False, "checking": is_checking(monitor.id)})


# --- AI-assisted login: an interactive agent drives a real browser to log in,
# pausing for a one-time code, and saves the resulting session on the monitor ---

@router.post("/monitors/{monitor_id}/ai-login/start")
async def ai_login_start(
    monitor_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor = await _owned_monitor(session, user, monitor_id)
    app = await get_app_settings(session)
    key = get_openrouter_key(app)

    form = await request.form()
    # Pure manual mode: open the monitored site and let the user drive it — no AI,
    # no credentials needed. (Reuses the whole live-control machinery.)
    manual = _bool(form, "manual")

    if manual:
        target_url = monitor.url
        username = password = ""
        if monitor.login_flow:    # keep stored creds for the re-login convenience
            stored = resolve_secrets(monitor.login_flow)
            username, password = stored.get("username", ""), stored.get("password", "")
    else:
        if not (app.ai_enabled and key):
            return JSONResponse({"ok": False, "error": "AI isn’t configured — an admin must set an OpenRouter key in Settings."}, status_code=400)
        target_url = (form.get("login_url") or "").strip() or monitor.url
        username = (form.get("login_username") or "").strip()
        password = form.get("login_password") or ""
        if monitor.login_flow and (not username or not password):
            stored = resolve_secrets(monitor.login_flow)
            username = username or stored.get("username", "")
            password = password or stored.get("password", "")
        if not username or not password:
            return JSONResponse({"ok": False, "error": "Enter the login URL, username and password."}, status_code=400)

    if url_err := validate_monitor_url(target_url):
        return JSONResponse({"ok": False, "error": url_err}, status_code=400)
    # Same SSRF policy as every other outbound path: the live login agent drives a
    # real browser at target_url and streams the response back, so a private/internal
    # target (169.254.169.254, localhost, RFC1918) must be refused unless explicitly allowed.
    if not settings.allow_private_targets and (url_err := validate_public_url(target_url)):
        return JSONResponse({"ok": False, "error": url_err}, status_code=400)

    sess = ai_login.create_session(monitor.id, user.id, target_url,
                                   {"username": username, "password": password})
    if manual:
        sess.mode = "manual"
        sess.prompt = "Manual control — drive the page, then ‘Capture session & finish’."
    model, base = app.ai_model, app.ai_base_url
    engine = monitor.engine.value
    proxy = monitor.proxy
    creds = {"username": username, "password": password}

    async def action_fn(**kw):
        if not key:
            return {"action": "fail", "index": -1, "reason": "manual mode"}
        return await ai_login_action(api_key=key, model=model, base_url=base, **kw)

    async def solve_captcha_fn(target, rows, cols, png):
        return await solve_captcha_grid(api_key=key, model=model, base_url=base,
                                        target=target, rows=rows, cols=cols, image_png=png)

    async def persist_fn(state):
        async with SessionLocal() as s2:
            m2 = (await s2.execute(
                select(Monitor).where(Monitor.id == monitor_id)
                .options(selectinload(Monitor.login_flow)))).scalar_one_or_none()
            if m2 is None:
                return
            flow = m2.login_flow or LoginFlow(monitor_id=m2.id)
            flow.session_state = state
            mark_session(flow, state)
            # Remember the credentials (encrypted) so a future re-login is one click.
            flow.encrypted_secrets = {**(flow.encrypted_secrets or {}),
                                      **build_secret_map({k: v for k, v in creds.items() if v})}
            if m2.login_flow is None:
                s2.add(flow)
            await s2.commit()

    import asyncio
    sess._task = asyncio.create_task(ai_login.run_agent(
        sess, action_fn, persist_fn, engine=engine, proxy=proxy,
        wait_until=monitor.wait_until,
        solve_captcha_fn=None if manual else solve_captcha_fn))
    return JSONResponse({"ok": True, "sid": sess.id})


@router.get("/monitors/{monitor_id}/ai-login/status")
async def ai_login_status(
    monitor_id: int, sid: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    s = ai_login.get_session(sid, user.id)
    if not s or s.monitor_id != monitor_id:
        return JSONResponse({"ok": False, "error": "This login session has expired — start again."}, status_code=404)
    captured = cookie_summary(s.result_state) if s.result_state else None
    return JSONResponse({"ok": True, "status": s.status, "prompt": s.prompt,
                         "log": s.log[-14:], "has_shot": s.screenshot is not None,
                         "error": s.error, "mode": s.mode, "captured": captured})


@router.get("/monitors/{monitor_id}/ai-login/shot")
async def ai_login_shot(
    monitor_id: int, sid: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    s = ai_login.get_session(sid, user.id)
    if not s or s.monitor_id != monitor_id or not s.screenshot:
        raise HTTPException(status_code=404)
    return Response(content=s.screenshot, media_type="image/png",
                   headers={"Cache-Control": "no-store"})


@router.post("/monitors/{monitor_id}/ai-login/code")
async def ai_login_code(
    monitor_id: int, sid: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    s = ai_login.get_session(sid, user.id)
    if not s or s.monitor_id != monitor_id:
        return JSONResponse({"ok": False, "error": "expired"}, status_code=404)
    form = await request.form()
    s.submit_code((form.get("code") or "").strip())
    return JSONResponse({"ok": True})


@router.post("/monitors/{monitor_id}/ai-login/mode")
async def ai_login_mode(
    monitor_id: int, sid: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Switch between AI control and manual remote control."""
    s = ai_login.get_session(sid, user.id)
    if not s or s.monitor_id != monitor_id:
        return JSONResponse({"ok": False, "error": "expired"}, status_code=404)
    form = await request.form()
    mode = (form.get("mode") or "").strip()
    if mode in ("ai", "manual"):
        s.mode = mode
        s.prompt = "" if mode == "ai" else (s.prompt or "Manual control — drive the page, then ‘Capture session & finish’.")
    return JSONResponse({"ok": True, "mode": s.mode})


@router.post("/monitors/{monitor_id}/ai-login/input")
async def ai_login_input(
    monitor_id: int, sid: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Relay one manual input event (click/type/key/scroll) to the live browser."""
    s = ai_login.get_session(sid, user.id)
    if not s or s.monitor_id != monitor_id:
        return JSONResponse({"ok": False, "error": "expired"}, status_code=404)
    if s.mode != "manual":
        return JSONResponse({"ok": False, "error": "not in manual mode"}, status_code=409)
    try:
        ev = await request.json()
    except Exception:
        ev = {}
    et = ev.get("type") if isinstance(ev, dict) else None
    if et in ("click", "dblclick", "move", "drag", "type", "key", "scroll"):
        if et != "move":
            # Never log the typed `text`/`key` — during a manual login that's the
            # user's password / OTP / email. Coordinates + a redacted length are
            # enough to debug input relay. (Whole block is gated off by default.)
            redacted = {k: ev.get(k) for k in ("fx", "fy", "fx2", "fy2")
                        if ev.get(k) is not None}
            if ev.get("text") is not None:
                redacted["text"] = f"<{len(str(ev.get('text')))} chars>"
            if ev.get("key") is not None:
                redacted["key"] = "<key>"
            ai_login._dlog("input[%s] %s %s qlen=%d", sid[:8], et, redacted, len(s._events))
        # collapse consecutive hover moves so they can never flood out real input
        if et == "move" and s._events and s._events[-1].get("type") == "move":
            s._events[-1] = ev
        elif et == "move":
            if len(s._events) < 400:
                s._events.append(ev)
        else:
            # Cap the actionable (non-move) backlog hard. The loop drains ~12 per
            # tick; if the user clicks faster than it can apply them (a slow page),
            # an unbounded queue means every click lands many seconds late — so
            # drop new clicks once a short backlog has built rather than pile on.
            pending = sum(1 for e in s._events if e.get("type") != "move")
            if pending < 10:
                s._events.append(ev)
            else:
                return JSONResponse({"ok": True, "dropped": True})
    return JSONResponse({"ok": True})


@router.post("/monitors/{monitor_id}/ai-login/finish")
async def ai_login_finish(
    monitor_id: int, sid: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """User asks to capture the current session and finish."""
    s = ai_login.get_session(sid, user.id)
    if not s or s.monitor_id != monitor_id:
        return JSONResponse({"ok": False, "error": "expired"}, status_code=404)
    s._finish = True
    return JSONResponse({"ok": True})


# --- Stored-cookie viewer / editor -----------------------------------------

@router.get("/monitors/{monitor_id}/cookies")
async def cookies_get(
    monitor_id: int,
    values: int = 0,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """The monitor's stored session cookies as editable rows. Values are only
    included when ?values=1 (the UI fetches them on 'reveal')."""
    monitor = await _owned_monitor(session, user, monitor_id)
    state = monitor.login_flow.session_state if monitor.login_flow else None
    rows = cookie_rows(state, with_values=bool(values))
    return JSONResponse({"ok": True, "cookies": rows, "summary": cookie_summary(state)})


@router.post("/monitors/{monitor_id}/cookies")
async def cookies_save(
    monitor_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Replace the monitor's stored cookies with an edited row set (deletes are
    rows omitted; edits are changed values). Keeps localStorage origins."""
    monitor = await _owned_monitor(session, user, monitor_id)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid request."}, status_code=400)
    edited = body.get("cookies") if isinstance(body, dict) else None
    flow = monitor.login_flow
    if flow is None:
        return JSONResponse({"ok": False, "error": "This monitor has no stored session."}, status_code=400)
    try:
        new_state = apply_cookie_edits(flow.session_state, edited or [])
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    if not new_state["cookies"]:
        flow.session_state = None
        flow.session_valid_until = None
    else:
        flow.session_state = new_state
        flow.session_valid_until = utcnow() + COOKIE_SESSION_TTL
    await session.commit()
    return JSONResponse({"ok": True, "summary": cookie_summary(flow.session_state)})


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
            "checking": is_checking(monitor.id),
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
    # Blobs are a mix of formats (legacy PNG + new WebP) — sniff from magic bytes.
    return FileResponse(p, media_type=_image_media_type(p), headers=headers)


def _image_media_type(path) -> str:
    """Detect image/{webp,png,jpeg} from the file header (defaults to png)."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    return "image/png"


@router.get("/monitors/{monitor_id}/snapshots/{snapshot_id}/image")
async def snapshot_image(
    request: Request,
    monitor_id: int,
    snapshot_id: int,
    v: str | None = None,    # "mobile" for the mobile-viewport capture
    section: int = 0,        # which whole-page section (0 = top); -1 not allowed
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await _owned_monitor(session, user, monitor_id)
    snap = await session.get(Snapshot, snapshot_id)
    if not snap or snap.monitor_id != monitor_id:
        raise HTTPException(404)
    mobile = v == "mobile"
    sections = (snap.screenshot_mobile_sections if mobile else snap.screenshot_sections) or []
    if sections and 0 <= section < len(sections):
        key = sections[section]
    else:
        # Legacy snapshots (no sections) or out-of-range → the primary blob.
        key = snap.screenshot_mobile_blob if mobile else snap.screenshot_blob
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
