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


def test_encrypted_json_roundtrip_and_legacy_fallback():
    """session_state is Fernet-encrypted at rest, round-trips to the same dict,
    and still reads legacy plaintext-JSON rows (so existing data isn't lost)."""
    import json as _json
    from watcher.models import EncryptedJSON
    t = EncryptedJSON()
    val = {"cookies": [{"name": "sid", "value": "s3cr3t-cookie"}], "origins": []}
    enc = t.process_bind_param(val, None)
    assert isinstance(enc, str) and "s3cr3t-cookie" not in enc   # stored as ciphertext
    assert t.process_result_value(enc, None) == val              # decrypts back
    # legacy plaintext JSON (written before encryption) still loads
    assert t.process_result_value(_json.dumps(val), None) == val
    # a Fernet-looking token the current key can't decrypt → None (and logs), rather
    # than being misread as plaintext (the data is unrecoverable, signal it)
    assert t.process_result_value("gAAAAA-not-a-real-token", None) is None
    # None passes through untouched
    assert t.process_bind_param(None, None) is None
    assert t.process_result_value(None, None) is None


def test_proxy_pool_parse_and_pick():
    """parse_pool keeps only scheme-valid proxies; effective_proxy prefers the
    monitor's own, else round-robins a healthy pooled one, else None."""
    import watcher.proxy_pool as P
    assert P.parse_pool("http://h:1\nnope\n  \nsocks5://h:2\nftp://x:3") == [
        "http://h:1", "socks5://h:2"]
    P._pool = ["http://a:1", "http://b:2"]
    P._healthy = {"http://a:1": True, "http://b:2": False}
    P._rebuild_cycle()
    assert P.effective_proxy(N(proxy="http://own:9", use_proxy_pool=True)) == "http://own:9"
    assert P.effective_proxy(N(proxy=None, use_proxy_pool=True)) == "http://a:1"   # only healthy
    assert P.effective_proxy(N(proxy=None, use_proxy_pool=False)) is None
    assert P.healthy_count() == (1, 2)
    P._pool, P._healthy = [], {}; P._rebuild_cycle()
    assert P.effective_proxy(N(proxy=None, use_proxy_pool=True)) is None           # empty pool


def test_totp_verify_step():
    """verify_step returns the matched step for a fresh code (enabling single-use)
    and None for wrong/short codes."""
    import pyotp
    from watcher.auth.otp import verify, verify_step
    sec = pyotp.random_base32()
    code = pyotp.TOTP(sec).now()
    assert verify_step(sec, code) is not None and verify(sec, code) is True
    assert verify_step(sec, "000000") is None          # wrong code
    assert verify_step(sec, "12345") is None            # wrong length
    assert verify_step(None, code) is None              # no secret


def test_handoff_unattended_fails_clean():
    """In unattended (auto re-login) mode a handoff is a hard error — no human can
    take over — whereas interactive mode hands off to manual control as before."""
    from watcher.auth.ai_login import LoginSession, _handoff
    s = LoginSession(id="x", monitor_id=1, user_id=1, url="http://t", secrets={})
    s._unattended = True
    _handoff(s, "a captcha is blocking")
    assert s.status == "error" and s.mode == "ai" and "captcha" in s.error

    s2 = LoginSession(id="y", monitor_id=1, user_id=1, url="http://t", secrets={})
    _handoff(s2, "a captcha is blocking")
    assert s2.status == "running" and s2.mode == "manual"   # interactive: takeover


