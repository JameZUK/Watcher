"""Common rendering contract shared by all engines.

Every engine produces a RenderResult with the same captured artifacts so the
detection layer is engine-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Monitor

# JS that extracts human-visible text, skipping script/style/hidden nodes.
VISIBLE_TEXT_JS = r"""
() => {
  function visible(el) {
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
    return true;
  }
  const skip = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG']);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const p = node.parentElement;
      if (!p || skip.has(p.tagName)) return NodeFilter.FILTER_REJECT;
      if (!visible(p)) return NodeFilter.FILTER_REJECT;
      const t = node.textContent.trim();
      return t ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    }
  });
  const parts = [];
  let n;
  while ((n = walker.nextNode())) parts.push(n.textContent.trim());
  return parts.join('\n');
}
"""


@dataclass
class RenderResult:
    ok: bool = True
    http_status: int | None = None
    error: str | None = None
    title: str | None = None
    html: str | None = None
    rendered_text: str | None = None
    extracted_value: str | None = None
    screenshot_png: bytes | None = None          # desktop viewport
    screenshot_mobile_png: bytes | None = None   # mobile viewport
    content_type: str | None = None
    render_ms: int | None = None
    # Updated browser storage_state if a login flow ran (for session persistence).
    session_state: dict | None = None
    # HTML of any consent/cookie banner the automatic handler could NOT clear —
    # fed to the optional AI fallback so it can learn dismiss selectors.
    unhandled_consent_html: list | None = None
    extra: dict = field(default_factory=dict)


class Renderer(Protocol):
    async def render(self, monitor: Monitor) -> RenderResult: ...
