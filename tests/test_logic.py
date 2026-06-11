"""Unit tests for the pure logic helpers across the new features."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as N

import pytest


# --- value parsing ---------------------------------------------------------

def test_parse_number():
    from watcher.detection.value import parse_number
    assert parse_number("Now £1,263.99 (was £1,499)") == (1263.99, "£1,263.99")
    assert parse_number("In stock: 4 left")[0] == 4.0
    assert parse_number("4.5 stars")[0] == 4.5
    assert parse_number("no number here") is None
    assert parse_number(None) is None


# --- Cookie Editor JSON conversion -----------------------------------------

def test_cookie_editor_conversion():
    from watcher.auth.login_flows import cookie_editor_to_storage_state
    ss = cookie_editor_to_storage_state(
        '[{"name":"datadome","value":"v","domain":".a.com","secure":true,'
        '"sameSite":"no_restriction","expirationDate":111.5}]'
    )
    c = ss["cookies"][0]
    assert c["name"] == "datadome" and c["sameSite"] == "None" and c["expires"] == 111.5
    # accepts a full storage_state too
    assert cookie_editor_to_storage_state('{"cookies":[{"name":"x","value":"1","domain":"a"}],"origins":[]}')


def test_cookie_editor_errors():
    from watcher.auth.login_flows import cookie_editor_to_storage_state
    assert cookie_editor_to_storage_state("") is None
    with pytest.raises(ValueError):
        cookie_editor_to_storage_state("{not json")
    with pytest.raises(ValueError):
        cookie_editor_to_storage_state("[]")


def test_cookie_editor_cross_domain_scope():
    """Host-scoping keeps only the monitor's domain (Glassdoor); the all-domains
    path (allowed_host=None) also keeps federated auth cookies (Indeed)."""
    from watcher.auth.login_flows import cookie_editor_to_storage_state
    raw = ('[{"name":"gdId","value":"g","domain":".glassdoor.co.uk","path":"/"},'
           '{"name":"PPID","value":"i","domain":".indeed.com","path":"/"}]')
    scoped = cookie_editor_to_storage_state(raw, allowed_host="www.glassdoor.co.uk")
    assert [c["name"] for c in scoped["cookies"]] == ["gdId"]          # indeed dropped
    alld = cookie_editor_to_storage_state(raw, allowed_host=None)
    assert {c["name"] for c in alld["cookies"]} == {"gdId", "PPID"}    # both kept


def test_cookie_editor_multiple_blocks():
    """Several JSON exports pasted in one box (any separator) all parse."""
    from watcher.auth.login_flows import cookie_editor_to_storage_state
    two = ('[{"name":"gdId","value":"g","domain":".glassdoor.co.uk"}]\n'
           '[{"name":"PPID","value":"i","domain":".indeed.com"}]')
    ss = cookie_editor_to_storage_state(two, allowed_host=None)
    assert {c["name"] for c in ss["cookies"]} == {"gdId", "PPID"}
    # comma-separated and a bare single object also work
    assert len(cookie_editor_to_storage_state(
        '[{"name":"a","value":"1","domain":".x.com"}],'
        '{"name":"b","value":"2","domain":".x.com"}', allowed_host=None)["cookies"]) == 2


def test_merge_storage_state():
    from watcher.auth.login_flows import merge_storage_state
    existing = {"cookies": [{"name": "gdId", "value": "old", "domain": ".glassdoor.co.uk", "path": "/"},
                            {"name": "keep", "value": "k", "domain": ".glassdoor.co.uk", "path": "/"}], "origins": []}
    new = {"cookies": [{"name": "gdId", "value": "new", "domain": ".glassdoor.co.uk", "path": "/"},
                       {"name": "PPID", "value": "i", "domain": ".indeed.com", "path": "/"}], "origins": []}
    merged = merge_storage_state(existing, new)
    by = {c["name"]: c["value"] for c in merged["cookies"]}
    assert by == {"gdId": "new", "keep": "k", "PPID": "i"}     # added + updated, none lost
    assert merge_storage_state(None, None) is None


def test_cookie_domain_scoping():
    from watcher.auth.login_flows import cookie_editor_to_storage_state as conv
    raw = ('[{"name":"a","value":"1","domain":".jbl.com"},'
           '{"name":"b","value":"1","domain":"uk.jbl.com"},'
           '{"name":"c","value":"1","domain":".evil.com"}]')
    kept = sorted(c["name"] for c in conv(raw, allowed_host="uk.jbl.com")["cookies"])
    assert kept == ["a", "b"]                       # parent + exact host kept, unrelated dropped
    # no host → unrestricted (back-compat)
    assert len(conv(raw)["cookies"]) == 3


def test_session_is_valid_tz_safe():
    """SQLite returns naive datetimes; comparison must not crash."""
    from watcher.auth.login_flows import session_is_valid
    state = {"cookies": [{"name": "x"}]}
    # naive UTC datetimes, mimicking what SQLite hands back (no tzinfo)
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    future = N(session_state=state, session_valid_until=now_naive + timedelta(hours=1))
    past = N(session_state=state, session_valid_until=now_naive - timedelta(hours=1))
    none_until = N(session_state=state, session_valid_until=None)
    assert session_is_valid(future) is True
    assert session_is_valid(past) is False
    assert session_is_valid(none_until) is True
    assert session_is_valid(None) is False
    assert session_is_valid(N(session_state=None, session_valid_until=None)) is False


# --- value threshold + adaptive interval -----------------------------------

def test_threshold_crossing():
    from watcher.runner import _threshold_crossing
    m = N(value_threshold=300.0, value_threshold_dir="below")
    assert _threshold_crossing(m, 320.0, 280.0, "£280") is not None   # crossed below
    assert _threshold_crossing(m, 280.0, 270.0, "£270") is None       # already below
    assert _threshold_crossing(m, None, 280.0, "£280") is not None    # first reading, below
    above = N(value_threshold=300.0, value_threshold_dir="above")
    assert _threshold_crossing(above, 280.0, 320.0, "£320") is not None
    off = N(value_threshold=None, value_threshold_dir=None)
    assert _threshold_crossing(off, 1.0, 2.0, "x") is None


def test_adapt_interval():
    from watcher.runner import _adapt_interval
    fast = N(id=1, interval_seconds=3600, adaptive_interval=True)
    _adapt_interval(fast, changed=True)
    assert fast.interval_seconds == 1800           # halved when changing
    slow = N(id=1, interval_seconds=3600, adaptive_interval=True)
    _adapt_interval(slow, changed=False)
    assert slow.interval_seconds > 3600            # backs off when stable


# --- quiet hours -----------------------------------------------------------

def test_quiet_hours():
    from watcher.notify import _in_quiet_hours
    h = datetime.now(timezone.utc).hour
    inside = N(quiet_start=(h - 1) % 24, quiet_end=(h + 2) % 24)
    outside = N(quiet_start=(h + 2) % 24, quiet_end=(h + 4) % 24)
    assert _in_quiet_hours(inside) is True
    assert _in_quiet_hours(outside) is False
    assert _in_quiet_hours(N(quiet_start=None, quiet_end=None)) is False
    assert _in_quiet_hours(None) is False


# --- HTML→text for AI suggestions ------------------------------------------

def test_netsec_url_guards():
    from watcher.netsec import validate_monitor_url, validate_public_url
    # scheme guard (no DNS needed)
    assert validate_monitor_url("file:///etc/passwd")
    assert validate_monitor_url("javascript:alert(1)")
    assert validate_monitor_url("data:text/html,x")
    assert validate_monitor_url("https://example.com/p") is None
    # SSRF guard against literal internal IPs (resolve without network)
    assert validate_public_url("http://127.0.0.1/")
    assert validate_public_url("http://169.254.169.254/latest/meta-data/")
    assert validate_public_url("http://[::1]/")
    assert validate_public_url("http://10.0.0.5/")


def test_ignore_pattern_redos_is_bounded():
    import time
    from watcher.detection.noise import normalize_text
    t = time.time()
    # A catastrophic-backtracking pattern must not hang (bounded by the regex timeout).
    normalize_text("a" * 80 + "!", ignore_patterns=[r"(a+)+$"])
    assert time.time() - t < 5.0
    # invalid regex still falls back to literal removal (no crash)
    assert "(this" not in normalize_text("keep (this stuff", ignore_patterns=["(this"])


def test_safe_patterns():
    from watcher.web.routes.monitors import _safe_patterns
    assert _safe_patterns(["(a+", "valid.*", "x" * 300, "\\d{2}:\\d{2}"]) == ["valid.*", "\\d{2}:\\d{2}"]
    assert _safe_patterns(["p"] * 100) == ["p"] * 25   # count capped


def test_json_mode_diffs_normalized_json():
    from types import SimpleNamespace as N
    from watcher.detection.detector import detect
    from watcher.models import DetectionMode
    mon = N(detection_mode=DetectionMode.json, min_change_threshold=0.0)
    prev = N(rendered_text='{\n  "a": 1\n}')
    same = N(rendered_text='{\n  "a": 1\n}', html="<pre>{}</pre>")
    diff = N(rendered_text='{\n  "a": 2\n}', html="")
    assert not detect(mon, prev, same).changed   # identical normalized JSON → no spurious change
    assert detect(mon, prev, diff).changed


def test_json_diff_recursion_safe():
    """A deeply-nested (malicious) JSON page must not raise out of detection."""
    from watcher.detection.structured import diff_json
    deep = "[" * 3000 + "1" + "]" * 3000
    r = diff_json("[]", deep)          # must fall back, not RecursionError
    assert r is not None
    assert diff_json(deep, deep) is not None


def test_combine_intent():
    """Group + monitor watch-intents combine non-destructively (neither clobbers)."""
    from watcher.runner import _combine_intent
    assert _combine_intent("back in stock", "price below 250") == \
        "Group goal: back in stock  This page specifically: price below 250"
    assert _combine_intent("back in stock", None) == "Group goal: back in stock"
    assert _combine_intent(None, "price below 250") == "This page specifically: price below 250"
    assert _combine_intent(None, None) is None
    assert _combine_intent("  ", "") is None


def test_otp_verify():
    import pyotp
    from watcher.auth import otp
    s = otp.new_secret()
    assert otp.verify(s, pyotp.TOTP(s).now()) is True
    assert otp.verify(s, "000000") is False
    assert otp.verify(s, None) is False
    assert otp.verify(None, "123456") is False
    assert otp.verify(s, "12345") is False        # wrong length
    assert otp.verify(s, "12 34 56") is False      # stripped to 6 digits but wrong


def test_netsec_blocks_internal_targets_and_encodings():
    """SSRF: validate_public_url / validate_proxy must reject internal targets,
    including IPv4-mapped / NAT64 / CGNAT / 0.0.0.0 encodings (offline, literals)."""
    from watcher.netsec import validate_proxy, validate_public_url
    for u in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/",
              "http://[::1]/", "http://10.0.0.5/admin", "http://0.0.0.0/",
              "http://[::ffff:127.0.0.1]/", "http://100.64.0.1/"):
        assert validate_public_url(u), u            # all blocked (truthy error)
    # proxy host gets the same IP policy + scheme allowlist
    assert validate_proxy("http://127.0.0.1:8080")
    assert validate_proxy("socks5h://10.0.0.1:1080")
    assert validate_proxy("ftp://1.1.1.1:1080")     # bad scheme
    assert validate_proxy("") is None               # empty allowed
    assert validate_proxy(None) is None


def test_render_gate_is_transient_tolerant():
    """The render-time SSRF gate blocks resolved-internal/bad-scheme targets but
    NOT a transient resolution failure (so a DNS blip can't auto-pause a monitor)."""
    from watcher.netsec import proxy_block_reason, render_block_reason
    assert render_block_reason("http://169.254.169.254/")          # resolved-internal
    assert render_block_reason("file:///etc/passwd")               # bad scheme
    assert render_block_reason("http://nonexistent.invalid.zzz/") is None   # transient
    assert proxy_block_reason("socks5h://10.0.0.1:1080")           # internal proxy
    assert proxy_block_reason("http://nonexistent.invalid.zzz:8080") is None
    assert proxy_block_reason("") is None


def test_csrf_host_parsing_ipv6_and_port():
    from watcher.main import _host_only
    assert _host_only("http://[::1]:8000") == "::1"     # IPv6 literal + port
    assert _host_only("//[::1]:8000") == "::1"          # Host-header form
    assert _host_only("//example.com:8000") == "example.com"
    assert _host_only("https://EXAMPLE.com") == "example.com"


def test_api_token_hashing():
    from watcher.auth.security import hash_token, looks_hashed, new_api_token
    raw = new_api_token()
    h = hash_token(raw)
    assert len(h) == 64 and looks_hashed(h)
    assert not looks_hashed(raw)                     # cleartext isn't a hash
    assert hash_token(raw) == h                      # deterministic lookup


def test_rate_limiter_window():
    from watcher.web.ratelimit import allow, reset
    reset("unit")
    assert all(allow("unit", limit=3, window=60) for _ in range(3))
    assert not allow("unit", limit=3, window=60)     # 4th over the limit
    reset("unit")
    assert allow("unit", limit=3, window=60)         # reset clears it


def test_visual_noise_floor(monkeypatch):
    """Sub-floor pixel churn (anti-aliasing, lazy images, carousels) must NOT
    register as a change — the root cause of the JBL phantom-change bug."""
    from types import SimpleNamespace as N
    from watcher.detection import detector, visual
    from watcher.models import DetectionMode

    mag = [0.002]   # 0.2% of pixels — below the 0.5% default floor
    monkeypatch.setattr(detector.blobs, "get_bytes", lambda h: b"before")
    monkeypatch.setattr(detector.visual, "diff_images",
                        lambda b, a, **k: visual.VisualDiff(True, mag[0], "x", b"overlay"))

    mon = N(detection_mode=DetectionMode.visual, min_change_threshold=0.0,
            normalize_whitespace=True, normalize_numbers=False, ignore_patterns=[])
    prev = N(screenshot_blob="x", screenshot_mobile_blob=None, rendered_text="")
    cur = N(screenshot_png=b"after", screenshot_mobile_png=None, rendered_text="", html="")

    assert detector.detect(mon, prev, cur).changed is False   # 0.2% < floor → noise
    mag[0] = 0.02                                             # 2% — a real change
    assert detector.detect(mon, prev, cur).changed is True

    # auto mode (text identical) is governed by the same floor
    mon.detection_mode = DetectionMode.auto
    mag[0] = 0.002
    assert detector.detect(mon, prev, cur).changed is False


def test_html_to_text():
    from watcher.web.routes.monitors import _html_to_text
    out = _html_to_text(
        "<style>a{color:red}</style><h1>Hi</h1><script>var x=1</script><p>Price &amp; more</p>"
    )
    assert "Hi" in out and "Price & more" in out
    assert "color:red" not in out and "var x" not in out


# --- AI self-healing consent learning --------------------------------------

def _consent_args(monkeypatch, *, returned_selectors, ai_on=True, key="k",
                  walls=("<div id=cmp>cookies</div>",), existing=None):
    """Wire up app/monitor/result + stubs for _maybe_learn_consent and return them."""
    import watcher.ai as ai
    import watcher.runner as R

    async def _fake_suggest(**kw):
        _fake_suggest.kw = kw
        return returned_selectors
    _fake_suggest.kw = None
    monkeypatch.setattr(ai, "suggest_consent_selectors", _fake_suggest)
    monkeypatch.setattr(R, "get_openrouter_key", lambda app: key)

    app = N(ai_enabled=ai_on, ai_model="m", ai_base_url=None)
    monitor = N(block_annoyances=True, ai_enabled=True, id=1, consent_ai_tried=False,
                url="https://x", consent_clicks=list(existing or []))
    result = N(unhandled_consent_html=list(walls) if walls else None)
    return R, app, monitor, result, _fake_suggest


def test_maybe_learn_consent_caches_selectors(monkeypatch):
    import asyncio
    R, app, monitor, result, _ = _consent_args(
        monkeypatch, returned_selectors=["#accept-all", "button.ok"])
    asyncio.run(R._maybe_learn_consent(app, monitor, result))
    assert monitor.consent_clicks == ["#accept-all", "button.ok"]
    assert monitor.consent_ai_tried is True


def test_maybe_learn_consent_one_shot_even_when_ai_returns_nothing(monkeypatch):
    """A persistent overlay the AI can't solve must NOT re-spend tokens: after one
    empty result, consent_ai_tried is set and a second call is a no-op."""
    import asyncio
    R, app, monitor, result, stub = _consent_args(monkeypatch, returned_selectors=None)
    asyncio.run(R._maybe_learn_consent(app, monitor, result))
    assert monitor.consent_ai_tried is True and monitor.consent_clicks == []
    stub.kw = None                      # reset call marker
    asyncio.run(R._maybe_learn_consent(app, monitor, result))
    assert stub.kw is None              # not called again — bounded to one attempt


def test_maybe_learn_consent_skips_when_no_wall(monkeypatch):
    import asyncio
    R, app, monitor, result, stub = _consent_args(
        monkeypatch, returned_selectors=["#x"], walls=None)
    asyncio.run(R._maybe_learn_consent(app, monitor, result))
    assert monitor.consent_clicks == []
    assert stub.kw is None                      # AI never called


def test_maybe_learn_consent_skips_when_already_have_clicks(monkeypatch):
    import asyncio
    R, app, monitor, result, stub = _consent_args(
        monkeypatch, returned_selectors=["#new"], existing=["#manual"])
    asyncio.run(R._maybe_learn_consent(app, monitor, result))
    assert monitor.consent_clicks == ["#manual"]   # untouched; AI not re-spent
    assert stub.kw is None


def test_maybe_learn_consent_skips_when_ai_off(monkeypatch):
    import asyncio
    R, app, monitor, result, stub = _consent_args(
        monkeypatch, returned_selectors=["#x"], ai_on=False)
    asyncio.run(R._maybe_learn_consent(app, monitor, result))
    assert monitor.consent_clicks == []
    assert stub.kw is None


# --- captcha/anti-bot "needs help" alert hint --------------------------------

def test_help_hint_fires_on_antibot_and_login():
    from watcher.runner import _help_hint
    for reason in ("Blocked — HTTP 403 · DataDome anti-bot protection",
                   "Blocked — HTTP 401 · Cloudflare anti-bot protection",
                   "Blocked — PerimeterX challenge page (no content rendered)"):
        h = _help_hint(reason)
        assert "Needs your help" in h and "Session cookies" in h, reason

def test_help_hint_silent_on_ordinary_errors():
    from watcher.runner import _help_hint
    assert _help_hint("Timeout 30000ms exceeded") == ""
    assert _help_hint("net::ERR_NAME_NOT_RESOLVED") == ""
    assert _help_hint(None) == ""


# --- AI login: code-event race + session ------------------------------------

def test_login_session_code_event():
    """The code event handles both orders: submit-then-wait (the status/submit
    race) and wait-then-submit, and isolates each code."""
    import asyncio
    from watcher.auth.ai_login import create_session

    async def _t():
        s = create_session(7, 3, "https://x.test/login", {"username": "u", "password": "p"})
        # submitted before the agent starts waiting → returned immediately
        s.submit_code(" 111 ")
        assert await s._wait_for_code() == "111"
        # normal order: agent waits, user submits shortly after
        async def later():
            await asyncio.sleep(0.05)
            s.submit_code("222")
        t = asyncio.create_task(later())
        assert await s._wait_for_code() == "222"
        await t
        # ownership check
        from watcher.auth import ai_login
        assert ai_login.get_session(s.id, 3) is s
        assert ai_login.get_session(s.id, 999) is None
    asyncio.run(_t())


def test_credential_for_field_guard():
    """A credential field is always filled from the stored secret, never free-typed
    — so the model can't invent (or leak) an email/password into the page."""
    from watcher.auth.ai_login import _credential_for_field
    secrets = {"username": "me@x.com", "password": "pw"}
    assert _credential_for_field({"type": "email", "name": "__email"}, secrets) == "username"
    assert _credential_for_field({"type": "text", "name": "login"}, secrets) == "username"
    assert _credential_for_field({"type": "password", "name": "pass"}, secrets) == "password"
    assert _credential_for_field({"type": "text", "autocomplete": "username"}, secrets) == "username"
    # not a credential field → free text allowed
    assert _credential_for_field({"type": "text", "name": "search"}, secrets) is None
    # secret missing → don't claim the field
    assert _credential_for_field({"type": "password"}, {"username": "u"}) is None
