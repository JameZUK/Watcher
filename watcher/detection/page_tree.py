"""Build a typed, ordered tree of page regions from a captured ``element_map``.

The element map (``engines/_common.capture_element_map``) is a *flat* list of the
page's repeated content blocks — every record from every repeating-sibling group,
mixed together. The capture step already knows which group each block came from (it
groups children by structural signature to find the ≥3-member record-sets) and now
records that as the block's ``g`` field. This module regroups the flat list back into
a tree of regions purely from that data — no DOM access, no models, no per-site code —
so detection can reason about *records* and *lists* instead of flat text.

This is the structural layer the universal new-item detection, typed value extraction
and scoped triage all stand on:

  PageTree
   └─ Region (one record-set: reviews, jobs, products, model cards, forum posts…)
       └─ Record (one item, with a stable cross-render identity key)

Because the tree is derived from the stored ``element_map``, it works on every
historical snapshot too — no migration, nothing new to persist for the tree itself.
``role`` typing is a deterministic prior here; the per-monitor AI page-profile is what
upgrades it (once, cached) when the DOM is uninformative div-soup.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

# Blocks captured before the ``g`` field existed (or any block the capture couldn't
# attribute to a group) collapse into this single fallback region rather than being
# dropped — old snapshots still yield a usable, if coarse, tree.
_UNGROUPED = "_"

# A record-set sitting at the very top of the page whose items are short is almost
# always chrome (a menu/tab bar), not content. Deliberately conservative: the capture
# step already excludes <25-char items, so anything taller/longer stays 'records'.
_NAV_TOP_Y = 120
_NAV_MAX_MEDIAN_LEN = 40


@dataclass(frozen=True)
class Record:
    """One item within a region (a single review, job, product card, …).

    ``key`` is the digit-masked text hash assigned at capture time — stable across
    renders, so a reordered-but-unchanged item keeps the same key and a genuinely new
    item gets a new one. That makes new-item detection a plain set difference.
    """

    key: str
    text: str
    box: tuple[int, int, int, int]  # (x, y, w, h) in this render's full-page coords


@dataclass
class Region:
    """A record-set: one repeating group of structurally-similar blocks."""

    rid: str                         # stable-ish region signature (the block ``g``)
    role: str                        # 'records' | 'nav'  (deterministic prior)
    box: tuple[int, int, int, int]   # union bbox of the region's records
    records: list[Record]

    @property
    def keys(self) -> set[str]:
        """The region's record identities — the unit of new-item detection."""
        return {r.key for r in self.records}

    @property
    def text_weight(self) -> int:
        """Total characters of record text — a content-richness proxy used to pick
        the page's main list over incidental ones (footers, 'related' rails)."""
        return sum(len(r.text) for r in self.records)


@dataclass
class PageTree:
    pw: int
    ph: int
    regions: list[Region]            # in reading order (top → bottom)

    def region(self, rid: str) -> Region | None:
        return next((r for r in self.regions if r.rid == rid), None)

    def main_region(self) -> Region | None:
        """The single record-set most likely to be the content the user is watching:
        the text-richest 'records' region. A deterministic prior only — the AI
        page-profile overrides it per monitor when the structural guess is wrong."""
        records = [r for r in self.regions if r.role == "records"]
        return max(records, key=lambda r: r.text_weight, default=None)


def _union(blocks: list[dict]) -> tuple[int, int, int, int]:
    xs = [b["x"] for b in blocks]
    ys = [b["y"] for b in blocks]
    x0, y0 = min(xs), min(ys)
    x1 = max(b["x"] + b["w"] for b in blocks)
    y1 = max(b["y"] + b["h"] for b in blocks)
    return (x0, y0, x1 - x0, y1 - y0)


def _classify(blocks: list[dict]) -> str:
    """Deterministic role prior for a region. Conservative by design: everything is a
    'records' set unless it looks like top-of-page chrome (short items, near the top)."""
    top = min(b["y"] for b in blocks)
    med_len = median([len(b.get("s") or "") for b in blocks])
    if top < _NAV_TOP_Y and med_len < _NAV_MAX_MEDIAN_LEN:
        return "nav"
    return "records"


def build_tree(element_map: dict | None) -> PageTree | None:
    """Regroup a flat ``element_map`` into a typed, reading-ordered tree of regions.

    Returns ``None`` when there's nothing to build (no map / no blocks). Blocks without
    a ``g`` field (pre-upgrade snapshots) collapse into one fallback region so older
    data still yields a usable tree.
    """
    if not isinstance(element_map, dict):
        return None
    raw = element_map.get("blocks")
    if not raw:
        return None

    groups: dict[str, list[dict]] = {}
    for b in raw:
        # A block needs a position and an identity key to be a Record; skip malformed
        # entries rather than letting a KeyError sink the whole tree.
        if b.get("k") is None or b.get("x") is None:
            continue
        groups.setdefault(b.get("g") or _UNGROUPED, []).append(b)

    regions: list[Region] = []
    for rid, blocks in groups.items():
        records = [
            Record(key=b["k"], text=b.get("s") or "",
                   box=(b["x"], b["y"], b["w"], b["h"]))
            for b in blocks
        ]
        regions.append(Region(rid=rid, role=_classify(blocks),
                              box=_union(blocks), records=records))

    if not regions:
        return None
    regions.sort(key=lambda r: r.box[1])  # reading order
    return PageTree(pw=element_map.get("pw") or 0,
                    ph=element_map.get("ph") or 0,
                    regions=regions)


def select_region(tree: PageTree | None, rid: str | None = None,
                  sample: str | None = None) -> Region | None:
    """Resolve the monitor's relevant record-set on this render.

    Preference order: the AI-chosen ``rid`` if it's still present → the region whose
    records best match the AI-chosen ``sample`` text (covers a drifted/rehashed
    signature) → the structural ``main_region`` heuristic. This keeps a once-made AI
    choice stable across renders without hard-coding anything per site.
    """
    if tree is None:
        return None
    if rid:
        exact = tree.region(rid)
        if exact is not None:
            return exact
    if sample:
        norm = sample.strip().lower()[:160]
        best, best_hits = None, 0
        for r in tree.regions:
            hits = sum(1 for rec in r.records if norm and norm in rec.text.lower())
            if hits > best_hits:
                best, best_hits = r, hits
        if best is not None:
            return best
    return tree.main_region()


def new_keys(before: PageTree | None, after: PageTree | None,
             region_id: str | None = None) -> set[str]:
    """Record identities present in ``after`` but not ``before`` — the genuinely new
    items, immune to reordering. Compares the named region on each side, or each tree's
    ``main_region`` when ``region_id`` is None. Empty on the first capture (no baseline)
    and whenever the change was pure reordering.
    """
    if after is None:
        return set()
    a = after.region(region_id) if region_id else after.main_region()
    if a is None:
        return set()
    if before is None:
        return set()
    b = before.region(region_id) if region_id else before.main_region()
    if b is None:
        return set()
    return a.keys - b.keys
