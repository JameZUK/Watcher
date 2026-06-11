"""Thorough local review of the AI-login agent against a faithful federated-login
simulation (Glassdoor -> Indeed popup, social buttons, email step, OTP variants).

The action_fn here emulates the REAL model's decision policy (including how it
reacts to code_pending), so it reproduces the bugs we hit and proves the fixes.
"""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import watcher.auth.ai_login as A

# ---- opener: a "Glassdoor wall" that opens the Indeed popup, and reloads to a
# signed-in state once the session cookie appears (as the real opener does). ----
OPENER = """<!doctype html><html><body>
<div id="wall">
  <button type="submit">Continue with Google</button>
  <button id="go" onclick="window.open('/popup?s=%(s)s','_blank','width=500,height=700')">Continue with Apple or email</button>
</div>
<div id="in" style="display:none">Signed in. Welcome.</div>
<script>
const S = "%(s)s";
function reflect(){
  if (document.cookie.includes('sess=ok')) {  // a valid session => signed in
    document.getElementById('wall').style.display='none';
    document.getElementById('in').style.display='block';
  }
}
reflect();  // on (re)load: a valid session shows signed-in — this is what verify checks
// 'noreload' simulates an opener that does NOT live-refresh while the popup is
// open — so the agent must infer success from the popup closing, not the opener.
if (S !== 'noreload') setInterval(reflect, 300);
</script></body></html>"""

# ---- popup: Indeed-like state machine. Email step (with social buttons), then
# an OTP step whose behaviour depends on the scenario. ----
POPUP = """<!doctype html><html><body>
<div id="email_step">
  <h1>Sign in to Indeed</h1>
  <button type="submit">Continue with Google</button>
  <button type="submit">Continue with Apple</button>
  <input id="__email" type="email" name="__email" placeholder="Email address">
  <button id="email_continue" type="submit">Continue</button>
</div>
<div id="code_step" style="display:none">
  <h1>Enter the code we sent you</h1>
  <div id="code_holder"></div>
  <button id="code_continue" type="submit" style="display:%(code_btn)s">Continue</button>
</div>
<div id="verifying" style="display:none"><h1>Verifying…</h1></div>
<script>
const S = "%(s)s";
function ok(){ document.cookie='sess=ok;path=/'; }
function done(){ ok(); setTimeout(()=>window.close(), 120); }
document.getElementById('email_continue').addEventListener('click', e => {
  e.preventDefault();
  if (!document.getElementById('__email').value) return;
  document.getElementById('email_step').style.display='none';
  document.getElementById('code_step').style.display='block';
  const holder = document.getElementById('code_holder');
  if (S === 'multibox') {
    for (let i=0;i<6;i++){ const b=document.createElement('input');
      b.maxLength=1; b.autocomplete='one-time-code'; b.inputMode='numeric'; holder.appendChild(b); }
    const boxes=[...holder.querySelectorAll('input')]; boxes[0].focus();
    boxes.forEach((b,i)=>b.addEventListener('input',()=>{ if(b.value&&i<5)boxes[i+1].focus();
      if(boxes.map(x=>x.value).join('').length===6) done(); }));
  } else {
    const f=document.createElement('input'); f.id='code'; f.autocomplete='one-time-code';
    f.placeholder='Enter code'; holder.appendChild(f); f.focus();
    if (S === 'enter' || S === 'verify' || S === 'noreload') {
      f.addEventListener('keydown', ev => { if(ev.key==='Enter' && f.value.length>=6){
        if (S==='verify'){ document.getElementById('code_step').style.display='none';
          document.getElementById('verifying').style.display='block'; setTimeout(done, 2600); }
        else done(); } });
    }
    if (S === 'button') {
      document.getElementById('code_continue').addEventListener('click', ev => {
        ev.preventDefault(); if (f.value.length>=6) done(); });
    }
    if (S === 'reject') {
      f.addEventListener('keydown', ev => { if(ev.key==='Enter'){
        // bounce back to the social wall, no cookie (login blocked/rejected)
        document.getElementById('code_step').style.display='none';
        document.getElementById('email_step').style.display='block';
        document.getElementById('__email').value=''; } });
    }
    if (S === 'rejectclose') {
      // The popup closes (looks like success) but NO valid session is set — a
      // silently-rejected login. The verify-on-reload step must catch this.
      f.addEventListener('keydown', ev => { if(ev.key==='Enter'){ window.close(); } });
    }
  }
});
</script></body></html>"""


