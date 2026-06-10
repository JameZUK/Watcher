"""Text normalization and ignore-pattern handling to suppress noisy diffs."""

from __future__ import annotations

import re

try:  # the `regex` module supports a per-call timeout — defuses ReDoS
    import regex as _rx
    _RX_TIMEOUT = 2.0
except ImportError:  # graceful fallback (no interruptibility)
    _rx = None
    _RX_TIMEOUT = None


def _apply_ignore(pattern: str, text: str) -> str:
    """Apply a user ignore-regex with a time budget; on an invalid or
    pathological pattern, fall back to literal substring removal (no backtrack)."""
    try:
        if _rx is not None:
            return _rx.sub(pattern, "", text, timeout=_RX_TIMEOUT)
        return re.sub(pattern, "", text)
    except Exception:  # noqa: BLE001 — invalid regex or timeout
        return text.replace(pattern, "")


_WS = re.compile(r"[ \t\f\v]+")
_BLANKLINES = re.compile(r"\n{3,}")
# Loosely matches numbers, including currency/decimals/thousands separators.
_NUMBER = re.compile(r"\d[\d,.\s]*\d|\d")


def normalize_text(
    text: str,
    *,
    whitespace: bool = True,
    numbers: bool = False,
    ignore_patterns: list[str] | None = None,
) -> str:
    if text is None:
        return ""
    out = text

    for pat in ignore_patterns or []:
        out = _apply_ignore(pat, out)

    if numbers:
        out = _NUMBER.sub("#", out)

    if whitespace:
        lines = [_WS.sub(" ", ln).strip() for ln in out.splitlines()]
        out = "\n".join(lines)
        out = _BLANKLINES.sub("\n\n", out).strip()

    return out
