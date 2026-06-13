"""Structural churn learning (no AI, no per-site rules).

Tracks which *lines* of a page change on nearly every check, so we can tell the AI
"these keep flipping — almost certainly incidental churn" as a grounded, observed
signal. We deliberately do NOT hard-suppress on this alone: a value the user is
watching (a price that ticks every check) also churns, and only the intent-aware AI
can tell the difference. So churn is an input to triage/profiling, never a silent veto.

State lives on the monitor as a small dict ``{hash: [count, sample_text]}`` updated
with a leaky bucket: a line seen in this check's diff gains a point (capped); lines
not seen lose one and are dropped at zero. A line that recurs across many checks rises
above the threshold; a one-off decays away.
"""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")

_COUNT_CAP = 12          # max bucket level (bounds how "sticky" a churn line is)
_MAX_TRACKED = 200       # bound the per-monitor state size
_CHURN_THRESHOLD = 3     # a line is "churn" once it has recurred this many net checks


def _norm(line: str) -> str:
    return _WS.sub(" ", line.strip())


def changed_lines(diff_text: str | None) -> list[tuple[str, str]]:
    """The (hash, normalized-text) of each added/removed content line in a unified
    diff (skipping the +++/--- headers and trivially short lines), de-duplicated."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ln in (diff_text or "").splitlines():
        if ln[:1] not in "+-" or ln[:3] in ("+++", "---"):
            continue
        text = _norm(ln[1:])
        if len(text) < 3:
            continue
        # Hash with digits masked, so an incrementing counter / timestamp / view-count
        # ("37 jobs" → "38 jobs", "9 Jun" → "10 Jun") is recognised as the same churning
        # line each check. The sample keeps the actual last-seen text.
        key = _DIGITS.sub("#", text)
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        if h in seen:
            continue
        seen.add(h)
        out.append((h, text))
    return out


def update(churn: dict | None, lines: list[tuple[str, str]]) -> dict:
    """Leaky-bucket update against this check's changed lines. Returns a new dict
    ``{hash: [count, sample_text]}`` (the caller assigns it back to the monitor)."""
    out: dict = {h: [int(v[0]), str(v[1])] for h, v in (churn or {}).items()}
    changed = {h: t for h, t in lines}
    for h, t in changed.items():
        if h in out:
            out[h][0] = min(_COUNT_CAP, out[h][0] + 1)
            out[h][1] = t
        else:
            out[h] = [1, t]
    for h in list(out):
        if h not in changed:
            out[h][0] -= 1
            if out[h][0] <= 0:
                del out[h]
    if len(out) > _MAX_TRACKED:               # keep the most-recurring
        for h in sorted(out, key=lambda k: out[k][0])[: len(out) - _MAX_TRACKED]:
            del out[h]
    return out


def churny_texts(churn: dict | None, *, threshold: int = _CHURN_THRESHOLD,
                 limit: int = 25) -> list[str]:
    """Sample texts of the lines that recur on most checks (the observed churn),
    most-frequent first — fed to triage/profiling as a hint."""
    items = sorted((churn or {}).items(), key=lambda kv: kv[1][0], reverse=True)
    return [v[1] for _h, v in items if v[0] >= threshold][:limit]
