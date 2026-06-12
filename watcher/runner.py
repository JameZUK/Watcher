"""The check pipeline: render a monitor, snapshot it, detect & record changes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .ai import ai_login_action, extract_value, triage_change
from .app_settings import get_app_settings, get_openrouter_key
from .auth.login_flows import (
    build_secret_map,
    mark_session,
    resolve_secrets,
    session_is_valid,
)
from .config import settings
from .db import SessionLocal
from .detection import detect
from .detection.value import parse_number
from .engines import RenderResult, render_monitor
from .models import (
    Change, DetectionMode, Engine, Group, LoginFlow, Monitor, Snapshot, SnapshotStatus, utcnow,
)
from .notify import dispatch, notify_monitor_alert
from .storage import blobs

log = logging.getLogger("watcher")

# Importance ratings the AI considers "low value" and subject to the gating policy.
_LOW_VALUE = {"low", "noise"}

# --- Automatic AI re-login on session expiry --------------------------------
# In-flight monitors (avoid concurrent recoveries) + post-failure cooldowns
# (monotonic deadline). Both in-memory: lost on restart is fine — the per-user AI
# rate-limit is the real budget ceiling; this just avoids hammering.
_relogin_inflight: set[int] = set()
_relogin_tasks: set = set()   # strong refs so recovery tasks aren't GC'd mid-run

# After a stealth (Camoufox) escalation that ALSO got blocked, skip re-escalating
# this monitor for a while so a hard-walled site doesn't double-render every check.
_escalate_cooldown: dict[int, float] = {}


def _relogin_blocked_reason(monitor, flow, app) -> str | None:
    """None if an automatic AI re-login should be attempted for this failed check,
    otherwise a short reason it shouldn't. Cheap checks only (no decryption / I/O)."""
    if not getattr(monitor, "auto_relogin_enabled", False):
        return "not enabled"
    if not flow or not flow.session_state:
        return "no stored session"
    if session_is_valid(flow):
        return "session still valid"        # the failure wasn't a stale session
    # Credentials must exist (cookie-only flows can't be re-driven). build_secret_map
    # only stores keys with non-empty values, so key presence ⇒ a usable value.
    if not {"username", "password"} <= set(flow.encrypted_secrets or {}):
        return "no stored credentials"
    if not (app.ai_enabled and get_openrouter_key(app)):
        return "AI not configured"
    cd = flow.relogin_cooldown_until
    if cd is not None:
        if cd.tzinfo is None:
            cd = cd.replace(tzinfo=timezone.utc)
        if cd > utcnow():
            return "cooldown after a recent failed attempt"
    return None


async def _maybe_auto_relogin(monitor, flow, app) -> bool:
    """Launch an out-of-band AI re-login if this failed check looks like an expired
    login session on an opted-in monitor. Returns True when a recovery is in
    progress (so the caller skips the penalising _fail this cycle), else False."""
    if monitor.id in _relogin_inflight:
        return True                          # already recovering — keep skipping _fail
    if _relogin_blocked_reason(monitor, flow, app) is not None:
        return False
    # Count the attempt against the owner's shared-AI-key budget (records a hit).
    from .web.ratelimit import allow
    if not allow(f"ai:{monitor.user_id}", limit=settings.ai_max_calls,
                 window=settings.ai_window_seconds):
        return False                         # over budget → let it fail normally
    _relogin_inflight.add(monitor.id)
    task = asyncio.create_task(_run_relogin(
        monitor.id, monitor.user_id, app.ai_model, app.ai_base_url, get_openrouter_key(app)))
    _relogin_tasks.add(task)
    task.add_done_callback(_relogin_tasks.discard)
    log.info("auto re-login launched for monitor %s", monitor.id)
    return True