def test_auto_relogin_eligibility():
    """_relogin_blocked_reason gates auto re-login: only an opted-in monitor with a
    stored-but-expired session, credentials, an AI key, and no cooldown qualifies."""
    from datetime import datetime, timezone
    import watcher.runner as R
    from watcher.auth.security import encrypt_secret
    past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc)
    app = N(ai_enabled=True, openrouter_key_enc=encrypt_secret("sk-test"),
            ai_model="m", ai_base_url=None)
    no_ai = N(ai_enabled=False, openrouter_key_enc=None)

    def mon(**kw):
        return N(**{"id": 1, "user_id": 1, "auto_relogin_enabled": True, **kw})

    def flow(valid=False, creds=True, state=True, cooldown=None):
        return N(session_state=({"cookies": []} if state else None),
                 session_valid_until=(future if valid else past),
                 relogin_cooldown_until=cooldown,
                 encrypted_secrets=({"username": "u", "password": "p"} if creds else {}))

    assert R._relogin_blocked_reason(mon(auto_relogin_enabled=False), flow(), app) == "not enabled"
    assert R._relogin_blocked_reason(mon(), flow(state=False), app) == "no stored session"
    assert R._relogin_blocked_reason(mon(), flow(valid=True), app) == "session still valid"
    assert R._relogin_blocked_reason(mon(), flow(creds=False), app) == "no stored credentials"
    assert R._relogin_blocked_reason(mon(), flow(), no_ai) == "AI not configured"
    assert R._relogin_blocked_reason(mon(), flow(), app) is None            # all conditions met
    assert "cooldown" in R._relogin_blocked_reason(mon(), flow(cooldown=future), app)  # in cooldown
    assert R._relogin_blocked_reason(mon(), flow(cooldown=past), app) is None           # cooldown lapsed


def test_screenshot_compressed_to_webp_under_budget():
    """A large capture is downscaled to the megapixel budget and re-encoded as WebP,
    and still decodes (readable)."""
    from io import BytesIO
    from PIL import Image
    import watcher.engines._common as C
    from watcher.config import settings
    buf = BytesIO()
    Image.new("RGB", (2560, 12000), "white").save(buf, format="PNG")   # ~31 MP, over budget
    out = C._compress_screenshot(buf.getvalue())
    res = Image.open(BytesIO(out))
    assert res.format == "WEBP"
    assert res.width * res.height <= settings.max_screenshot_megapixels * 1_000_000
    res.load()                                                          # decodes → readable


def test_image_media_type_detection(tmp_path):
    """The blob server sniffs WebP / PNG / JPEG from the file header."""
    from PIL import Image
    from watcher.web.routes.monitors import _image_media_type
    for fmt, ext, mt in (("PNG", "png", "image/png"), ("WEBP", "webp", "image/webp"),
                         ("JPEG", "jpg", "image/jpeg")):
        p = tmp_path / f"x.{ext}"
        Image.new("RGB", (4, 4), "white").save(p, fmt)
        assert _image_media_type(p) == mt


def test_churn_learns_recurring_lines_and_ignores_one_offs():
    """A line that flips on most checks rises above the churn threshold; a one-off
    decays away. The leaky bucket bounds and converges."""
    from watcher.detection import churn

    state: dict = {}
    uniq = ["alpha", "bravo", "charlie", "delta", "echo"]
    # A job-count line churns every check (caught via digit-masking); the rest is a
    # genuinely unique remark each time.
    for i in range(5):
        diff = (f"@@\n-{36 + i} jobs in United Kingdom\n+{37 + i} jobs in United Kingdom\n"
                f"+A unique remark about {uniq[i]} appearing only once")
        state = churn.update(state, churn.changed_lines(diff))
    texts = churn.churny_texts(state)
    assert any("jobs in united kingdom" in t.lower() for t in texts)   # counter → flagged
    assert not any("unique remark" in t.lower() for t in texts)        # one-offs → not

    # A line that stops churning decays back out below the threshold.
    for _ in range(6):
        state = churn.update(state, churn.changed_lines("@@\n-foo bar\n+baz qux"))  # jobs absent
    assert not any("jobs in united kingdom" in t.lower() for t in churn.churny_texts(state))


def test_domain_helper_for_site_aware_summaries():
    """The fleet summary's site label uses the bare host, stripping a 'www.' prefix
    correctly (not str.lstrip, which would also eat a leading 'w' — 'walmart.com')."""
    from watcher.web.routes.dashboard import _domain
    assert _domain("https://www.amazon.co.uk/dp/X") == "amazon.co.uk"
    assert _domain("https://walmart.com/ip/Y") == "walmart.com"      # not 'almart.com'
    assert _domain("https://uk.jbl.com/x.html") == "uk.jbl.com"
    assert _domain("") == "" and _domain(None) == ""


