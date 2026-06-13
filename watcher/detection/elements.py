"""Content-anchored change localization from per-snapshot element maps.

The element map (captured in ``engines/_common.capture_element_map``) lists a page's
repeated content blocks with each block's full-page bounding box in *that render's own
coordinates*, plus the page width/height and devicePixelRatio. Because a block's
identity is its (digit-masked) content — not its pixel position — we can:

  * diff two maps regardless of render width/offset (Camoufox randomises width; an
    insertion shifts everything below), and
  * crop the changed block out of each snapshot's OWN screenshot, mapping the CSS
    bbox through the same dpr + section slicing the capture used.

This is the resolution-independent replacement for pixel/heat-map diffing, which is
unreliable on reflowing pages.
"""
from __future__ import annotations

import difflib
import io
import re
from collections import Counter

from ..config import settings


def classify_block(snippet: str) -> str:
    """Best-effort generic content-type for a block, by SHAPE (no per-site logic):
    a review has pros+cons; a job has an apply/view control; a rating/comparison row
    has a star number + appraisal words. Falls back to 'item'."""
    t = (snippet or "").lower()
    if re.search(r"\bpros\b", t) and re.search(r"\bcons\b", t):
        return "review"
    if re.search(r"\bview job\b|\bapply now\b|\bapply\b", t):
        return "job"
    if re.search(r"\b[1-5]\.\d\b", t) and re.search(
            r"recommend|compensation|culture|management|benefits|outlook", t):
        return "rating/comparison"
    return "item"


def summarize_diff(diff: dict) -> str:
    """A short, typed, human-readable summary of which content blocks were added /
    removed — reliable structured grounding for triage ('what items actually
    changed, and of what kind')."""
    lines = []
    for label, key in (("ADDED", "added"), ("REMOVED", "removed")):
        blocks = diff.get(key) or []
        if not blocks:
            continue
        types = Counter(classify_block(b.get("s")) for b in blocks)
        tdesc = ", ".join(f"{n}× {t}" for t, n in types.most_common())
        examples = " | ".join((b.get("s") or "")[:90].strip() for b in blocks[:3])
        lines.append(f"{label} {len(blocks)} content block(s) — {tdesc}. e.g.: {examples}")
    return "\n".join(lines)


def _pick(blocks: list) -> dict | None:
    """Choose a representative block to crop: prefer a review, then the largest area
    (usually the container — most surrounding context)."""
    if not blocks:
        return None
    return sorted(blocks, key=lambda b: (classify_block(b.get("s")) != "review",
                                         -((b.get("w") or 0) * (b.get("h") or 0))))[0]


def localize_change(before_map: dict | None, after_map: dict | None,
                    after_sections: list | None, before_sections: list | None) -> dict:
    """Content-anchored localization for a detected change. Returns
    ``{'summary': str, 'crop': bytes|None}`` — a typed add/remove summary plus a
    focused crop of a representative changed block (from the after render for an add,
    else the before render). Empty/None-safe."""
    diff = diff_maps(before_map, after_map)
    summary = summarize_diff(diff)
    added, removed = diff.get("added") or [], diff.get("removed") or []
    crop = None
    if added:
        crop = crop_block(after_sections, after_map, _pick(added)) if _pick(added) else None
    if crop is None and removed:
        crop = crop_block(before_sections, before_map, _pick(removed)) if _pick(removed) else None
    return {"summary": summary, "crop": crop}


def diff_maps(before: dict | None, after: dict | None) -> dict:
    """Return ``{'added': [...], 'removed': [...]}`` content blocks, matched by key.

    A block present in ``after`` but not ``before`` is added; vice-versa for removed.
    Keys are digit-masked text hashes, so counts/dates that tick every load don't
    register as changes.
    """
    a_blocks = list((after or {}).get("blocks") or [])
    b_blocks = list((before or {}).get("blocks") or [])
    bkeys = {b.get("k") for b in b_blocks}
    akeys = {b.get("k") for b in a_blocks}
    added = [b for b in a_blocks if b.get("k") not in bkeys]
    removed = [b for b in b_blocks if b.get("k") not in akeys]
    return {"added": added, "removed": removed}


def diff_maps_matched(before: dict | None, after: dict | None,
                      *, match_threshold: float = 0.6) -> dict:
    """Like ``diff_maps`` but also pairs a removed block with an added block that is
    the SAME slot edited in place (high text similarity) into a 'changed' entry.

    Returns ``{'added': [block], 'removed': [block], 'changed': [{'before','after'}]}``.
    Greedy best-match by snippet similarity, so a review/listing whose text mostly
    stayed the same (e.g. a rating ticked 4→5) reads as ONE change, not add+remove.
    """
    d = diff_maps(before, after)
    added = list(d["added"])
    removed = list(d["removed"])
    changed: list[dict] = []
    used: set[int] = set()
    pure_removed: list[dict] = []
    for rb in removed:
        rs = rb.get("s") or ""
        best_i, best_ratio = -1, match_threshold
        for i, ab in enumerate(added):
            if i in used:
                continue
            ratio = difflib.SequenceMatcher(None, rs, ab.get("s") or "").ratio()
            if ratio > best_ratio:
                best_ratio, best_i = ratio, i
        if best_i >= 0:
            used.add(best_i)
            changed.append({"before": rb, "after": added[best_i]})
        else:
            pure_removed.append(rb)
    pure_added = [ab for i, ab in enumerate(added) if i not in used]
    return {"added": pure_added, "removed": pure_removed, "changed": changed}


