"""Visual (screenshot) diffing via a vectorised pixelmatch-equivalent + Pillow."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from ..config import settings

# numpy is the fast path; pixelmatch is the defensive fallback if it's ever absent.
try:
    import numpy as np
except Exception:  # pragma: no cover - numpy is a hard dependency in practice
    np = None
try:
    from pixelmatch.contrib.PIL import pixelmatch
except Exception:  # pragma: no cover
    pixelmatch = None


def _pixel_mismatch(before: Image.Image, after: Image.Image, threshold: float):
    """Count changed pixels and build a highlight overlay.

    Uses the SAME per-pixel decision pixelmatch makes — a YIQ colour-delta compared
    against ``35215·threshold²`` — but vectorised in numpy, so a full-page multi-section
    diff finishes in ~0.1s instead of the 15-21s a single 2 MP pure-Python pixelmatch
    took (which blew the detect ceiling and silently SKIPPED change detection). The one
    thing dropped vs pixelmatch is its anti-aliasing suppression; the min-visual-change
    floor absorbs the small edge-noise difference (validated to give the same
    changed/magnitude verdict as pixelmatch on real captures, incl. true negatives).
    Returns (mismatch_count, overlay RGBA Image)."""
    if np is not None:
        a = np.asarray(before, dtype=np.float32)
        b = np.asarray(after, dtype=np.float32)

        def yiq(x):
            r, g, bl = x[..., 0], x[..., 1], x[..., 2]
            return (r * 0.29889531 + g * 0.58662247 + bl * 0.11448223,
                    r * 0.59597799 - g * 0.27417610 - bl * 0.32180189,
                    r * 0.21147017 - g * 0.52261711 + bl * 0.31114694)

        (y1, i1, q1), (y2, i2, q2) = yiq(a), yiq(b)
        dy, di, dq = y1 - y2, i1 - i2, q1 - q2
        delta = 0.5053 * dy * dy + 0.299 * di * di + 0.1957 * dq * dq
        mask = delta > (35215.0 * threshold * threshold)
        overlay_arr = np.zeros((a.shape[0], a.shape[1], 4), dtype=np.uint8)
        overlay_arr[mask] = (255, 0, 0, 255)          # changed pixels → red highlight
        return int(mask.sum()), Image.fromarray(overlay_arr, "RGBA")

    overlay = Image.new("RGBA", before.size)          # fallback: pure-Python pixelmatch
    mismatch = pixelmatch(before, after, overlay, includeAA=False, threshold=threshold)
    return mismatch, overlay


def _bound(im: Image.Image) -> Image.Image:
    """Downscale an oversized screenshot so diffing a deliberately enormous page
    can't exhaust memory. Keeps aspect ratio; preserves small images unchanged."""
    budget = settings.max_diff_megapixels * 1_000_000
    px = im.width * im.height
    if px <= budget or px == 0:
        return im
    scale = (budget / px) ** 0.5
    return im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))


@dataclass
class VisualDiff:
    changed: bool
    magnitude: float           # fraction of pixels changed (0..1)
    summary: str
    overlay_png: bytes | None   # diff highlight overlay


def _load(png: bytes) -> Image.Image:
    return _bound(Image.open(io.BytesIO(png)).convert("RGB"))


def _fit(a: Image.Image, b: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Pad both images to a common canvas (so height changes are visible)."""
    w = max(a.width, b.width)
    h = max(a.height, b.height)
    ca = Image.new("RGB", (w, h), (255, 255, 255))
    cb = Image.new("RGB", (w, h), (255, 255, 255))
    ca.paste(a, (0, 0))
    cb.paste(b, (0, 0))
    return ca, cb


def diff_sections(before_pngs, after_pngs, *, threshold: float = 0.1,
                  max_sections: int = 8) -> "VisualDiff | None":
    """Whole-page visual diff: compare the stored page sections top-to-bottom and return
    the MOST-changed one, so a visual change BELOW the first section isn't missed (the
    old behaviour only diffed section[0], i.e. the first ~8000 CSS px). Only OVERLAPPING
    sections are compared (zip) — a section-count change is just a page-height reflow,
    which pixel diff shouldn't treat as a guaranteed change. Returns None when either
    side has no sections (caller falls back to the single top-blob diff)."""
    bp = [b for b in (before_pngs or []) if b][:max_sections]
    ap = [a for a in (after_pngs or []) if a][:max_sections]
    if not bp or not ap:
        return None
    best: VisualDiff | None = None
    for b, a in zip(bp, ap):
        try:
            vd = diff_images(b, a, threshold=threshold)
        except Exception:
            continue
        if best is None or vd.magnitude > best.magnitude:
            best = vd
    return best


def diff_images(before_png: bytes, after_png: bytes, *, threshold: float = 0.1) -> VisualDiff:
    before, after = _fit(_load(before_png), _load(after_png))
    # _load bounds each image to the megapixel budget, but _fit pads them to a common
    # canvas that can be larger again when their aspect ratios differ (e.g. a tall
    # section vs a legacy full-page capture). Re-bound the padded canvas so the pure-
    # Python pixelmatch always runs on a bounded image and can't blow the detect timeout.
    before, after = _bound(before), _bound(after)
    mismatch, overlay = _pixel_mismatch(before, after, threshold)

    total = before.width * before.height
    magnitude = mismatch / total if total else 0.0
    changed = mismatch > 0

    buf = io.BytesIO()
    # Composite overlay over the "after" image for an at-a-glance highlight.
    composite = Image.alpha_composite(after.convert("RGBA"), overlay)
    composite.convert("RGB").save(buf, format="PNG")

    summary = (
        f"{magnitude * 100:.2f}% of pixels changed ({mismatch:,} px)"
        if changed
        else "No visual change"
    )
    return VisualDiff(changed, magnitude, summary, buf.getvalue())