def test_churn_changed_lines_skips_headers_and_short():
    """Diff headers and trivially short lines aren't tracked as churn."""
    from watcher.detection import churn
    lines = churn.changed_lines("--- a\n+++ b\n@@ -1 +1 @@\n+\n-ab\n+A real changed line")
    texts = [t for _h, t in lines]
    assert texts == ["A real changed line"]


def test_visual_diff_bounds_mismatched_canvas():
    """Diffing two captures with very different aspect ratios (a tall section vs a
    legacy full-page capture) must stay within the diff megapixel budget — otherwise
    _fit's padded canvas balloons and the pure-Python pixelmatch blows the detect
    timeout (the cause of The Register's 'detect() aborted')."""
    from io import BytesIO

    from PIL import Image
    from watcher.config import settings
    from watcher.detection import visual

    def png(w, h):
        b = BytesIO(); Image.new("RGB", (w, h), "white").save(b, format="PNG"); return b.getvalue()

    vd = visual.diff_images(png(700, 16000), png(1800, 11000))   # 1:23 vs 1:6
    overlay = Image.open(BytesIO(vd.overlay_png))
    assert overlay.width * overlay.height <= settings.max_diff_megapixels * 1_000_000 * 1.05


def test_slice_sections_tiles_whole_page():
    """A long page is sliced into multiple readable, full-width WebP sections that
    together cover the whole page; a short page yields a single section; the section
    count is capped so a near-infinite page can't balloon storage."""
    from io import BytesIO

    from PIL import Image
    from watcher.config import settings
    from watcher.engines._common import _WEBP_MAX_DIM, _slice_sections

    sec_css = settings.screenshot_section_height_px
    dpr = 2.0
    band = int(sec_css * dpr)

    def png_of(h):
        buf = BytesIO(); Image.new("RGB", (780, h), "white").save(buf, format="PNG")
        return buf.getvalue()

    # Short page (< one band) → exactly one section.
    one = _slice_sections(png_of(band // 2), dpr)
    assert len(one) == 1 and Image.open(BytesIO(one[0])).format == "WEBP"

    # ~3.5 bands tall → 4 sections, each within WebP's limit.
    many = _slice_sections(png_of(int(band * 3.5)), dpr)
    assert len(many) == 4
    for b in many:
        im = Image.open(BytesIO(b))
        assert im.format == "WEBP" and max(im.width, im.height) <= _WEBP_MAX_DIM

    # Absurdly tall → capped at max_screenshot_sections (no runaway).
    capped = _slice_sections(png_of(band * 50), dpr)
    assert len(capped) == settings.max_screenshot_sections


def test_full_page_capture_short_vs_tall():
    """Short page → one full_page=True shot (fast path). Tall page → scrolled viewport
    strips (no full_page, which blanks in Chromium beyond the raster limit), stitched.
    Regression: The Register captured blank/white because a single full_page shot of a
    ~30k px page came back unpainted."""
    import asyncio
    from io import BytesIO

    from PIL import Image
    from watcher.engines._common import full_page_sections

    def png(w=24, h=24):
        b = BytesIO(); Image.new("RGB", (w, h), "white").save(b, "PNG"); return b.getvalue()

    class Page:
        def __init__(self, scroll_h): self.h = scroll_h; self.y = 0; self.shots = []
        async def evaluate(self, js, *a):
            if "devicePixelRatio" in js: return 2
            if "scrollHeight" in js and "{" in js:
                return {"h": self.h, "w": 1280, "vw": 1280, "vh": 800}
            if "scrollHeight" in js: return self.h
            if "scrollTo" in js: self.y = a[0] if a else 0; return None
            if "scrollY" in js: return self.y
            if "innerText" in js: return 100      # < 500 → no blank-retry
            return None
        async def set_viewport_size(self, kw): return None
        async def wait_for_timeout(self, ms): return None
        async def screenshot(self, **kw): self.shots.append(kw); return png()

    short = Page(scroll_h=500)
    asyncio.run(full_page_sections(short))
    assert any(s.get("full_page") for s in short.shots)           # fast path
    assert not any("clip" in s for s in short.shots)

    tall = Page(scroll_h=40000)
    asyncio.run(full_page_sections(tall))
    assert tall.shots and not any(s.get("full_page") for s in tall.shots)  # strips, not full_page
    assert len(tall.shots) >= 3                                    # multiple scrolled strips


def test_compress_screenshot_handles_huge_tall_pages():
    """A capture that's both over the megapixel budget AND longer than WebP's 16383px
    limit (and big enough to trip Pillow's decompression-bomb guard) must still encode
    to a valid, bounded WebP — not silently fall back to the raw multi-MB PNG."""
    from io import BytesIO

    from PIL import Image
    from watcher.engines._common import _WEBP_MAX_DIM, _compress_screenshot

    # 2560 x 40000 ≈ 102 MP, aspect 1:15.6 — both limits exceeded at once.
    buf = BytesIO()
    Image.new("RGB", (2560, 40000), "white").save(buf, format="PNG")
    out = _compress_screenshot(buf.getvalue())            # no height crop
    im = Image.open(BytesIO(out))
    assert im.format == "WEBP"
    assert max(im.width, im.height) <= _WEBP_MAX_DIM       # within WebP's limit
    assert im.width * im.height <= 20 * 1_000_000 * 1.05   # within the megapixel budget

    # With a height crop, only the top is kept (the rest of the page is dropped),
    # so the encoded image is far shorter than the original 40000px.
    cropped = _compress_screenshot(buf.getvalue(), max_height_px=8000)
    ic = Image.open(BytesIO(cropped))
    assert ic.format == "WEBP" and ic.height <= 8000 and ic.width >= 2000  # near full width


def test_mobile_screenshot_keeps_low_text_pages():
    """The mobile pass must NOT discard a page just because its visible text is
    low — SPA / shadow-DOM pages (modern e-commerce) read as near-empty even when
    fully rendered, and the desktop pass already proved the site isn't blocking us.
    Only a recognised challenge interstitial is rejected. Regression: JBL's mobile
    preview kept falling back to the desktop image because innerText was ~0."""
    import asyncio
    from watcher.engines import _common

    async def fake_png(page):
        return b"PNGDATA"

    _orig = _common.full_page_png
    _common.full_page_png = fake_png
    try:
        class Page:
            def __init__(self, text): self.text = text
            async def evaluate(self, js, *a):
                return self.text

        # Empty/low innerText but not a challenge → keep the capture.
        assert asyncio.run(_common.mobile_screenshot_or_none(Page(""))) == b"PNGDATA"
        assert asyncio.run(_common.mobile_screenshot_or_none(Page("a product page"))) == b"PNGDATA"
        # A real anti-bot interstitial → reject (don't store the challenge page).
        assert asyncio.run(_common.mobile_screenshot_or_none(
            Page("Just a moment... checking your browser before accessing"))) is None
    finally:
        _common.full_page_png = _orig


def test_summarize_fleet_includes_user_instruction():
    """A user's free-text summary preference is passed to the model as an extra
    style hint (so the dashboard summary is customisable)."""
    import asyncio
    import json as _json
    from watcher.ai import triage

    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content":
                    _json.dumps({"segments": [{"text": "ok", "monitor_id": 0}]})}}]}

    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            captured["body"] = json
            return FakeResp()

    _orig = triage.httpx.AsyncClient
    triage.httpx.AsyncClient = FakeClient
    try:
        changes = [{"monitor_id": 5, "monitor": "Alpha", "importance": "high", "headline": "Price"}]
        out = asyncio.run(triage.summarize_fleet(
            api_key="k", model="m", changes=changes, instruction="Lead with price drops."))
    finally:
        triage.httpx.AsyncClient = _orig

    contents = " ".join(m["content"] for m in captured["body"]["messages"])
    assert "Lead with price drops." in contents
    assert out == [{"text": "ok", "monitor_id": 0}]


