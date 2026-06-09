"""The check pipeline: render a monitor, snapshot it, detect & record changes."""

from __future__ import annotations

import asyncio
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import settings
from .db import SessionLocal
from .detection import detect
from .engines import render_monitor
from .models import Change, Monitor, Snapshot, SnapshotStatus, utcnow
from .notify import dispatch
from .storage import blobs

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

            result = await render_monitor(monitor)

            snap = Snapshot(monitor_id=monitor.id, render_ms=result.render_ms)
            monitor.last_checked_at = utcnow()

            if not result.ok:
                snap.status = SnapshotStatus.error
                snap.error = result.error
                snap.http_status = result.http_status
                session.add(snap)
                await session.commit()
                return

            # Surface anti-bot blocks / challenge pages as errors rather than
            # silently storing an empty "ok" snapshot.
            blocked = _blocked_reason(result)
            if blocked:
                snap.status = SnapshotStatus.error
                snap.error = blocked
                snap.http_status = result.http_status
                snap.title = result.title
                session.add(snap)
                await session.commit()
                return

            # A render that yielded no usable artifacts (e.g. the browser/driver
            # crashed mid-capture) is a failure, not a healthy empty snapshot.
            if not any((result.html, result.rendered_text, result.extracted_value,
                        result.screenshot_png)):
                snap.status = SnapshotStatus.error
                snap.error = "Empty render — the browser returned no content (engine/driver crash or block)"
                snap.http_status = result.http_status
                snap.title = result.title
                session.add(snap)
                await session.commit()
                return

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

            change_result = detect(monitor, prev, result)
            if change_result.changed:
                text_blob = blobs.put_text(change_result.diff_text) if change_result.diff_text else None
                visual_blob = (
                    blobs.put_bytes(change_result.diff_overlay_png)
                    if change_result.diff_overlay_png is not None else None
                )

                change = Change(
                    monitor_id=monitor.id,
                    from_snapshot_id=prev.id if prev else None,
                    to_snapshot_id=snap.id,
                    change_type=change_result.change_type,
                    summary=change_result.summary,
                    magnitude=change_result.magnitude,
                    diff_blob=text_blob,
                    visual_blob=visual_blob,
                )
                session.add(change)
                monitor.last_change_at = utcnow()
                await session.flush()
                await dispatch(session, monitor, change)

            await session.commit()