def make_server(scenario, code_btn):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith('/popup'):
                body = (POPUP % {"s": scenario, "code_btn": code_btn}).encode()
            else:
                body = (OPENER % {"s": scenario}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def lbl(e):
    return (e.get('label') or e.get('placeholder') or e.get('aria') or '').lower()


def model(elements, code_pending):
    """Faithful emulation of ai_login_action's policy."""
    ins = [e for e in elements if e['tag'] == 'input']
    # The real model, told a code is pending, hunts for a code field and fails
    # if there's none — this is the bug we fixed by not setting code_pending.
    if code_pending:
        cf = next((e for e in ins if A._is_code_field(e) and not e['filled']), None)
        if cf:
            return {"action": "await_code", "index": cf['idx'], "secret": "", "text": "", "reason": ""}
        return {"action": "fail", "index": -1, "secret": "", "text": "",
                "reason": "No input fields or buttons visible to enter the code"}
    email = next((e for e in ins if e['type'] == 'email' and not e['filled']), None)
    if email:
        return {"action": "type", "index": email['idx'], "secret": "username", "text": "", "reason": ""}
    pw = next((e for e in ins if e['type'] == 'password' and not e['filled']), None)
    if pw:
        return {"action": "type", "index": pw['idx'], "secret": "password", "text": "", "reason": ""}
    code = next((e for e in ins if A._is_code_field(e) and not e['filled']), None)
    if code:
        return {"action": "await_code", "index": code['idx'], "secret": "", "text": "", "reason": ""}
    # a real (non-social) submit button: Continue/Verify
    cont = next((e for e in elements if e['type'] in ('submit', 'button')
                 and lbl(e) in ('continue', 'verify', 'submit')
                 and 'google' not in lbl(e) and 'apple' not in lbl(e)), None)
    if cont:
        return {"action": "click", "index": cont['idx'], "secret": "", "text": "", "reason": ""}
    social = next((e for e in elements if 'apple or email' in lbl(e)
                   or 'continue with apple' in lbl(e)), None)
    if social:
        return {"action": "click", "index": social['idx'], "secret": "", "text": "", "reason": ""}
    actionable = [e for e in elements if e['tag'] == 'input' or e['type'] in ('submit', 'button')]
    if not actionable:
        return {"action": "done", "index": -1, "secret": "", "text": "", "reason": "signed in"}
    return {"action": "fail", "index": -1, "secret": "", "text": "",
            "reason": "no path: " + ",".join(lbl(e) or e['type'] for e in elements)}


def model_dumb(elements, code_pending):
    """A weak model (like gemini-flash-lite) that grabs an SSO button whenever one
    is present — exactly the misbehaviour that sent a real run down the Google
    path. The agent's social-login guard must steer it back to email regardless."""
    social = next((e for e in elements if A._is_social_login(e)), None)
    if social and not code_pending:
        return {"action": "click", "index": social['idx'], "secret": "", "text": "", "reason": "sso"}
    return model(elements, code_pending)


async def run_scenario(name, scenario, code_btn, expect, model_fn=model):
    srv = make_server(scenario, code_btn)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    captured = {}

    async def stub(*, elements, screenshot_png, available, history, code_pending):
        return model_fn(elements, code_pending)

    async def persist(state):
        captured['cookies'] = [c['name'] for c in (state.get('cookies') or [])]

    A.MAX_STEPS = 16
    sess = A.create_session(1, 1, url, {"username": "user@example.com", "password": "pw12345"})

    async def feed():
        for _ in range(120):
            if sess.status == "need_code":
                sess.submit_code("135790")
                return
            await asyncio.sleep(0.15)
    asyncio.create_task(feed())

    # Dead-ends now hand control to the user (manual mode) instead of hard-failing,
    # so the outcome is one of: done / error / manual-handoff.
    task = asyncio.create_task(A.run_agent(sess, stub, persist, engine="chromium"))
    got = "timeout"
    for _ in range(350):
        if sess.status in ("done", "error"):
            got = sess.status
            break
        if sess.mode == "manual":
            got = "manual"
            break
        await asyncio.sleep(0.2)
    if not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    srv.shutdown()
    ok = (got == expect)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: outcome={got} expect={expect}"
          f" cookies={captured.get('cookies')}")
    if not ok:
        for l in sess.log:
            print("        -", l)
        print("        err:", sess.error, "mode:", sess.mode)
    return ok