def test_navigate_falls_back_when_wait_never_settles():
    """A strict wait (networkidle) that never settles must not fail the check —
    navigate() retries with domcontentloaded and returns the page that loaded."""
    import asyncio
    from playwright.async_api import TimeoutError as PWTimeout
    from watcher.engines._common import navigate

    class Page:
        def __init__(self, ok_on): self.ok_on, self.calls = ok_on, []
        async def goto(self, url, wait_until, timeout):
            self.calls.append(wait_until)
            if wait_until in self.ok_on:
                return N(status=200)
            raise PWTimeout("never idle")

    # networkidle times out → falls back to domcontentloaded, succeeds
    p = Page(ok_on={"domcontentloaded"})
    mon = N(url="https://x.test", wait_until="networkidle", wait_timeout_ms=15000)
    resp = asyncio.run(navigate(p, mon))
    assert resp.status == 200 and p.calls == ["networkidle", "domcontentloaded"]

    # already the most lenient wait and it still times out → a genuine failure
    p2 = Page(ok_on=set())
    mon2 = N(url="https://x.test", wait_until="domcontentloaded", wait_timeout_ms=15000)
    with pytest.raises(PWTimeout):
        asyncio.run(navigate(p2, mon2))
    assert p2.calls == ["domcontentloaded"]   # no pointless second attempt


