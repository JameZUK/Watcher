"""Parse a tracked numeric value (e.g. a price) out of extracted text."""

from __future__ import annotations

import re

# A currency symbol (optional) followed by a number with optional thousands
# separators and decimals. Captures the symbol so we can build a tidy label.
_NUM_RE = re.compile(
    r"(?P<sym>[£$€])?\s*"
    r"(?P<num>\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
)


def parse_number(text: str | None) -> tuple[float, str] | None:
    """Return (value, display_label) parsed from text, or None.

    >>> parse_number("Now £1,263.99 (was £1,499)")
    (1263.99, '£1,263.99')
    """
    if not text:
        return None
    m = _NUM_RE.search(text)
    if not m:
        return None
    raw = m.group("num").replace(",", "").replace(" ", "")
    try:
        value = float(raw)
    except ValueError:
        return None
    sym = m.group("sym") or ""
    label = (sym + m.group("num")).strip()
    return value, label[:64]