async def _run_relogin(monitor_id: int, user_id: int, model, base_url, key) -> None:
    """Drive the AI login agent headlessly (no human) to refresh an expired session,
    then re-check on success. On any need-a-human stop (captcha/OTP) or error, set a
    cooldown so it doesn't retry every check."""
    from .auth import ai_login
    until = utcnow() + timedelta(seconds=settings.auto_relogin_cooldown_seconds)
    try:
        async with SessionLocal() as s:
            m = (await s.execute(
                select(Monitor).where(Monitor.id == monitor_id)
                .options(selectinload(Monitor.login_flow)))).scalar_one_or_none()
            if m is None or m.login_flow is None:
                return
            creds = resolve_secrets(m.login_flow)
            # Re-login at the recorded login page if there is one (first goto step),
            # otherwise the monitored URL itself.
            login_url = next((st["url"] for st in (m.login_flow.steps or [])
                              if isinstance(st, dict) and st.get("url")), m.url)
            engine, proxy, wait_until = m.engine.value, m.proxy, m.wait_until

        sess = ai_login.create_session(monitor_id, user_id, login_url, creds)

        async def action_fn(**kw):
            return await ai_login_action(api_key=key, model=model, base_url=base_url, **kw)

        async def persist_fn(state):
            async with SessionLocal() as s2:
                m2 = (await s2.execute(
                    select(Monitor).where(Monitor.id == monitor_id)
                    .options(selectinload(Monitor.login_flow)))).scalar_one_or_none()
                if m2 is None:
                    return
                fl = m2.login_flow or LoginFlow(monitor_id=m2.id)
                mark_session(fl, state)
                fl.encrypted_secrets = {**(fl.encrypted_secrets or {}),
                                        **build_secret_map({k: v for k, v in creds.items() if v})}
                if m2.login_flow is None:
                    s2.add(fl)
                await s2.commit()

        await ai_login.run_agent(sess, action_fn, persist_fn, engine=engine, proxy=proxy,
                                 wait_until=wait_until, unattended=True, solve_captcha_fn=None)

        if sess.status == "done":
            await _set_relogin_cooldown(monitor_id, None, notify_ok=True)
            log.info("auto re-login succeeded for monitor %s — re-checking", monitor_id)
            from .scheduler import trigger_now
            trigger_now(monitor_id)
        else:
            await _set_relogin_cooldown(monitor_id, until)
            log.warning("auto re-login failed for monitor %s: %s", monitor_id, sess.error)
    except Exception:
        await _set_relogin_cooldown(monitor_id, until)
        log.exception("auto re-login crashed for monitor %s", monitor_id)
    finally:
        _relogin_inflight.discard(monitor_id)


async def _set_relogin_cooldown(monitor_id: int, until, *, notify_ok: bool = False) -> None:
    """Persist the post-attempt cooldown on the login flow (survives restarts). On a
    successful recovery (until=None, notify_ok), also drop an inbox note."""
    async with SessionLocal() as s:
        m = (await s.execute(
            select(Monitor).where(Monitor.id == monitor_id)
            .options(selectinload(Monitor.login_flow)))).scalar_one_or_none()
        if m is None or m.login_flow is None:
            return
        m.login_flow.relogin_cooldown_until = until
        if notify_ok:
            await notify_monitor_alert(
                s, m, f"Re-logged in: {m.name or m.url}",
                "Watcher signed back in automatically after the session expired — "
                "the monitor is working again.")
        await s.commit()