def test_looks_blocked_detects_antibot_walls():
    """A cold deep-link that hit an anti-bot wall (4xx or a short challenge
    interstitial) is flagged for a warm-up retry; real content is not."""
    from watcher.engines._common import _looks_blocked
    # HTTP status walls
    assert _looks_blocked(N(status=403), "")
    assert _looks_blocked(N(status=401), "anything")
    assert _looks_blocked(N(status=429), "")
    # short challenge interstitial at HTTP 200 (Glassdoor "Humans only")
    assert _looks_blocked(N(status=200), "Humans only\nWe use advanced security systems")
    assert _looks_blocked(None, "Just a moment...")
    # real content is NOT blocked
    assert not _looks_blocked(N(status=200), "x" * 4000)
    # a long page that merely mentions a phrase isn't a challenge
    assert not _looks_blocked(N(status=200), "great company " * 200 + "humans only")
    assert not _looks_blocked(N(status=200), "normal page content here")


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


def test_cookie_summary_and_rows():
    from watcher.auth.login_flows import cookie_rows, cookie_summary
    state = {"cookies": [
        {"name": "sess", "value": "secret1", "domain": ".glassdoor.com", "path": "/", "expires": 1800000000},
        {"name": "gdId", "value": "secret2", "domain": ".glassdoor.com", "path": "/", "expires": -1},
        {"name": "PPID", "value": "secret3", "domain": ".indeed.com", "path": "/"},
    ], "origins": [{"origin": "https://x"}]}
    s = cookie_summary(state)
    assert s["count"] == 3
    doms = {d["domain"]: d["names"] for d in s["domains"]}
    assert doms[".glassdoor.com"] == ["gdId", "sess"] and doms[".indeed.com"] == ["PPID"]
    # rows hide values unless asked; scope reflects expiry
    rows = cookie_rows(state)
    assert all("value" not in r for r in rows)
    by = {r["name"]: r for r in rows}
    assert by["sess"]["session"] is False and by["gdId"]["session"] is True
    assert by["PPID"]["session"] is True   # no expiry → session
    assert "value" in cookie_rows(state, with_values=True)[0]


