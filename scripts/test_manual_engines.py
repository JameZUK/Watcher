"""Verify manual remote control maps clicks CORRECTLY (screenshot-driven) on every
rendering engine.

The earlier version computed click fractions the same way the backend scaled
them, so it was self-consistent and missed a real bug: under Camoufox the live
screenshot is a CROP of a wider page, so scaling clicks by innerWidth pushed every
click far to the right. This version is the honest test: it reads the ACTUAL
screenshot the user would see, clicks at a fraction OF THAT SCREENSHOT, and
asserts the click landed at the corresponding CSS pixel (fraction × screenshot CSS
size). It also checks typing.

Run locally (chromium/firefox/camoufox) or in the project container for all four:
    docker run --rm -v "$(pwd)":/host:ro -e PYTHONPATH=/host watcher:img \
        python /host/scripts/test_manual_engines.py
"""
import asyncio
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import watcher.auth.ai_login as A

PAGE = b"""<!doctype html><html><body style="margin:0;padding:0">
<div id="c" style="position:fixed;inset:0" onclick="
  var a=JSON.parse(localStorage.getItem('hits')||'[]');
  a.push(Math.round(event.clientX)+','+Math.round(event.clientY));
  localStorage.setItem('hits',JSON.stringify(a));"></div>
<input id="f" autocomplete="off" style="position:fixed;top:0;left:0;width:260px;height:38px;
  z-index:10;font-size:18px;box-sizing:border-box" oninput="localStorage.setItem('typed',this.value)">
<script>localStorage.setItem('dpr', String(window.devicePixelRatio||1));</script>
</body></html>"""

# fractions of the SCREENSHOT to click (avoid the top-left input strip)
FRACTIONS = [(0.2, 0.25), (0.5, 0.4), (0.78, 0.3), (0.35, 0.65), (0.6, 0.55)]
TYPED = "abc123"
TOL = 6  # px tolerance (human-move settles on target; allow rounding/jitter)


def _server():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _localstorage(state):
    out = {}
    for o in (state.get("origins") or []):
        for kv in (o.get("localStorage") or []):
            out[kv.get("name")] = kv.get("value")
    return out


def _png_dims(b):
    return struct.unpack(">II", b[16:24])


async def run_engine(engine):
    srv = _server()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    captured = {}

    async def stub(**k):
        return {"action": "fail", "index": -1, "secret": "", "text": "", "reason": "manual"}

    async def persist(state):
        captured["state"] = state

    sess = A.create_session(1, 1, url, {})
    sess.mode = "manual"
    task = asyncio.create_task(A.run_agent(sess, stub, persist, engine=engine))
    try:
        for _ in range(250):
            if sess.screenshot is not None:
                break
            await asyncio.sleep(0.1)
        if sess.status == "error":
            raise RuntimeError(sess.error or "launch failed")
        sw_px, sh_px = _png_dims(sess.screenshot)        # the screenshot the user sees
        captured["shot"] = (sw_px, sh_px)
        for fx, fy in FRACTIONS:
            sess._events.append({"type": "click", "fx": fx, "fy": fy})
            await asyncio.sleep(0.6)
        # focus the input (its centre on screen) and type
        sess._events.append({"type": "click", "fx": 130 / sw_px, "fy": 19 / sh_px})
        await asyncio.sleep(0.5)
        sess._events.append({"type": "type", "text": TYPED})
        await asyncio.sleep(1.5)
        sess._finish = True
        outcome = "timeout"
        for _ in range(200):
            if sess.status in ("done", "error"):
                outcome = sess.status
                break
            await asyncio.sleep(0.1)
    except Exception as exc:
        srv.shutdown()
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        return ("launch", str(exc).splitlines()[0][:80])
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        srv.shutdown()

    ls = _localstorage(captured.get("state") or {})
    dpr = float(ls.get("dpr", "1") or 1)
    sw_px, sh_px = captured["shot"]
    sw_css, sh_css = sw_px / dpr, sh_px / dpr
    hits = [tuple(map(int, s.split(","))) for s in json.loads(ls.get("hits", "[]"))]
    typed = ls.get("typed")

    detail = []
    clicks_ok = len(hits) == len(FRACTIONS)
    for (fx, fy), hit in zip(FRACTIONS, hits):
        ex, ey = fx * sw_css, fy * sh_css
        dx, dy = abs(hit[0] - ex), abs(hit[1] - ey)
        ok = dx <= TOL and dy <= TOL
        clicks_ok = clicks_ok and ok
        detail.append(f"f({fx},{fy})->want({ex:.0f},{ey:.0f}) got{hit} d=({dx:.0f},{dy:.0f}){'' if ok else ' X'}")
    type_ok = typed == TYPED
    ok = outcome == "done" and clicks_ok and type_ok
    print(f"[{'PASS' if ok else 'FAIL'}] engine={engine}: outcome={outcome} "
          f"shot={sw_px}x{sh_px} dpr={dpr} clicks_ok={clicks_ok} typed={typed!r}")
    if not ok:
        for d in detail:
            print("        ", d)
        for l in sess.log:
            print("        -", l)
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
    print(f"\n{passed}/{len(results) - skipped} usable engine(s) mapped clicks correctly "
          f"({skipped} skipped)")

asyncio.run(main())
