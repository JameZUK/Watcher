"""Text normalization and ignore-pattern handling to suppress noisy diffs."""

from __future__ import annotations

import re

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
        try:
            out = re.sub(pat, "", out)
        except re.error:
            # Treat an invalid regex as a literal substring.
            out = out.replace(pat, "")

    if numbers:
        out = _NUMBER.sub("#", out)

    if whitespace:
        lines = [_WS.sub(" ", ln).strip() for ln in out.splitlines()]
        out = "\n".join(lines)
        out = _BLANKLINES.sub("\n\n", out).strip()

    return out
