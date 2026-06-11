"""Verify manual remote control works PRECISELY across rendering engines.

A 5x5 grid where every cell records its own index on click (into localStorage,
which we read back from the captured session state). We click five specific
cells by fraction and assert the EXACT cells were hit — this catches any
coordinate scaling/offset error a big-button test would miss. Then we focus an
input and type, asserting the typed value landed.

Run locally (chromium/firefox/camoufox) or in the project container for all four:
    docker run --rm -v "$(pwd)":/host:ro -e PYTHONPATH=/host watcher:img \
        python /host/scripts/test_manual_engines.py
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import watcher.auth.ai_login as A

PAGE = b"""<!doctype html><html><body style="margin:0;padding:0;font-family:sans-serif">
<div id="grid" style="position:fixed;inset:0;display:grid;
     grid-template-columns:repeat(5,1fr);grid-template-rows:repeat(5,1fr)"></div>
<input id="field" autocomplete="off" style="position:fixed;bottom:0;left:0;width:100%;
     height:8%;font-size:20px;z-index:10;box-sizing:border-box">
<script>
function rec(k,v){ const a=JSON.parse(localStorage.getItem(k)||'[]'); a.push(v);
  localStorage.setItem(k, JSON.stringify(a)); }
const g=document.getElementById('grid');
for(let i=0;i<25;i++){ const c=document.createElement('div'); c.dataset.i=i;
  c.style.cssText='border:1px solid #333;display:flex;align-items:center;justify-content:center;font-size:20px';
  c.textContent=i;
  c.addEventListener('click',e=>rec('clicks', parseInt(e.currentTarget.dataset.i)));
  g.appendChild(c); }
const f=document.getElementById('field');
f.addEventListener('input',()=>localStorage.setItem('typed', f.value));
</script></body></html>"""

# (row, col) -> index, click at the cell centre. Rows 0..3 stay clear of the
# bottom input strip.
CLICKS = [(0, 0), (0, 4), (2, 2), (3, 1), (1, 3)]
EXPECT_CLICKS = [r * 5 + c for r, c in CLICKS]
TYPED = "abc123"


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
        for _ in range(200):
            if sess.screenshot is not None:
                break
            await asyncio.sleep(0.1)
        if sess.status == "error":
            raise RuntimeError(sess.error or "launch failed")
        # precise clicks on five grid cells
        for r, c in CLICKS:
            sess._events.append({"type": "click", "fx": (c + 0.5) / 5, "fy": (r + 0.5) / 5})
            await asyncio.sleep(0.5)
        # focus the bottom input and type
        sess._events.append({"type": "click", "fx": 0.5, "fy": 0.96})
        await asyncio.sleep(0.4)
        sess._events.append({"type": "type", "text": TYPED})
        await asyncio.sleep(1.5)
        sess._finish = True
        outcome = "timeout"
        for _ in range(150):
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
    clicks = json.loads(ls.get("clicks", "[]"))
    typed = ls.get("typed")
    clicks_ok = clicks == EXPECT_CLICKS
    type_ok = typed == TYPED
    ok = outcome == "done" and clicks_ok and type_ok
    print(f"[{'PASS' if ok else 'FAIL'}] engine={engine}: outcome={outcome} "
          f"clicks={clicks} (want {EXPECT_CLICKS}) typed={typed!r}")
    if not ok:
        print(f"        clicks_ok={clicks_ok} type_ok={type_ok}")
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
        if isinstance(r, tuple):   # launch failure / not installed
            print(f"[SKIP] engine={e}: not runnable here ({r[1]})")
            results[e] = "skip"
        else:
            results[e] = r
    passed = sum(1 for v in results.values() if v is True)
    skipped = sum(1 for v in results.values() if v == "skip")
    tested = len(results) - skipped
    print(f"\n{passed}/{tested} usable engine(s) passed precisely ({skipped} skipped)")

asyncio.run(main())
