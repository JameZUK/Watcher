"""Top-level change detection: compare a new render against the prior snapshot."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings
from ..engines.base import RenderResult
from ..models import DetectionMode, Monitor, Snapshot
from ..storage import blobs
from . import structured, text, visual
from .noise import normalize_text


def _visual_floor(monitor: Monitor) -> float:
    """Effective minimum visual change: the monitor's own threshold, but never
    below the global noise floor that absorbs render jitter / dynamic chrome."""
    return max(monitor.min_change_threshold or 0.0, settings.min_visual_change)


@dataclass
class ChangeResult:
    changed: bool
    change_type: DetectionMode
    magnitude: float
    summary: str
    diff_text: str | None = None
    diff_overlay_png: bytes | None = None
    diff_overlay_mobile_png: bytes | None = None


def _mobile_overlay(prev: Snapshot, current: RenderResult) -> bytes | None:
    """Best-effort mobile-viewport visual overlay, mirroring the desktop one.

    Returns None unless both the previous and current renders have a mobile
    screenshot (so blocked/legacy snapshots simply yield no mobile overlay)."""
    before = blobs.get_bytes(prev.screenshot_mobile_blob) if prev.screenshot_mobile_blob else None
    after = current.screenshot_mobile_png
    if before is None or after is None:
        return None
    vd = visual.diff_images(before, after)
    return vd.overlay_png if vd.changed else None


def _norm(monitor: Monitor, value: str | None) -> str:
    return normalize_text(
        value or "",
        whitespace=monitor.normalize_whitespace,
        numbers=monitor.normalize_numbers,
        ignore_patterns=monitor.ignore_patterns,
    )


def detect(monitor: Monitor, prev: Snapshot | None, current: RenderResult) -> ChangeResult:
    mode = monitor.detection_mode

    # No previous snapshot → this is the baseline, not a change.
    if prev is None:
        return ChangeResult(False, mode, 0.0, "Baseline snapshot")

    if mode == DetectionMode.auto:
        return _detect_auto(monitor, prev, current)

    if mode == DetectionMode.element:
        before = (prev.extracted_value or "").strip()
        after = (current.extracted_value or "").strip()
        changed = before != after
        summary = f"{before!r} → {after!r}" if changed else "Unchanged"
        return ChangeResult(changed, mode, 1.0 if changed else 0.0, summary,
                            diff_text=summary if changed else None)

    if mode == DetectionMode.visual:
        before_png = blobs.get_bytes(prev.screenshot_blob) if prev.screenshot_blob else None
        if before_png is None or current.screenshot_png is None:
            return ChangeResult(False, mode, 0.0, "No screenshot to compare")
        vd = visual.diff_images(before_png, current.screenshot_png)
        changed = vd.changed and vd.magnitude >= _visual_floor(monitor)
        return ChangeResult(changed, mode, vd.magnitude, vd.summary,
                            diff_overlay_png=vd.overlay_png if changed else None,
                            diff_overlay_mobile_png=_mobile_overlay(prev, current) if changed else None)

    if mode == DetectionMode.html:
        before = blobs.get_text(prev.html_blob) if prev.html_blob else ""
        after = current.html or ""
        td = structured.diff_html(before, after)
    elif mode == DetectionMode.json:
        # Both sides must be the canonical (normalized, sorted) JSON text — for a
        # JSON endpoint that's rendered_text, not the browser-wrapped html_blob.
        before = prev.rendered_text or ""
        after = current.rendered_text or current.html or ""
        td = structured.diff_json(before, after)
    else:  # text
        td = text.diff_text(_norm(monitor, prev.rendered_text), _norm(monitor, current.rendered_text))

    changed = td.changed and td.magnitude >= monitor.min_change_threshold
    return ChangeResult(changed, mode, td.magnitude, td.summary,
                        diff_text=td.unified if changed else None)


def _detect_auto(monitor: Monitor, prev: Snapshot, current: RenderResult) -> ChangeResult:
    """Smart mode: detect both content (text) and appearance (visual) changes,
    and surface whichever changed — with both a text diff and a visual overlay."""
    td = text.diff_text(_norm(monitor, prev.rendered_text), _norm(monitor, current.rendered_text))
    text_changed = td.changed and td.magnitude >= monitor.min_change_threshold

    vd = None
    visual_changed = False
    before_png = blobs.get_bytes(prev.screenshot_blob) if prev.screenshot_blob else None
    if before_png is not None and current.screenshot_png is not None:
        vd = visual.diff_images(before_png, current.screenshot_png)
        visual_changed = vd.changed and vd.magnitude >= _visual_floor(monitor)

    if not (text_changed or visual_changed):
        return ChangeResult(False, DetectionMode.auto, 0.0, "No change")

    parts = []
    if text_changed:
        parts.append(td.summary.lower())
    if visual_changed and vd is not None:
        parts.append(vd.summary.lower())
    summary = "Content & appearance changed — " + "; ".join(parts) if (text_changed and visual_changed) \
        else ("Content changed — " + td.summary if text_changed else "Appearance changed — " + vd.summary)

    magnitude = max(td.magnitude if text_changed else 0.0, vd.magnitude if visual_changed else 0.0)
    return ChangeResult(
        changed=True,
        change_type=DetectionMode.auto,
        magnitude=magnitude,
        summary=summary,
        diff_text=td.unified if text_changed else None,
        diff_overlay_png=vd.overlay_png if (visual_changed and vd is not None) else None,
        diff_overlay_mobile_png=_mobile_overlay(prev, current) if visual_changed else None,
    )
