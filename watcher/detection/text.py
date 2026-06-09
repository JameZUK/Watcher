"""Text / HTML diffing: unified diff, change magnitude, human summary."""

from __future__ import annotations

import difflib
from dataclasses import dataclass


@dataclass
class TextDiff:
    changed: bool
    magnitude: float          # fraction of lines changed (0..1)
    added: int
    removed: int
    summary: str
    unified: str              # unified diff text


def diff_text(before: str, after: str, *, context: int = 3) -> TextDiff:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()

    sm = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1

    total = max(len(before_lines), len(after_lines), 1)
    magnitude = (added + removed) / (2 * total)
    magnitude = min(magnitude, 1.0)

    unified = "\n".join(
        difflib.unified_diff(
            before_lines, after_lines, fromfile="previous", tofile="current", lineterm="", n=context
        )
    )

    changed = added > 0 or removed > 0
    summary = (
        f"{added} line(s) added, {removed} removed"
        if changed
        else "No textual change"
    )
    return TextDiff(changed, magnitude, added, removed, summary, unified)
