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
    T.ai_login_action, T.solve_captcha_grid, T.profile_page, T.refine_intent,
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
        '"value_threshold_dir":"none","selectors":["#accept-all","#accept-all","button.agree","<bad>"],'
        '"action":"type","index":0,"secret":"username","text":"",'
        '"page_summary":"P","relevant":"R","noise":"N",'
        '"refined":"Alert only on analyst-role reviews","note":"Resolved the contradiction",'
        '"cells":[1,3,9,99]}'
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


def test_profile_page_assembles_understanding(fake_http):
    """profile_page returns a plain-language understanding (summary + what matters +
    what's churn) assembled from the model's structured fields."""
    out = _run(T.profile_page(api_key="k", model="m", base_url="http://x/v1",
                              url="u", title="t", intent="only reviews",
                              page_text="lots of page text here " * 20))
    assert out is not None
    assert "P" in out and "Changes that matter: R" in out
    assert "churn" in out.lower() and "N" in out
    assert fake_http.last["url"] == "http://x/v1"


def test_refine_intent_returns_clear_rewrite(fake_http):
    """refine_intent rewrites a rough draft into a clear instruction (+ a note), and
    forwards the draft and page context to the model."""
    out = _run(T.refine_intent(api_key="k", model="m", base_url="http://x/v1",
                               url="u", title="t", draft="only analyst reviews but all new reviews",
                               page_profile="reviews have a role field"))
    assert out == {"refined": "Alert only on analyst-role reviews", "note": "Resolved the contradiction"}
    sent = fake_http.last["json"]["messages"][1]["content"]
    assert "only analyst reviews but all new reviews" in sent and "role field" in sent


def test_refine_intent_skips_without_draft():
    """No draft → nothing to refine."""
    assert _run(T.refine_intent(api_key="k", model="m", url="u", title="t", draft="  ")) is None


def test_profile_page_skips_without_text():
    """No captured text → no profile (don't burn an AI call on an empty/blocked page)."""
    assert _run(T.profile_page(api_key="k", model="m", url="u", title="t",
                               intent="x", page_text="")) is None


def test_triage_includes_page_profile(fake_http):
    """A page profile, when present, is forwarded to the model so it can judge scope."""
    _run(T.triage_change(api_key="k", model="m", url="u", title="t", intent="only reviews",
                         diff_text="- a\n+ b",
                         page_profile="This page: reviews. Churn: a rotating jobs widget."))
    user = fake_http.last["json"]["messages"][1]["content"]
    user_text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user)
    assert "rotating jobs widget" in user_text


def test_triage_includes_churn_hint(fake_http):
    """Observed churn lines are forwarded to the model as a 'likely incidental' hint."""
    _run(T.triage_change(api_key="k", model="m", url="u", title="t", intent="only reviews",
                         diff_text="- a\n+ b", churn_hint=["Jobs: 37", "Followers 1,234"]))
    user = fake_http.last["json"]["messages"][1]["content"]
    user_text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user)
    assert "Jobs: 37" in user_text and "change on most recent checks" in user_text


def test_profile_includes_churn_samples(fake_http):
    """Observed churn samples are forwarded to the profiler so the page understanding
    is grounded in real behaviour, not just inference."""
    _run(T.profile_page(api_key="k", model="m", url="u", title="t", intent="reviews",
                        page_text="text " * 100, churn_samples=["Jobs: 37"]))
    user_text = fake_http.last["json"]["messages"][1]["content"]
    assert "Jobs: 37" in user_text and "change repeatedly" in user_text


def test_visual_only_triage_forbids_content_claims(fake_http):
    """A visual-only change (no text diff) means the page TEXT is unchanged, so the
    prompt must forbid claiming new textual content. Regression: Glassdoor's rotating
    images triaged as 'New analyst reviews are visible'."""
    from io import BytesIO

    from PIL import Image
    buf = BytesIO(); Image.new("RGB", (12, 12), "white").save(buf, format="PNG")
    _run(T.triage_change(api_key="k", model="m", url="u", title="t", intent="only reviews",
                         diff_text=None, image_png=buf.getvalue()))
    content = fake_http.last["json"]["messages"][1]["content"]
    text = " ".join(p.get("text", "") for p in content) if isinstance(content, list) else content
    assert "VISUAL-ONLY" in text
    assert "no new reviews" in text.lower()        # explicitly rules out content claims