async def test_manual_capture():
    """Manual remote control: take over from the start, relay input, then 'Capture
    session & finish' grabs the (valid) session."""
    PAGE = (b"<!doctype html><html><body><div>Signed in. Welcome.</div>"
            b"<input id='probe'><script>document.cookie='sess=ok;path=/';</script></body></html>")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(PAGE)))
            self.end_headers()
            self.wfile.write(PAGE)
    srv = ThreadingHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    captured = {}

    async def stub(**k):
        return {"action": "fail", "index": -1, "secret": "", "text": "", "reason": "n/a"}

    async def persist(state):
        captured['cookies'] = [c['name'] for c in (state.get('cookies') or [])]
    sess = A.create_session(1, 1, url, {"username": "u", "password": "p"})
    sess.mode = "manual"   # user drives from the start
    task = asyncio.create_task(A.run_agent(sess, stub, persist, engine="chromium"))
    for _ in range(100):
        if sess.screenshot is not None:
            break
        await asyncio.sleep(0.1)
    sess._events.append({"type": "type", "text": "hello"})    # exercise the relay
    sess._events.append({"type": "click", "fx": 0.5, "fy": 0.5})
    await asyncio.sleep(0.6)
    sess._finish = True
    got = "timeout"
    for _ in range(100):
        if sess.status in ("done", "error"):
            got = sess.status
            break
        await asyncio.sleep(0.1)
    if not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    srv.shutdown()
    ok = (got == "done" and "sess" in (captured.get('cookies') or []))
    print(f"[{'PASS' if ok else 'FAIL'}] I manual control + finish captures session:"
          f" outcome={got} cookies={captured.get('cookies')}")
    if not ok:
        for l in sess.log:
            print("        -", l)
    return ok


async def main():
    results = []
    # name, scenario, code_btn(css display for the Continue btn), expected status
    results.append(await run_scenario("A single-field Enter auto-submit", "enter", "none", "done"))
    results.append(await run_scenario("B single-field needs Continue click", "button", "inline", "done"))
    results.append(await run_scenario("C multi-box auto-advance", "multibox", "none", "done"))
    results.append(await run_scenario("D transitional 'verifying' then success", "verify", "none", "done"))
    results.append(await run_scenario("E code rejected -> hands off to manual", "reject", "none", "manual"))
    results.append(await run_scenario("F weak model keeps clicking Google/Apple", "enter", "none",
                                      "done", model_fn=model_dumb))
    results.append(await run_scenario("G popup closes but opener doesn't reload", "noreload", "none", "done"))
    results.append(await run_scenario("H popup closes but session invalid -> manual", "rejectclose", "none", "manual"))
    results.append(await test_manual_capture())
    print(f"\n{sum(results)}/{len(results)} scenarios passed")

asyncio.run(main())
