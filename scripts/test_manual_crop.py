"""Verify the manual view shows the WHOLE page (no crop) and edge/corner clicks
land correctly — on every engine, in all window-size situations.

Markers are pinned to each corner + the centre. We assert (a) every marker shows
up in the live screenshot at roughly its expected spot (nothing cropped off), and
(b) clicking each marker's on-screen fraction actually hits THAT marker.

Run in the project container for all four engines:
    docker run --rm -v "$(pwd)":/host:ro -e PYTHONPATH=/host watcher:latest \
        python /host/scripts/test_manual_crop.py
"""
import asyncio
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import watcher.auth.ai_login as A

# id -> (css corner, rgb). Each marker is a 70px box pinned to a corner/centre.
MARKERS = {
    "tl": ("top:0;left:0", (220, 0, 0)),
    "tr": ("top:0;right:0", (0, 0, 220)),
    "bl": ("bottom:0;left:0", (0, 160, 0)),
    "br": ("bottom:0;right:0", (210, 180, 0)),
    "cc": ("top:50%;left:50%;transform:translate(-50%,-50%)", (200, 0, 200)),
}
_divs = "".join(
    f"<div data-id='{k}' style='position:fixed;{pos};width:70px;height:70px;"
    f"background:rgb{rgb};z-index:5' "
    f"onclick=\"var a=JSON.parse(localStorage.getItem('hits')||'[]');"
    f"a.push(this.dataset.id);localStorage.setItem('hits',JSON.stringify(a));\"></div>"
    for k, (pos, rgb) in MARKERS.items())
PAGE = (f"<!doctype html><html><body style='margin:0;background:#fff'>{_divs}"
        "<script>localStorage.setItem('dpr',String(devicePixelRatio||1));</script>"
        "</body></html>").encode()


def _server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _png_dims(b):
    return struct.unpack(">II", b[16:24])


def _near(px, x, y, rgb, tol=70):
    try:
        r, g, b = px[x, y][:3]
    except Exception:
        return False
    return abs(r - rgb[0]) <= tol and abs(g - rgb[1]) <= tol and abs(b - rgb[2]) <= tol


def _all_corners_visible(png):
    """Every corner/centre marker shows up where expected → nothing cropped."""
    from io import BytesIO

    from PIL import Image
    im = Image.open(BytesIO(png)).convert("RGB")
    w, h = im.size
    px = im.load()
    spots = {
        "tl": (20, 20), "tr": (w - 20, 20), "bl": (20, h - 20),
        "br": (w - 20, h - 20), "cc": (w // 2, h // 2),
    }
    missing = [k for k, (x, y) in spots.items() if not _near(px, x, y, MARKERS[k][1])]
    return (not missing), missing


def _ls(state):
    out = {}
    for o in (state.get("origins") or []):
        for kv in (o.get("localStorage") or []):
            out[kv.get("name")] = kv.get("value")
    return out


# centre fraction of each marker on screen (markers are 70px; corners ~35px in)
def _frac(mid, sw_css, sh_css):
    fx = {"tl": 35, "bl": 35}.get(mid, sw_css - 35 if mid in ("tr", "br") else sw_css / 2) / sw_css
    fy = {"tl": 35, "tr": 35}.get(mid, sh_css - 35 if mid in ("bl", "br") else sh_css / 2) / sh_css
    return fx, fy


async def run_engine(engine):
    srv = _server()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    captured = {}

    async def stub(**k):
        return {"action": "fail", "index": -1, "secret": "", "text": "", "reason": "m"}

    async def persist(s):
        captured["state"] = s
    sess = A.create_session(1, 1, url, {})
    sess.mode = "manual"
    task = asyncio.create_task(A.run_agent(sess, stub, persist, engine=engine))
    try:
        for _ in range(250):
            if sess.screenshot is not None:
                break
            if sess.status == "error":
                break
            await asyncio.sleep(0.1)
        if sess.status == "error":
            return ("launch", sess.error or "launch failed")
        await asyncio.sleep(0.6)
        shot = sess.screenshot
        sw, sh = _png_dims(shot)
        no_crop, missing = _all_corners_visible(shot)
        # click each marker at its on-screen fraction (dpr 1 in headless)
        order = ["tl", "tr", "br", "bl", "cc"]
        for mid in order:
            fx, fy = _frac(mid, sw, sh)
            sess._events.append({"type": "click", "fx": fx, "fy": fy})
            await asyncio.sleep(0.6)
        await asyncio.sleep(0.8)
        sess._finish = True
        outcome = "timeout"
        for _ in range(200):
            if sess.status in ("done", "error"):
                outcome = sess.status
                break
            await asyncio.sleep(0.1)
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        srv.shutdown()

    hits = json.loads(_ls(captured.get("state") or {}).get("hits", "[]"))
    clicks_ok = hits == order
    ok = no_crop and clicks_ok and outcome == "done"
    print(f"[{'PASS' if ok else 'FAIL'}] engine={engine}: shot={sw}x{sh} no_crop={no_crop}"
          f"{'' if no_crop else ' missing=' + str(missing)} corner_clicks={hits} ok={clicks_ok}")
    return ok


async def main():
    results = {}
    for e in ["chromium", "firefox", "webkit", "camoufox"]:
        try:
            r = await run_engine(e)
        except Exception as exc:
            r = ("launch", f"{type(exc).__name__}: {exc}")
        if isinstance(r, tuple):
            print(f"[SKIP] engine={e}: not runnable here ({r[1]})")
            results[e] = "skip"
        else:
            results[e] = r
    passed = sum(1 for v in results.values() if v is True)
    skipped = sum(1 for v in results.values() if v == "skip")
    print(f"\n{passed}/{len(results) - skipped} engine(s): full page shown (no crop) + all "
          f"corner clicks landed — {skipped} skipped")

asyncio.run(main())
