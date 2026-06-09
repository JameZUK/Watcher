"""Visual (screenshot) diffing via pixelmatch + Pillow."""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image
from pixelmatch.contrib.PIL import pixelmatch


@dataclass
class VisualDiff:
    changed: bool
    magnitude: float           # fraction of pixels changed (0..1)
    summary: str
    overlay_png: bytes | None   # diff highlight overlay


def _load(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


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
    overlay = Image.new("RGBA", before.size)
    mismatch = pixelmatch(before, after, overlay, includeAA=True, threshold=threshold)

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
