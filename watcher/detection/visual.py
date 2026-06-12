"""Visual (screenshot) diffing via pixelmatch + Pillow."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch

from ..config import settings


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


def diff_images(before_png: bytes, after_png: bytes, *, threshold: float = 0.1) -> VisualDiff:
    before, after = _fit(_load(before_png), _load(after_png))
    # _load bounds each image to the megapixel budget, but _fit pads them to a common
    # canvas that can be larger again when their aspect ratios differ (e.g. a tall
    # section vs a legacy full-page capture). Re-bound the padded canvas so the pure-
    # Python pixelmatch always runs on a bounded image and can't blow the detect timeout.
    before, after = _bound(before), _bound(after)
    overlay = Image.new("RGBA", before.size)
    # includeAA=False → pixelmatch detects and *ignores* anti-aliased pixels, so
    # sub-pixel font/edge rendering jitter between otherwise-identical renders
    # doesn't register as a change (a big source of phantom diffs).
    mismatch = pixelmatch(before, after, overlay, includeAA=False, threshold=threshold)

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