def test_apply_cookie_edits():
    from watcher.auth.login_flows import apply_cookie_edits
    state = {"cookies": [
        {"name": "a", "value": "v1", "domain": ".x.com", "path": "/", "expires": 123, "httpOnly": True},
        {"name": "b", "value": "v2", "domain": ".x.com", "path": "/"},
        {"name": "c", "value": "v3", "domain": ".y.com", "path": "/"},
    ], "origins": [{"origin": "https://x.com"}]}
    # delete 'c' (omit it), edit a's value, keep b's value implicitly (no value key)
    edited = [
        {"name": "a", "domain": ".x.com", "path": "/", "value": "NEW"},
        {"name": "b", "domain": ".x.com", "path": "/"},
    ]
    out = apply_cookie_edits(state, edited)
    names = {c["name"]: c for c in out["cookies"]}
    assert set(names) == {"a", "b"}                 # c deleted
    assert names["a"]["value"] == "NEW"             # value edited
    assert names["a"]["httpOnly"] is True           # other attrs preserved
    assert names["b"]["value"] == "v2"              # unchanged value kept
    assert out["origins"] == state["origins"]       # localStorage preserved
    # validation
    import pytest as _pt
    with _pt.raises(ValueError):
        apply_cookie_edits(state, [{"name": "", "domain": ".x.com"}])
    with _pt.raises(ValueError):
        apply_cookie_edits(state, "not a list")


def test_login_flow_save_preserves_session():
    """Re-saving a monitor with the SAME login steps must NOT wipe a session the
    AI login (or a cookie paste) captured — only a CHANGE to the steps does."""
    from watcher.web.routes.monitors import _build_login_flow
    from watcher.models import LoginFlow, Monitor

    saved = {"cookies": [{"name": "sess", "domain": "example.com"}]}
    steps = [{"action": "goto", "url": "https://example.com/login"}]

    monitor = Monitor(id=1, url="https://example.com/page")
    monitor.login_flow = LoginFlow(monitor_id=1, steps=list(steps), session_state=dict(saved))
    out = _build_login_flow(monitor, {"login_enabled": "1", "login_url": "https://example.com/login"})
    assert out.session_state == saved   # same config → session preserved

    # Changing the login URL DOES invalidate the stale session.
    monitor.login_flow = LoginFlow(monitor_id=1, steps=list(steps), session_state=dict(saved))
    out2 = _build_login_flow(monitor, {"login_enabled": "1", "login_url": "https://example.com/other"})
    assert out2.session_state is None


def test_is_code_field():
    """One-time-code fields are recognised (so a filled code page is never
    mistaken for the signed-in page, and credentials aren't typed into them)."""
    from watcher.auth.ai_login import _is_code_field
    assert _is_code_field({"tag": "input", "autocomplete": "one-time-code"})
    assert _is_code_field({"tag": "input", "placeholder": "Enter the code"})
    assert _is_code_field({"tag": "input", "name": "otp"})
    assert _is_code_field({"tag": "input", "aria": "Verification code"})
    assert not _is_code_field({"tag": "input", "type": "email", "name": "__email"})
    assert not _is_code_field({"tag": "button", "label": "Continue"})


def test_is_social_login():
    """SSO buttons are recognised so the agent never goes down a Google/Apple
    path — but Glassdoor's email gateway ('Continue with Apple or email') is not
    treated as social."""
    from watcher.auth.ai_login import _is_social_login
    assert _is_social_login({"label": "Continue with Google"})
    assert _is_social_login({"label": "Continue with Apple"})
    assert _is_social_login({"aria": "Sign in with Facebook"})
    assert not _is_social_login({"label": "Continue with Apple or email"})
    assert not _is_social_login({"label": "Continue"})
    assert not _is_social_login({"label": "Email address"})


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


def test_engine_error_message():
    from watcher.auth.ai_login import engine_error_message
    wall = ("BrowserType.launch: Host system is missing dependencies to run browsers.\n"
            "Missing libraries:\n  libwoff2dec.so.1.0.2\n  libgtk-4.so.1")
    msg = engine_error_message(wall)
    assert msg and "isn't available" in msg and "Docker" in msg
    assert "libwoff2dec" not in msg            # the raw wall is gone
    assert engine_error_message("Executable doesn't exist at /path") is not None
    assert engine_error_message("TimeoutError: nope") is None