def test_triage_user_content_includes_site_domain(fake_http):
    """Triage is told the page's domain so the headline can name the real site."""
    _run(T.triage_change(api_key="k", model="m", url="https://www.amazon.co.uk/dp/X",
                         title="Some product", intent=None, diff_text="- a\n+ b"))
    user = fake_http.last["json"]["messages"][1]["content"]
    text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user)
    assert "amazon.co.uk" in text


def test_fleet_summary_includes_site_domain_and_title(fake_http):
    """The fleet summary is given each change's site domain AND page title so it can
    name the real site and understand the page (not just the saved label)."""
    _run(T.summarize_fleet(api_key="k", model="m", changes=[
        {"monitor_id": 3, "monitor": "JBL", "domain": "uk.jbl.com",
         "title": "JBL Boombox 3 Wi-Fi | JBL UK", "importance": "high", "headline": "Price drop"}]))
    msgs = " ".join(m["content"] for m in fake_http.last["json"]["messages"]
                    if isinstance(m["content"], str))
    assert "uk.jbl.com" in msgs and "JBL Boombox 3 Wi-Fi | JBL UK" in msgs


def test_triage_prompt_enforces_scope_and_grounding(fake_http):
    """The triage prompt must keep its grounding + scope guardrails, and forward the
    user's watch intent, so out-of-scope churn (e.g. a rotating jobs widget) can't be
    mis-reported as the watched thing. Regression: Glassdoor 'new analyst reviews'."""
    _run(T.triage_change(api_key="k", model="m", url="u", title="t",
                         intent="only reviews, ignore job listings",
                         diff_text="- old\n+ new"))
    msgs = fake_http.last["json"]["messages"]
    system = msgs[0]["content"].lower()
    assert "ground every word" in system        # don't claim what the diff doesn't show
    assert "scope" in system and "noise" in system   # out-of-scope → noise
    assert "keyword" in system                  # anti-conflation rule
    # don't parrot the instruction's qualifier (e.g. label a non-analyst review "analyst")
    assert "does not describe what changed" in system and "parroting" in system
    # Explicitly-excluded kinds (a non-analyst review, a job listing) must be DROPPED as
    # 'noise', never softened to 'low'/'high', and a general-theme-only match isn't high.
    # Regression: 2026-06-13 Glassdoor false HIGH on a support-engineer review (chg 166)
    # and false low on a job listing (chg 167).
    assert "explicit exclusions are absolute" in system
    assert "out of scope" in system
    assert "not merely the general topic" in system
    # Item-type grounding: a jobs-widget rotation must not be called "reviews".
    # Regression: 2026-06-13 chg 180 (Glassdoor jobs widget mislabeled "New reviews").
    assert "identify each item by its structure" in system
    assert "jobs widget" in system or "jobs-widget" in system
    assert "view job" in system
    user = msgs[1]["content"]
    user_text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user)
    assert "only reviews, ignore job listings" in user_text


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


def test_ai_login_action_parses(fake_http):
    r = _run(T.ai_login_action(api_key="k", model="m", base_url="http://x/v1",
             elements=[{"idx": 0, "tag": "input", "type": "email"}], screenshot_png=None,
             available=["username", "password"], history=[]))
    assert r and r["action"] == "type" and r["index"] == 0 and r["secret"] == "username"
    assert fake_http.last["url"] == "http://x/v1"


def _tiny_png() -> bytes:
    import io as _io

    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (12, 12), (40, 40, 40)).save(buf, format="PNG")
    return buf.getvalue()


def test_solve_captcha_grid_parses_and_bounds(fake_http):
    r = _run(T.solve_captcha_grid(api_key="k", model="m", base_url="http://x/v1",
             target="motorcycles", rows=3, cols=3, image_png=_tiny_png()))
    # cell 99 is out of a 3x3 (9-cell) grid → dropped; valid cells kept
    assert r == [1, 3, 9]
    assert fake_http.last["url"] == "http://x/v1"


def test_solve_captcha_grid_needs_image(fake_http):
    assert _run(T.solve_captcha_grid(api_key="k", model="m", target="x",
                                     rows=3, cols=3, image_png=b"")) is None


def test_default_endpoint_is_openrouter(fake_http):
    _run(T.triage_change(api_key="k", model="m", url="u", title="t",
                         intent=None, diff_text="d"))
    assert fake_http.last["url"] == T.OPENROUTER_URL


def test_missing_key_returns_none(fake_http):
    assert _run(T.triage_change(api_key="", model="m", url="u", title="t",
                                intent=None, diff_text="d")) is None
