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


def test_html_to_text():
    from watcher.web.routes.monitors import _html_to_text
    out = _html_to_text(
        "<style>a{color:red}</style><h1>Hi</h1><script>var x=1</script><p>Price &amp; more</p>"
    )
    assert "Hi" in out and "Price & more" in out
    assert "color:red" not in out and "var x" not in out
