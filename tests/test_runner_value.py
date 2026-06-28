"""Typed value extraction: scope the AI to the pinned list region (runner._extract_value)."""

import asyncio
import types

from watcher import runner as R


def _blk(k, s, y):
    return {"k": k, "g": "grid>div.product", "x": 0, "y": y, "w": 300, "h": 80, "s": s}


def _result(rendered_text, blocks=None):
    em = {"pw": 1200, "ph": 4000, "blocks": blocks} if blocks else None
    return types.SimpleNamespace(extracted_value=None, rendered_text=rendered_text,
                                 element_map=em, title="t")


def _setup(monkeypatch):
    """Capture the page_text handed to the AI extractor."""
    seen = {}

    async def _fake_extract(*, page_text, **kw):
        seen["page_text"] = page_text
        return (1.0, "£1.00")

    monkeypatch.setattr(R, "extract_value", _fake_extract)
    monkeypatch.setattr(R, "get_openrouter_key", lambda app: "key")
    app = types.SimpleNamespace(ai_enabled=True, ai_model="m", ai_base_url=None)
    return seen, app


def _monitor(**kw):
    base = dict(ai_enabled=True, url="https://e.test", ai_list_rid=None, ai_list_sample=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_scopes_to_pinned_region(monkeypatch):
    seen, app = _setup(monkeypatch)
    blocks = [_blk("a", "Acme Widget £12.00", 100),
              _blk("b", "Other Widget £9.50", 200),
              _blk("c", "Third Widget £20.00", 300)]
    m = _monitor(ai_list_rid="grid>div.product")
    res = _result("WHOLE PAGE has market cap $9,000,000,000 and other noise", blocks)
    asyncio.run(R._extract_value(app, m, res))
    assert "Acme Widget" in seen["page_text"]
    assert "market cap" not in seen["page_text"]      # full page text was NOT used


def test_unpinned_monitor_uses_full_text(monkeypatch):
    seen, app = _setup(monkeypatch)
    blocks = [_blk("a", "Acme Widget £12.00", 100), _blk("b", "x £9.50", 200),
              _blk("c", "y £20.00", 300)]
    m = _monitor(ai_list_rid=None)                    # no region pinned
    res = _result("WHOLE PAGE price is £42.00", blocks)
    asyncio.run(R._extract_value(app, m, res))
    assert seen["page_text"] == "WHOLE PAGE price is £42.00"


def test_selector_value_still_wins_for_free(monkeypatch):
    seen, app = _setup(monkeypatch)
    m = _monitor(ai_list_rid="grid>div.product")
    res = _result("ignored", None)
    res.extracted_value = "£263.99"
    out = asyncio.run(R._extract_value(app, m, res))
    assert out == (263.99, "£263.99")
    assert "page_text" not in seen                    # AI never called