async def _maybe_group_alert(session, monitor) -> None:
    """If this monitor is in a price group with a target, re-evaluate the BEST
    value across the group and fire one deduped alert when it crosses."""
    if not monitor.group_id:
        return
    group = await session.get(Group, monitor.group_id)
    if group is None or group.target_value is None or group.target_dir not in ("below", "above"):
        return
    below = group.target_dir == "below"

    member_ids = (await session.execute(
        select(Monitor.id).where(Monitor.group_id == group.id))).scalars().all()
    best, best_id = None, None
    for mid in member_ids:
        v = (await session.execute(
            select(Snapshot.numeric_value).where(
                Snapshot.monitor_id == mid, Snapshot.numeric_value.is_not(None),
                Snapshot.status == SnapshotStatus.ok)
            .order_by(Snapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
        if v is None:
            continue
        if best is None or (below and v < best) or (not below and v > best):
            best, best_id = v, mid
    if best is None:
        return

    crossed = (below and best < group.target_value) or (not below and best > group.target_value)
    if crossed and not group.alert_active:
        group.alert_active = True
        bestmon = await session.get(Monitor, best_id)
        to_snap = (await session.execute(
            select(Snapshot.id).where(Snapshot.monitor_id == best_id, Snapshot.status == SnapshotStatus.ok)
            .order_by(Snapshot.taken_at.desc()).limit(1))).scalar_one_or_none()
        label = (await session.execute(
            select(Snapshot.value_label).where(
                Snapshot.monitor_id == best_id, Snapshot.numeric_value.is_not(None))
            .order_by(Snapshot.taken_at.desc()).limit(1))).scalar_one_or_none() or f"{best:g}"
        word = "dropped below" if below else "rose above"
        which = "cheapest" if below else "best"
        msg = (f"{group.name}: {which} now {label} at {_host(bestmon.url)} — "
               f"{word} your target of {group.target_value:g}.")
        if to_snap and bestmon is not None:
            ch = Change(monitor_id=best_id, to_snapshot_id=to_snap,
                        change_type=DetectionMode.auto, summary=msg, ai_headline=msg,
                        ai_category="price", ai_importance="high", magnitude=0.0)
            session.add(ch)
            bestmon.last_change_at = utcnow()
            await session.flush()
            await dispatch(session, bestmon, ch)
    elif not crossed and group.alert_active:
        group.alert_active = False   # recovered — re-arm for the next crossing


def _target_block_reason(monitor) -> str | None:
    """Return why a monitor's network targets resolve to an internal address
    (SSRF), or None. Transient-tolerant (a DNS failure is not a block, so a
    resolver blip doesn't auto-pause a legit monitor). Resolves DNS — call off
    the event loop."""
    from .netsec import proxy_block_reason, render_block_reason
    err = render_block_reason(monitor.url) or proxy_block_reason(monitor.proxy)
    if err:
        return err
    flow = getattr(monitor, "login_flow", None)
    for step in (flow.steps if flow and flow.steps else []):
        if step.get("action") == "goto" and step.get("url"):
            if (e := render_block_reason(step["url"])):
                return e
    return None


def _combine_intent(group_intent: str | None, mon_intent: str | None) -> str | None:
    """Merge a group's shared watch intent with a monitor's own — non-destructive,
    so a page that already has its own intent keeps it AND gets the group's."""
    parts = []
    if (group_intent or "").strip():
        parts.append(f"Group goal: {group_intent.strip()}")
    if (mon_intent or "").strip():
        parts.append(f"This page specifically: {mon_intent.strip()}")
    return "  ".join(parts) or None


async def _maybe_triage(app, monitor, change_result, result, intent=None):
    """Best-effort AI triage of a detected change. Returns a Triage or None."""
    if not (monitor.ai_enabled and app.ai_enabled):
        return None
    key = get_openrouter_key(app)
    if not key:
        return None
    has_text = bool((change_result.diff_text or "").strip())
    image = None if has_text else (change_result.diff_overlay_png or result.screenshot_png)
    return await triage_change(
        api_key=key, model=app.ai_model, base_url=app.ai_base_url, url=monitor.url, title=result.title,
        intent=intent if intent is not None else monitor.ai_watch_intent,
        diff_text=change_result.diff_text, image_png=image,
    )


async def _extract_value(app, monitor, result):
    """Extract the monitor's tracked numeric value: a parsed selector/value
    first (free), else the AI. Returns (value, label) or None."""
    if (result.extracted_value or "").strip():
        parsed = parse_number(result.extracted_value)
        if parsed:
            return parsed
    if monitor.ai_enabled and app.ai_enabled:
        key = get_openrouter_key(app)
        if key and (result.rendered_text or "").strip():
            return await extract_value(
                api_key=key, model=app.ai_model, base_url=app.ai_base_url, url=monitor.url,
                title=result.title, page_text=result.rendered_text,
            )
    return None


async def _maybe_learn_consent(app, monitor, result) -> None:
    """Self-healing AI fallback: when the automatic handler leaves a consent
    banner / large overlay showing, ask the model ONCE for dismiss selectors and
    cache them on the monitor so the next render clears it. No-op unless AI is on,
    the monitor blocks annoyances, and an obstruction remains. Bounded to a SINGLE
    AI call per monitor (consent_ai_tried), even when the model returns nothing,
    so a persistent unsolvable overlay can't re-spend tokens every render."""
    if not getattr(monitor, "block_annoyances", True):
        return
    snippets = getattr(result, "unhandled_consent_html", None)
    if not snippets:
        return
    # Already have manual/learned selectors, or already spent our one AI attempt.
    if monitor.consent_clicks or getattr(monitor, "consent_ai_tried", False):
        return
    if not (monitor.ai_enabled and app.ai_enabled):
        return
    key = get_openrouter_key(app)
    if not key:
        return
    from .ai import suggest_consent_selectors
    monitor.consent_ai_tried = True          # one-shot, regardless of outcome
    sels = await suggest_consent_selectors(
        api_key=key, model=app.ai_model, base_url=app.ai_base_url,
        url=monitor.url, html_snippets=snippets,
    )
    if sels:
        monitor.consent_clicks = sels[:15]   # persisted by the caller's commit
        logging.getLogger("watcher").info(
            "AI consent: learned %d dismiss selector(s) for monitor %s", len(sels), monitor.id)


def _adapt_interval(monitor, changed: bool) -> None:
    """Auto-tune the check cadence: faster when changing, slower when stable."""
    cur = monitor.interval_seconds
    lo, hi = settings.min_interval_seconds, 24 * 3600
    new = max(lo, int(cur * 0.5)) if changed else min(hi, int(cur * 1.3))
    if new != cur:
        monitor.interval_seconds = new
        try:  # lazy import to avoid a scheduler<->runner import cycle
            from .scheduler import retune_interval
            retune_interval(monitor.id, new)   # in-place; safe from inside the job
        except Exception:
            logging.getLogger("watcher").warning("adaptive retune failed for monitor %s", monitor.id)


async def _render(monitor, engine=None) -> RenderResult:
    """Render with a hard ceiling so a hung browser can't pin a concurrency slot.
    `engine` overrides the monitor's configured engine (for stealth escalation).

    Cancellation propagates into the engine's `async with`, tearing the browser
    down on timeout."""
    import contextlib
    from .auth.ai_login import engine_error_message
    # Camoufox is much heavier (RAM) than the Playwright browsers, so cap its
    # concurrency separately (nested inside the global render semaphore) to keep a
    # batch of stealth checks from OOM-ing the box.
    eff = engine or monitor.engine
    slot = _camoufox_semaphore if eff == Engine.camoufox else contextlib.nullcontext()
    try:
        async with slot:
            result = await asyncio.wait_for(render_monitor(monitor, engine=engine),
                                            timeout=settings.render_timeout_seconds + 30)
    except asyncio.TimeoutError:
        return RenderResult(ok=False, error="Render timed out", http_status=None)
    except Exception as exc:  # noqa: BLE001
        return RenderResult(ok=False, http_status=None,
                            error=engine_error_message(exc) or f"Render failed: {type(exc).__name__}: {exc}")
    # Collapse a raw 'missing libraries' wall (e.g. WebKit without its deps) into
    # one clear line.
    if not result.ok and result.error and (friendly := engine_error_message(result.error)):
        result.error = friendly
    return result


def _is_transient(result) -> bool:
    """A render failure worth retrying — driver/network/5xx, not a clean block."""
    return result.http_status is None or result.http_status >= 500


async def _fail(session, monitor, snap, *, error, http_status, title=None, result=None) -> None:
    """Record an error snapshot, bump the failure streak, and alert / auto-pause.

    When a render produced a page (a block / captcha / challenge interstitial),
    `result` carries it — we persist the screenshot + HTML on the error snapshot
    so block pages can be reviewed afterwards (they're content-addressed, so
    repeated identical blocks dedupe to a single blob)."""
    flow = monitor.login_flow
    if flow and flow.session_state and not session_is_valid(flow):
        error = (error or "Check failed") + (
            " · stored session has expired — re-paste cookies in this monitor's Session cookies section"
        )
    snap.status = SnapshotStatus.error
    snap.error = error
    snap.http_status = http_status
    snap.title = title
    if result is not None:
        try:
            _b = await asyncio.to_thread(_store_snapshot_blobs, result)
            snap.screenshot_blob = _b["screenshot_blob"]
            snap.screenshot_mobile_blob = _b["screenshot_mobile_blob"]
            snap.html_blob = _b["html_blob"]
            snap.dom_hash = _b["dom_hash"]
            if result.title and not snap.title:
                snap.title = result.title
        except Exception:
            logging.getLogger("watcher").warning(
                "could not persist block-page capture for monitor %s", monitor.id)
    session.add(snap)
    prev = monitor.consecutive_failures or 0
    monitor.consecutive_failures = prev + 1
    await session.commit()

    n = monitor.consecutive_failures
    threshold = settings.auto_pause_after_failures
    if threshold and n >= threshold and monitor.enabled:
        monitor.enabled = False
        await session.commit()
        try:  # lazy import to avoid a scheduler<->runner import cycle
            from .scheduler import unschedule_monitor
            unschedule_monitor(monitor.id)
        except Exception:
            pass
        await notify_monitor_alert(
            session, monitor, f"Paused: {monitor.name}",
            f"Auto-paused after {n} consecutive failures.\n{error}",
        )
    elif prev == 0:
        await notify_monitor_alert(
            session, monitor, f"Monitor failing: {monitor.name}", error or "Check failed",
        )


def _threshold_crossing(monitor, prev_val, new_val, label):
    """If the tracked value just crossed the alert threshold, return a message."""
    t = monitor.value_threshold
    if t is None or new_val is None or monitor.value_threshold_dir not in ("below", "above"):
        return None
    below = monitor.value_threshold_dir == "below"
    now_hit = new_val < t if below else new_val > t
    prev_hit = prev_val is not None and (prev_val < t if below else prev_val > t)
    if now_hit and not prev_hit:
        shown = label or f"{new_val:g}"
        return f"Value dropped {'below' if below else 'above'} {t:g} — now {shown}"
    return None

# Cap concurrent browser renders to protect a single box. Camoufox gets a tighter
# nested cap (it's far more RAM-hungry than the Playwright browsers).
_semaphore = asyncio.Semaphore(settings.max_render_concurrency)
_camoufox_semaphore = asyncio.Semaphore(settings.max_camoufox_concurrency)


def _hash(*parts: str | None) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
    return h.hexdigest()


def _store_snapshot_blobs(result) -> dict:
    """Hash + write a snapshot's artifacts to the content-addressed blob store.
    sha256 over multi-MB screenshots plus file writes is CPU/IO-bound, so this is
    meant to run via ``asyncio.to_thread`` and keep the event loop free."""
    out: dict = {"html_blob": None, "dom_hash": None, "screenshot_blob": None,
                 "screenshot_mobile_blob": None}
    if result.html:
        out["html_blob"] = blobs.put_text(result.html)
        out["dom_hash"] = _hash(result.html)
    if result.screenshot_png:
        out["screenshot_blob"] = blobs.put_bytes(result.screenshot_png)
    if getattr(result, "screenshot_mobile_png", None):
        out["screenshot_mobile_blob"] = blobs.put_bytes(result.screenshot_mobile_png)
    out["content_hash"] = _hash(getattr(result, "rendered_text", None),
                                getattr(result, "extracted_value", None), result.html)
    return out


def _store_change_blobs(cr) -> tuple:
    """Write a change's diff artifacts off-thread (text diff + overlay PNGs)."""
    return (
        blobs.put_text(cr.diff_text) if cr.diff_text else None,
        blobs.put_bytes(cr.diff_overlay_png) if cr.diff_overlay_png is not None else None,
        blobs.put_bytes(cr.diff_overlay_mobile_png) if cr.diff_overlay_mobile_png is not None else None,
    )


def _host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc or url


# Signatures of common anti-bot / challenge interstitials.
_ANTIBOT = [
    ("captcha-delivery.com", "DataDome"),
    ("datadome", "DataDome"),
    ("/cdn-cgi/challenge", "Cloudflare"),
    ("cf-browser-verification", "Cloudflare"),
    ("just a moment", "Cloudflare"),
    ("perimeterx", "PerimeterX"),
    ("px-captcha", "PerimeterX"),
    ("/_incapsula_", "Imperva Incapsula"),
    ("access denied", "WAF"),
]


def _blocked_reason(result) -> str | None:
    """Return a human-readable reason if a render was blocked/challenged,
    otherwise None. Catches both HTTP error statuses and 200-status
    challenge pages that contain no real content."""
    html = (result.html or "").lower()
    vendor = next((name for sig, name in _ANTIBOT if sig in html), None)
    status = result.http_status

    if status and status >= 400:
        base = f"Blocked — HTTP {status}"
        return f"{base} · {vendor} anti-bot protection" if vendor else base
    if vendor and not (result.rendered_text or "").strip():
        return f"Blocked — {vendor} challenge page (no content rendered)"
    return None


_HELP_MARKERS = ("datadome", "cloudflare", "perimeterx", "incapsula", "anti-bot",
                 "captcha", "challenge", "waf", "http 401", "http 403", "access denied")


def _help_hint(reason: str | None) -> str:
    """If a failure looks like a captcha / anti-bot wall or a login gate, append
    concrete, actionable guidance so the alert tells the user how to fix it. A
    headless browser (even stealth) can't solve an image/slider captcha, so the
    honest remedy is a real session cookie."""
    r = (reason or "").lower()
    if not any(m in r for m in _HELP_MARKERS):
        return ""
    return (
        "\n\nNeeds your help: this page is guarded by a captcha / anti-bot system "
        "(or requires login) that automated stealth can't reliably pass. Open this "
        "monitor → Session cookies and paste a Cookie Editor JSON export taken from "
        "a browser where the page loads normally — sign in first for login-gated "
        "pages. That carries the clearance/login cookie so future checks succeed."
    )


async def check_monitor(monitor_id: int) -> None:
    """Run one full check cycle for a monitor. Safe to call concurrently."""
    async with _semaphore:
        async with SessionLocal() as session:
            monitor = (
                await session.execute(
                    select(Monitor)
                    .where(Monitor.id == monitor_id)
                    .options(selectinload(Monitor.login_flow))
                )
            ).scalar_one_or_none()
            if monitor is None or not monitor.enabled:
                return

            prev = (
                await session.execute(
                    select(Snapshot)
                    .where(Snapshot.monitor_id == monitor_id,
                           Snapshot.status == SnapshotStatus.ok)
                    .order_by(Snapshot.taken_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            # SSRF gate: re-validate the target just before navigating, so a
            # monitor created before this check existed — or one whose DNS now
            # points at an internal address — is refused. Resolution is blocking,
            # so it runs off the event loop.
            if not settings.allow_private_targets:
                target_err = await asyncio.to_thread(_target_block_reason, monitor)
                if target_err:
                    snap = Snapshot(monitor_id=monitor.id)
                    await _fail(session, monitor, snap,
                                error=f"Blocked internal target — {target_err}", http_status=None)
                    return

            result = await _render(monitor)
            # Retry transient render failures (driver crash / network / 5xx) with backoff.
            attempt = 0
            while (not result.ok) and _is_transient(result) and attempt < settings.render_retries:
                attempt += 1
                await asyncio.sleep(settings.retry_backoff_seconds * attempt)
                result = await _render(monitor)

            # Auto-escalate to stealth: if a non-Camoufox engine got bot-walled,
            # retry once on Camoufox. If that clears the wall, switch the monitor to
            # Camoufox permanently (so future checks skip the double render) and note
            # it. A failed escalation sets a short cooldown to avoid double-rendering
            # every blocked check when stealth doesn't help either.
            if (_blocked_reason(result) and monitor.engine != Engine.camoufox
                    and time.monotonic() >= _escalate_cooldown.get(monitor.id, 0.0)):
                alt = await _render(monitor, engine=Engine.camoufox)
                if alt.ok and not _blocked_reason(alt):
                    log.info("monitor %s: %s on %s — cleared on Camoufox; switching engine",
                             monitor.id, monitor.engine.value, monitor.engine.value)
                    result = alt
                    monitor.engine = Engine.camoufox
                    _escalate_cooldown.pop(monitor.id, None)
                else:
                    _escalate_cooldown[monitor.id] = time.monotonic() + 3600

            snap = Snapshot(monitor_id=monitor.id, render_ms=result.render_ms)
            monitor.last_checked_at = utcnow()
            blocked = _blocked_reason(result)

            # Auto re-login: when a check fails or is blocked AND the stored login
            # session has expired AND the monitor opted in, fire an out-of-band AI
            # re-login (no human). Record a soft, non-penalising error this cycle and
            # return — a fresh check runs once the session is refreshed. If it isn't
            # eligible (no creds, cooldown, over budget, …) fall through to _fail.
            if not result.ok or blocked:
                app = await get_app_settings(session)
                if await _maybe_auto_relogin(monitor, monitor.login_flow, app):
                    snap.status = SnapshotStatus.error
                    snap.error = "Login session expired — automatic re-login in progress…"
                    snap.http_status = result.http_status
                    session.add(snap)
                    await session.commit()
                    return

            if not result.ok:
                await _fail(session, monitor, snap,
                            error=(result.error or "") + _help_hint(result.error),
                            http_status=result.http_status, result=result)
                return

            # Surface anti-bot blocks / challenge pages as errors rather than
            # silently storing an empty "ok" snapshot. A captcha/anti-bot block
            # gets actionable "add session cookies" guidance in the alert, and the
            # block page itself is saved (result=) for later review.
            if blocked:
                await _fail(session, monitor, snap, error=blocked + _help_hint(blocked),
                            http_status=result.http_status, title=result.title, result=result)
                return

            # A render that yielded no usable artifacts (e.g. the browser/driver
            # crashed mid-capture) is a failure, not a healthy empty snapshot.
            if not any((result.html, result.rendered_text, result.extracted_value,
                        result.screenshot_png)):
                await _fail(session, monitor, snap,
                            error="Empty render — the browser returned no content (engine/driver crash or block)",
                            http_status=result.http_status, title=result.title, result=result)
                return

            # Success — clear any failure streak (and announce a recovery).
            if monitor.consecutive_failures:
                if monitor.consecutive_failures >= 2:
                    await notify_monitor_alert(session, monitor,
                                               f"Recovered: {monitor.name}", "The monitor is working again.")
                monitor.consecutive_failures = 0

            # Persist artifacts to the content-addressed blob store.
            snap.status = SnapshotStatus.ok
            snap.http_status = result.http_status
            snap.title = result.title
            snap.rendered_text = result.rendered_text
            snap.extracted_value = result.extracted_value
            # Hash + write blobs off the event loop (sha256 over multi-MB PNGs).
            _b = await asyncio.to_thread(_store_snapshot_blobs, result)
            snap.html_blob = _b["html_blob"]
            snap.dom_hash = _b["dom_hash"]
            snap.screenshot_blob = _b["screenshot_blob"]
            snap.screenshot_mobile_blob = _b["screenshot_mobile_blob"]
            snap.content_hash = _b["content_hash"]

            # Auto-populate the monitor name from the page title if left blank
            # (or still on the legacy "Untitled" placeholder).
            if (monitor.name or "").strip().lower() in ("", "untitled") and (result.title or "").strip():
                monitor.name = (result.title or _host(monitor.url)).strip()[:255]

            # Update persisted login session if the engine refreshed it.
            if result.session_state is not None and monitor.login_flow is not None:
                monitor.login_flow.session_state = result.session_state

            session.add(snap)
            await session.flush()  # assign snap.id

            app = await get_app_settings(session)

            # Self-healing: if a consent banner survived the automatic handler,
            # learn dismiss selectors via AI (once) so the next render is clean.
            await _maybe_learn_consent(app, monitor, result)

            # Value tracking: capture a numeric value each check for trends and
            # threshold alerts (e.g. price drops below a target).
            threshold_msg = None
            if monitor.track_value:
                # Reuse the prior value when the page is byte-identical (skips a
                # per-check AI extraction call on unchanged pages).
                if (prev is not None and prev.numeric_value is not None
                        and snap.content_hash and snap.content_hash == prev.content_hash):
                    snap.numeric_value, snap.value_label = prev.numeric_value, prev.value_label
                else:
                    extracted = await _extract_value(app, monitor, result)
                    if extracted:
                        snap.numeric_value, snap.value_label = extracted
                # Baseline = the last snapshot that actually had a value, so
                # intermittent extraction doesn't re-fire the same crossing.
                baseline = prev.numeric_value if (prev and prev.numeric_value is not None) else None
                if baseline is None:
                    baseline = (await session.execute(
                        select(Snapshot.numeric_value)
                        .where(Snapshot.monitor_id == monitor.id,
                               Snapshot.numeric_value.is_not(None), Snapshot.id != snap.id)
                        .order_by(Snapshot.id.desc()).limit(1)
                    )).scalar_one_or_none()
                threshold_msg = _threshold_crossing(
                    monitor, baseline, snap.numeric_value, snap.value_label,
                )

            # Diffing (PIL/pixelmatch + blob reads + user-supplied ignore regex)
            # is CPU/IO heavy and could hang on a pathological pattern — off-thread
            # with a hard ceiling so it can't pin the pipeline.
            try:
                change_result = await asyncio.wait_for(
                    asyncio.to_thread(detect, monitor, prev, result),
                    timeout=settings.detect_timeout_seconds,
                )
            except (asyncio.TimeoutError, RecursionError) as exc:
                # Timeout (e.g. pathological ignore-regex) or deep-recursion on a
                # malicious deeply-nested JSON page — keep the captured snapshot,
                # skip change detection for this cycle.
                logging.getLogger("watcher").warning(
                    "detect() aborted for monitor %s (%s)", monitor.id, type(exc).__name__)
                await session.commit()
                return
            if change_result.changed or threshold_msg:
                # Combine the group's shared watch-intent with the monitor's own
                # (non-destructive) so members are triaged against the group goal too.
                effective_intent = monitor.ai_watch_intent
                if monitor.group_id:
                    g = await session.get(Group, monitor.group_id)
                    effective_intent = _combine_intent(g.watch_intent if g else None, monitor.ai_watch_intent)
                # AI triage only when there's a real content change to summarise.
                triage = await _maybe_triage(app, monitor, change_result, result, effective_intent) if change_result.changed else None
                importance = triage.importance if triage else None
                # A threshold crossing is always notable and overrides muting.
                if threshold_msg:
                    importance = "high"
                low_value = importance in _LOW_VALUE
                policy = monitor.ai_policy or app.ai_low_value_policy  # silent|label|drop

                # "drop": don't even record a low-value change (snapshot is kept).
                if low_value and policy == "drop":
                    await session.commit()
                    return

                text_blob, visual_blob, visual_mobile_blob = await asyncio.to_thread(
                    _store_change_blobs, change_result)

                headline = threshold_msg or (triage.headline if triage else None)
                change = Change(
                    monitor_id=monitor.id,
                    from_snapshot_id=prev.id if prev else None,
                    to_snapshot_id=snap.id,
                    change_type=change_result.change_type,
                    summary=(headline or change_result.summary),
                    magnitude=change_result.magnitude,
                    diff_blob=text_blob,
                    visual_blob=visual_blob,
                    visual_mobile_blob=visual_mobile_blob,
                    ai_headline=headline,
                    ai_category=("price" if threshold_msg else (triage.category if triage else None)),
                    ai_importance=importance,
                )
                session.add(change)
                monitor.last_change_at = utcnow()
                await session.flush()

                # "silent": record the change but don't push/webhook for low value.
                if not (low_value and policy == "silent"):
                    await dispatch(session, monitor, change)

            # Group-level alert: cheapest/best across the price group crosses target.
            if monitor.track_value and monitor.group_id:
                await _maybe_group_alert(session, monitor)

            if monitor.adaptive_interval:
                _adapt_interval(monitor, bool(change_result.changed or threshold_msg))

            await session.commit()
