"""The check pipeline: render a monitor, snapshot it, detect & record changes."""

from __future__ import annotations

import asyncio
import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .ai import extract_value, triage_change
from .app_settings import get_app_settings, get_openrouter_key
from .auth.login_flows import session_is_valid
from .config import settings
from .db import SessionLocal
from .detection import detect
from .detection.value import parse_number
from .engines import RenderResult, render_monitor
from .models import Change, DetectionMode, Group, Monitor, Snapshot, SnapshotStatus, utcnow
from .notify import dispatch, notify_monitor_alert
from .storage import blobs

# Importance ratings the AI considers "low value" and subject to the gating policy.
_LOW_VALUE = {"low", "noise"}


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


async def _render(monitor) -> RenderResult:
    """Render with a hard ceiling so a hung browser can't pin a concurrency slot.

    Cancellation propagates into the engine's `async with`, tearing the browser
    down on timeout."""
    try:
        return await asyncio.wait_for(render_monitor(monitor),
                                      timeout=settings.render_timeout_seconds + 30)
    except asyncio.TimeoutError:
        return RenderResult(ok=False, error="Render timed out", http_status=None)


def _is_transient(result) -> bool:
    """A render failure worth retrying — driver/network/5xx, not a clean block."""
    return result.http_status is None or result.http_status >= 500


async def _fail(session, monitor, snap, *, error, http_status, title=None) -> None:
    """Record an error snapshot, bump the failure streak, and alert / auto-pause."""
    flow = monitor.login_flow
    if flow and flow.session_state and not session_is_valid(flow):
        error = (error or "Check failed") + (
            " · stored session has expired — re-paste cookies in this monitor's Session cookies section"
        )
    snap.status = SnapshotStatus.error
    snap.error = error
    snap.http_status = http_status
    snap.title = title
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

# Cap concurrent browser renders to protect a single box.
_semaphore = asyncio.Semaphore(settings.max_render_concurrency)


def _hash(*parts: str | None) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8"))
    return h.hexdigest()


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

            snap = Snapshot(monitor_id=monitor.id, render_ms=result.render_ms)
            monitor.last_checked_at = utcnow()

            if not result.ok:
                await _fail(session, monitor, snap,
                            error=(result.error or "") + _help_hint(result.error),
                            http_status=result.http_status)
                return

            # Surface anti-bot blocks / challenge pages as errors rather than
            # silently storing an empty "ok" snapshot. A captcha/anti-bot block
            # gets actionable "add session cookies" guidance in the alert.
            blocked = _blocked_reason(result)
            if blocked:
                await _fail(session, monitor, snap, error=blocked + _help_hint(blocked),
                            http_status=result.http_status, title=result.title)
                return

            # A render that yielded no usable artifacts (e.g. the browser/driver
            # crashed mid-capture) is a failure, not a healthy empty snapshot.
            if not any((result.html, result.rendered_text, result.extracted_value,
                        result.screenshot_png)):
                await _fail(session, monitor, snap,
                            error="Empty render — the browser returned no content (engine/driver crash or block)",
                            http_status=result.http_status, title=result.title)
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
            if result.html:
                snap.html_blob = blobs.put_text(result.html)
                snap.dom_hash = _hash(result.html)
            if result.screenshot_png:
                snap.screenshot_blob = blobs.put_bytes(result.screenshot_png)
            if result.screenshot_mobile_png:
                snap.screenshot_mobile_blob = blobs.put_bytes(result.screenshot_mobile_png)
            snap.content_hash = _hash(
                result.rendered_text, result.extracted_value, result.html
            )

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

                text_blob = blobs.put_text(change_result.diff_text) if change_result.diff_text else None
                visual_blob = (
                    blobs.put_bytes(change_result.diff_overlay_png)
                    if change_result.diff_overlay_png is not None else None
                )
                visual_mobile_blob = (
                    blobs.put_bytes(change_result.diff_overlay_mobile_png)
                    if change_result.diff_overlay_mobile_png is not None else None
                )

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
