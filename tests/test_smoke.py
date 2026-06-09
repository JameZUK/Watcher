"""Smoke tests: detection logic, blob store, blocked-detection, and app import."""

import io

from PIL import Image


# --- text / structured detection ------------------------------------------

def test_text_diff_detects_changes():
    from watcher.detection.text import diff_text
    d = diff_text("hello\nworld", "hello\nWORLD\nextra")
    assert d.changed
    assert d.added == 2 and d.removed == 1
    assert 0 < d.magnitude <= 1


def test_text_diff_no_change():
    from watcher.detection.text import diff_text
    assert not diff_text("same\ntext", "same\ntext").changed


def test_noise_normalization_suppresses_numbers_and_timestamps():
    from watcher.detection.noise import normalize_text
    a = normalize_text("Price: $12.99 at 10:42:01", numbers=True,
                        ignore_patterns=[r"\d{2}:\d{2}:\d{2}"])
    b = normalize_text("Price: $13.50 at 11:00:00", numbers=True,
                       ignore_patterns=[r"\d{2}:\d{2}:\d{2}"])
    assert a == b


def test_html_diff():
    from watcher.detection.structured import diff_html
    assert diff_html("<div><p>one</p></div>", "<div><p>one</p><p>two</p></div>").changed


def test_json_diff():
    from watcher.detection.structured import diff_json
    d = diff_json('{"a": 1, "b": 2}', '{"a": 1, "b": 3, "c": 4}')
    assert d.changed


# --- visual detection ------------------------------------------------------

def _png(color, box=None):
    img = Image.new("RGB", (80, 60), color)
    if box:
        for x in range(box[0], box[2]):
            for y in range(box[1], box[3]):
                img.putpixel((x, y), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_visual_diff():
    from watcher.detection.visual import diff_images
    assert not diff_images(_png("white"), _png("white")).changed
    changed = diff_images(_png("white"), _png("white", (5, 5, 30, 30)))
    assert changed.changed and changed.overlay_png


# --- storage ---------------------------------------------------------------

def test_blob_store_dedup():
    from watcher.config import settings
    settings.ensure_dirs()
    from watcher.storage import blobs
    k1 = blobs.put_text("hello")
    k2 = blobs.put_text("hello")
    k3 = blobs.put_text("different")
    assert k1 == k2 and k1 != k3
    assert blobs.get_text(k1) == "hello"


# --- blocked / anti-bot detection ------------------------------------------

def test_blocked_reason_flags_datadome():
    from watcher.runner import _blocked_reason
    from watcher.engines.base import RenderResult
    r = RenderResult(ok=True, http_status=403,
                     html="<html>...captcha-delivery.com...</html>", rendered_text="")
    assert "DataDome" in (_blocked_reason(r) or "")


def test_blocked_reason_passes_clean_page():
    from watcher.runner import _blocked_reason
    from watcher.engines.base import RenderResult
    assert _blocked_reason(RenderResult(ok=True, http_status=200,
                                        html="<html><body>ok</body></html>",
                                        rendered_text="ok")) is None


# --- app wiring ------------------------------------------------------------

def test_app_imports_and_has_routes():
    from watcher.main import app
    paths = {r.path for r in app.routes}
    for expected in ("/", "/login", "/monitors", "/inbox", "/settings"):
        assert expected in paths
