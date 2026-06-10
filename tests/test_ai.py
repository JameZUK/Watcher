"""Tests for the AI helper functions (signatures + base_url threading).

These would have caught the regression where a replace-all only updated the
multi-line function signatures, leaving extract_value/configure_monitor/
summarize_history without the base_url parameter their bodies referenced.
"""

import asyncio
import inspect

import pytest

from watcher.ai import triage as T

AI_FNS = [
    T.triage_change, T.suggest_watch_items, T.extract_value,
    T.configure_monitor, T.summarize_history, T.suggest_consent_selectors,
]


def test_all_ai_functions_accept_core_kwargs():
    for f in AI_FNS:
        params = inspect.signature(f).parameters
        for required in ("api_key", "model", "base_url"):
            assert required in params, f"{f.__name__} is missing '{required}'"


# --- mocked HTTP so we can exercise the real call/parse paths offline -------

class _FakeResp:
    status_code = 200
    text = ""

    def __init__(self, content):
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    """Records the last request and returns a canned, multi-schema JSON blob."""
    last: dict = {}
    _content = (
        '{"headline":"H","category":"price","importance":"high","detail":"d",'
        '"found":true,"value":1.5,"label":"£1.50","suggestions":["a","b"],'
        '"name":"N","detection_mode":"auto","selector":"","interval_minutes":30,'
        '"ai_watch_intent":"x","track_value":true,"value_threshold":0,'
        '"value_threshold_dir":"none","selectors":["#accept-all","#accept-all","button.agree","<bad>"]}'
    )

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.last = {"url": url, "json": json, "headers": headers}
        return _FakeResp(self._content)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.last = {}
    monkeypatch.setattr(T.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _run(coro):
    return asyncio.run(coro)


def test_triage_change_threads_base_url(fake_http):
    r = _run(T.triage_change(api_key="k", model="m", base_url="http://x/v1",
                             url="u", title="t", intent=None, diff_text="- a\n+ b"))
    assert r is not None and r.headline == "H" and r.category == "price"
    assert fake_http.last["url"] == "http://x/v1"
    assert fake_http.last["json"]["model"] == "m"


def test_extract_value_threads_base_url(fake_http):
    r = _run(T.extract_value(api_key="k", model="m", base_url="http://x/v1",
                             url="u", title="t", page_text="Price £1.50"))
    assert r == (1.5, "£1.50")
    assert fake_http.last["url"] == "http://x/v1"


def test_configure_monitor_threads_base_url(fake_http):
    r = _run(T.configure_monitor(api_key="k", model="m", base_url="http://x/v1",
                                 url="u", title="t", page_text="p", goal="g"))
    assert r and r["detection_mode"] == "auto"
    assert fake_http.last["url"] == "http://x/v1"


def test_suggest_threads_base_url(fake_http):
    r = _run(T.suggest_watch_items(api_key="k", model="m", base_url="http://x/v1",
                                   url="u", title="t", page_text="p"))
    assert r == ["a", "b"]
    assert fake_http.last["url"] == "http://x/v1"


def test_summarize_threads_base_url(fake_http):
    r = _run(T.summarize_history(api_key="k", model="m", base_url="http://x/v1",
                                 name="n", lines=["x"]))
    assert r
    assert fake_http.last["url"] == "http://x/v1"


def test_suggest_consent_selectors_parses_and_sanitizes(fake_http):
    r = _run(T.suggest_consent_selectors(
        api_key="k", model="m", base_url="http://x/v1", url="u",
        html_snippets=["<div id='cmp'>We use cookies</div>"]))
    # de-duped (case-insensitive) and the junk "<bad>" selector dropped
    assert r == ["#accept-all", "button.agree"]
    assert fake_http.last["url"] == "http://x/v1"


def test_suggest_consent_selectors_no_html_returns_none(fake_http):
    assert _run(T.suggest_consent_selectors(
        api_key="k", model="m", url="u", html_snippets=[])) is None
    assert _run(T.suggest_consent_selectors(
        api_key="k", model="m", url="u", html_snippets=["   "])) is None


def test_default_endpoint_is_openrouter(fake_http):
    _run(T.triage_change(api_key="k", model="m", url="u", title="t",
                         intent=None, diff_text="d"))
    assert fake_http.last["url"] == T.OPENROUTER_URL


def test_missing_key_returns_none(fake_http):
    assert _run(T.triage_change(api_key="", model="m", url="u", title="t",
                                intent=None, diff_text="d")) is None
