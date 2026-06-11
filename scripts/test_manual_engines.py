"""Verify manual remote control works across rendering engines.

For each engine: open a local page in manual mode, relay a CLICK at the centre
(exercising the viewport→coordinate mapping that differs per engine), relay a
TYPE into a field, then Capture & finish — and assert the click/type took effect
(a cookie is set on click) and the session is captured.
"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import watcher.auth.ai_login as A

PAGE = (b"<!doctype html><html><body style='margin:0'>"
        b"<button id='b' style='position:fixed;inset:10%;font-size:28px' "
        b"onclick=\"document.cookie='clicked=yes;path=/';this.textContent='CLICKED';\">CLICK ME</button>"
        b"<input id='probe' style='position:fixed;bottom:2%;left:10%;width:80%'>"
        b"</body></html>")


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


async def run_engine(engine):
    srv = _server()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    captured = {}

    async def stub(**k):
        return {"action": "fail", "index": -1, "secret": "", "text": "", "reason": "manual"}

    async def persist(state):
        captured["cookies"] = [c.get("name") for c in (state.get("cookies") or [])]

    sess = A.create_session(1, 1, url, {})
    sess.mode = "manual"
    task = asyncio.create_task(A.run_agent(sess, stub, persist, engine=engine))
    try:
        for _ in range(150):                      # wait for the live view
            if sess.screenshot is not None:
                break
            await asyncio.sleep(0.1)
        # click the centre (button spans 10%..90%, so 0.5/0.5 lands on it)
        sess._events.append({"type": "click", "fx": 0.5, "fy": 0.5})
        await asyncio.sleep(1.0)
        # focus the input near the bottom and type
        sess._events.append({"type": "click", "fx": 0.5, "fy": 0.96})
        sess._events.append({"type": "type", "text": "hello"})
        await asyncio.sleep(0.8)
        sess._finish = True
        outcome = "timeout"
        for _ in range(120):
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
    clicked = "clicked" in (captured.get("cookies") or [])
    err = (sess.error or "")
    launch_failed = outcome == "error" and any(
        m in err.lower() for m in ("executable doesn't exist", "missing dependencies",
                                   "host system is missing", "not installed", "playwright install"))
    if launch_failed:
        print(f"[SKIP] engine={engine}: browser not installed in this environment")
        return "skip"
    ok = (outcome == "done" and clicked)
    print(f"[{'PASS' if ok else 'FAIL'}] engine={engine}: outcome={outcome} "
          f"click_registered={clicked} cookies={captured.get('cookies')}")
    if not ok:
        print("        err:", err[:160])
        for l in sess.log:
            print("        -", l)
    return ok


async def main():
    engines = ["chromium", "firefox", "webkit", "camoufox"]
    results = []
    for e in engines:
        try:
            results.append(await run_engine(e))
        except Exception as exc:
            print(f"[FAIL] engine={e}: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
            results.append(False)
    passed = sum(1 for r in results if r is True)
    skipped = sum(1 for r in results if r == "skip")
    tested = len(results) - skipped
    print(f"\n{passed}/{tested} usable engine(s) passed ({skipped} skipped/not installed)")

asyncio.run(main())
