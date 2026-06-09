"""HTML and JSON structured diffing."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup
from bs4 import FeatureNotFound
from deepdiff import DeepDiff

from .text import TextDiff, diff_text


def _soup(html: str) -> BeautifulSoup:
    # Prefer lxml for speed; fall back to the stdlib parser if it's unavailable.
    try:
        return BeautifulSoup(html or "", "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html or "", "html.parser")


def normalize_html(html: str) -> str:
    """Pretty-print HTML so structural diffs are line-oriented and stable."""
    try:
        return _soup(html).prettify()
    except Exception:
        return html or ""


def diff_html(before: str, after: str) -> TextDiff:
    return diff_text(normalize_html(before), normalize_html(after))


def diff_json(before: str, after: str) -> TextDiff:
    try:
        b = json.loads(before or "null")
        a = json.loads(after or "null")
    except Exception:
        # Fall back to a plain text diff if either side is not valid JSON.
        return diff_text(before or "", after or "")

    dd = DeepDiff(b, a, ignore_order=True)
    changed = bool(dd)
    if not changed:
        return TextDiff(False, 0.0, 0, 0, "No JSON change", "")

    pretty = dd.pretty()
    counts = {k: len(v) for k, v in dd.items()}
    n = sum(counts.values())
    summary = ", ".join(f"{k}: {v}" for k, v in counts.items()) or "JSON changed"
    # Magnitude relative to size of the document keys, capped.
    magnitude = min(n / max(_count_nodes(a) + _count_nodes(b), 1), 1.0)
    return TextDiff(True, magnitude, n, n, summary, pretty)


def _count_nodes(obj) -> int:
    if isinstance(obj, dict):
        return 1 + sum(_count_nodes(v) for v in obj.values())
    if isinstance(obj, list):
        return 1 + sum(_count_nodes(v) for v in obj)
    return 1
