"""Playwright UI test for the change-highlight zoom lightbox.

Drives a real browser against a live server and exercises the lightbox detail-popup
behaviour that is pure front-end logic in base.html:

  - hover (mouse) reveals a POPULATED popup — guards the "empty popup" bug
  - tap (touch) reveals it, tap again hides it — touch devices have no hover
  - the popup stays ON-SCREEN near the pointer even when hovering the right edge of a
    full-width (mobile-style) box — guards the off-screen-popup bug
  - a mobile capture opens at a smaller "fit" zoom (zoomed out), not 100%
  - close → reopen leaves NO stale/empty popup showing — guards "empty and stays"

Skips cleanly where Playwright or its Chromium build isn't installed (minimal CI); it
runs in the Docker image or a dev box that ran `playwright install chromium`.
"""

import socket
import threading
import time
import uuid

import pytest

# Memory-frugal Chromium: a single process (no separate renderer/gpu/utility procs)
# keeps RAM low enough to run inside the whole `pytest tests/` suite without the OOM
# killer firing (exit 137). --disable-dev-shm-usage avoids the tiny default /dev/shm.
_LAUNCH_ARGS = [
    "--single-process", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_http(url: str, timeout: float = 25) -> bool:
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return True
        except Exception:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def live_server():
    """A real uvicorn server (fresh DB via the app's own lifespan) on an ephemeral port."""
    sync_mod = pytest.importorskip("playwright.sync_api")
    try:                                   # need an actual browser binary, not just the lib
        with sync_mod.sync_playwright() as p:
            p.chromium.launch(args=_LAUNCH_ARGS).close()
    except Exception as e:                 # pragma: no cover - env-dependent
        pytest.skip(f"no Chromium browser available: {e}")

    import uvicorn
    from watcher.main import create_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    if not _wait_http(base + "/login"):    # pragma: no cover
        server.should_exit = True
        pytest.skip("server did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# Opens the lightbox with a synthetic overlay: a narrow box + a FULL-WIDTH box (x=0,
# w=pw — the mobile shape whose side-anchored popup used to clip off-screen). Shape
# mirrors the real /changes/{id}/overlays payload. `fit` is the default zoom.
_OPEN_JS = """
([fit]) => {
  const boxes = [
    {x:120, y:60,  w:300,  h:22, kind:'added',   type:'item', snippet:'Added alpha line'},
    {x:0,   y:400, w:1000, h:22, kind:'changed', type:'item', snippet:'changed snippet',
     diff:[{op:'del',t:'old'},{op:'add',t:'new'},{op:'same',t:'tail'}]},
  ];
  const links = [
    {href:'https://example.com/story', x:120, y:120, w:320, h:20, t:'A story'},
    // sits UNDER the narrow 'added' box (y:60) → clicking that box must open THIS link
    {href:'https://example.com/boxed', x:120, y:62,  w:300, h:18, t:'Boxed link'},
  ];
  const img = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';
  Alpine.store('lb').open([img], 0, {boxes, dims:{pw:1000, ph:2000}, fit,
                                     links, linkDims:{pw:1000, ph:2000}});
}
"""


def _login(page, base):
    page.goto(base + "/register")          # first-user bootstrap; register auto-logs-in
    page.fill("input[name=email]", f"ui-{uuid.uuid4().hex[:8]}@example.test")
    page.fill("input[name=password]", "test-password-123")
    page.press("input[name=password]", "Enter")   # submit (button has no explicit type)
    page.wait_for_url(lambda u: u.startswith(base) and "/login" not in u and "/register" not in u)
    page.wait_for_function("() => window.Alpine && Alpine.store && Alpine.store('lb')")


def _open(page, fit):
    page.evaluate(_OPEN_JS, [fit])
    page.wait_for_selector(".ov-box")


def test_lightbox_popup_hover_tap_zoom_and_no_stale(live_server):
    from playwright.sync_api import sync_playwright
    base = live_server
    with sync_playwright() as p:
        browser = p.chromium.launch(args=_LAUNCH_ARGS)
        ctx = browser.new_context(viewport={"width": 900, "height": 700}, has_touch=True)
        page = ctx.new_page()
        try:
            _login(page, base)
            pop = page.locator(".ov-pop-pin")
            box = page.locator(".ov-box").nth(1)   # the full-width "changed" box
            vw, vh = 900, 700

            # --- desktop fit: opens at 100% ---
            _open(page, 1)
            assert page.evaluate("() => Alpine.store('lb').zoom") == 1

            # --- clickable-link overlay: real target=_blank anchors, on the page ---
            anchors = page.locator(".fixed.inset-0 .lb-link")
            anchors.first.wait_for(state="visible", timeout=3000)
            assert anchors.count() == 2
            a0 = anchors.first
            assert a0.get_attribute("href") == "https://example.com/story"
            assert a0.get_attribute("target") == "_blank"
            assert "noopener" in (a0.get_attribute("rel") or "")
            ar = a0.bounding_box()
            assert ar["x"] >= 0 and ar["x"] + ar["width"] <= vw + 1 and ar["width"] > 0

            # --- link + highlight box coexist: clicking the 'added' box (which sits over a
            #     link) opens the link via elementsFromPoint delegation, not zoom/popup ---
            boxed = page.locator(".fixed.inset-0 .ov-box").nth(0)   # the 'added' box over a link
            with ctx.expect_page() as popup_info:
                boxed.click()
            popup = popup_info.value
            assert "example.com/boxed" in popup.url
            popup.close()

            # --- hover (mouse) the RIGHT EDGE of the full-width box ---
            page.mouse.move(5, 5)                  # start outside any box
            bb = box.bounding_box()
            page.mouse.move(bb["x"] + bb["width"] - 8, bb["y"] + bb["height"] / 2)
            pop.wait_for(state="visible", timeout=3000)
            txt = pop.inner_text().strip()
            assert txt and "item" in txt.lower()              # POPULATED, not empty
            pr = pop.bounding_box()
            assert pr["x"] >= 0 and pr["x"] + pr["width"] <= vw + 1    # on-screen (clamped)
            assert pr["y"] >= 0 and pr["y"] + pr["height"] <= vh + 1

            # moving off the box hides it
            page.mouse.move(5, 5)
            pop.wait_for(state="hidden", timeout=3000)

            # --- tap (touch) reveals it; tap again toggles it off ---
            box.tap()
            pop.wait_for(state="visible", timeout=3000)
            assert pop.inner_text().strip()                   # populated on first tap too
            page.wait_for_timeout(600)                        # avoid a double-tap (zoom)
            box.tap()
            pop.wait_for(state="hidden", timeout=3000)

            # --- mobile fit: a phone capture opens zoomed out, and reopening shows NO
            #     stale/empty popup left over from the previous interaction ---
            box.tap()
            pop.wait_for(state="visible", timeout=3000)
            page.evaluate("() => Alpine.store('lb').close()")
            _open(page, 0.35)
            assert abs(page.evaluate("() => Alpine.store('lb').zoom") - 0.35) < 1e-6   # less zoomed
            page.wait_for_timeout(150)
            assert not pop.is_visible()                       # no leftover popup on reopen
        finally:
            ctx.close()
            browser.close()