def word_diff(before_text: str, after_text: str) -> list[dict]:
    """Word-level inline diff → list of ``{'op': 'same'|'add'|'del', 't': text}``
    segments, for rendering a changed block's before→after edit."""
    bw, aw = (before_text or "").split(), (after_text or "").split()
    out: list[dict] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, bw, aw).get_opcodes():
        if op == "equal":
            out.append({"op": "same", "t": " ".join(bw[i1:i2])})
        elif op == "delete":
            out.append({"op": "del", "t": " ".join(bw[i1:i2])})
        elif op == "insert":
            out.append({"op": "add", "t": " ".join(aw[j1:j2])})
        elif op == "replace":
            out.append({"op": "del", "t": " ".join(bw[i1:i2])})
            out.append({"op": "add", "t": " ".join(aw[j1:j2])})
    return [seg for seg in out if seg["t"]]


def _box(b: dict) -> dict:
    return {"x": b.get("x"), "y": b.get("y"), "w": b.get("w"), "h": b.get("h")}


def build_overlays(before_map: dict | None, after_map: dict | None) -> dict | None:
    """Build the interactive 'What changed' overlay payload for one viewport:
    page dims + the changed content blocks categorised (added / removed / changed)
    with their bounding boxes, types, snippets, and — for a changed block — the
    before→after word diff. Added/changed-after boxes draw on the AFTER screenshot;
    removed/changed-before boxes draw on the BEFORE screenshot. None if no maps."""
    if not (before_map and before_map.get("blocks")) and not (after_map and after_map.get("blocks")):
        return None
    m = diff_maps_matched(before_map, after_map)
    after_blocks, before_blocks = [], []
    for b in m["added"]:
        after_blocks.append({**_box(b), "kind": "added", "type": classify_block(b.get("s")),
                             "snippet": b.get("s")})
    for b in m["removed"]:
        before_blocks.append({**_box(b), "kind": "removed", "type": classify_block(b.get("s")),
                              "snippet": b.get("s")})
    for c in m["changed"]:
        bb, ab = c["before"], c["after"]
        wd = word_diff(bb.get("s") or "", ab.get("s") or "")
        after_blocks.append({**_box(ab), "kind": "changed", "type": classify_block(ab.get("s")),
                             "snippet": ab.get("s"), "diff": wd})
        before_blocks.append({**_box(bb), "kind": "changed", "type": classify_block(bb.get("s")),
                              "snippet": bb.get("s"), "diff": wd})
    return {
        "before": {"pw": (before_map or {}).get("pw"), "ph": (before_map or {}).get("ph"),
                   "blocks": before_blocks},
        "after": {"pw": (after_map or {}).get("pw"), "ph": (after_map or {}).get("ph"),
                  "blocks": after_blocks},
        "counts": {"added": len(m["added"]), "removed": len(m["removed"]), "changed": len(m["changed"])},
    }


def crop_block(section_blobs: list | None, element_map: dict | None, block: dict,
               *, pad: int = 16) -> bytes | None:
    """Crop ``block`` out of the snapshot's stored screenshot sections.

    Maps the block's CSS document-coord bbox into the sliced + WebP-scaled section
    image using the map's ``dpr`` and the section's actual stored pixel size, so it
    works for any engine/width (chromium dpr=2 fixed width, Camoufox dpr=1 random
    width, …). Returns WebP bytes, or None if it can't be located.
    """
    from PIL import Image

    from ..storage import blobs

    em = element_map or {}
    dpr = float(em.get("dpr") or 1) or 1.0
    pw = float(em.get("pw") or 0)
    sh = int(settings.screenshot_section_height_px or 8000)
    x, y = float(block.get("x") or 0), float(block.get("y") or 0)
    w, h = float(block.get("w") or 0), float(block.get("h") or 0)
    if not section_blobs or pw <= 0 or w <= 0 or h <= 0 or sh <= 0:
        return None

    sect_i = int(y // sh)
    if sect_i < 0 or sect_i >= len(section_blobs):
        return None
    raw = blobs.get_bytes(section_blobs[sect_i])
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None

    # Each stored section may have been downscaled by _encode_webp; recover the scale
    # from its actual width vs the raw band width (= page CSS width × dpr).
    raw_band_w = pw * dpr
    enc_scale = (im.width / raw_band_w) if raw_band_w else 1.0
    k = dpr * enc_scale

    px0 = max(0, int(x * k) - pad)
    py0 = max(0, int((y - sect_i * sh) * k) - pad)
    px1 = min(im.width, int((x + w) * k) + pad)
    py1 = min(im.height, int((y - sect_i * sh + h) * k) + pad)
    if px1 <= px0 or py1 <= py0:
        return None
    out = io.BytesIO()
    im.crop((px0, py0, px1, py1)).save(out, format="WEBP", quality=82, method=4)
    return out.getvalue()
