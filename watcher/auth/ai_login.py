"""Interactive, AI-driven login agent.

Drives a real browser to log into a site with stored credentials, deciding each
step with the model — which sees the page's element list + a screenshot but never
the secret values (we fill those for it). When a one-time code is needed it pauses
and asks the user via the web UI ("live prompt"), then resumes. On success the
session cookies are captured and persisted to the monitor so scheduled checks ride
the session.

Login sessions are held in-memory in a single uvicorn worker — fine for a one-off
interactive setup (a server restart just means starting the login over).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger("watcher.ailogin")

MAX_STEPS = 14
CODE_WAIT_SECONDS = 300
SESSION_TTL = 900

# Annotate + list the page's interactive elements so the model can reference them
# by index, and so we can act on them via [data-ai-idx="N"].
_OBSERVE_JS = r"""() => {
  // Clear annotations from previous observations so indices stay unique to the
  // CURRENT view (a stale idx could mis-target a now-hidden element).
  document.querySelectorAll('[data-ai-idx]').forEach(e => e.removeAttribute('data-ai-idx'));
  const out = []; let i = 0;
  const vis = (el) => { let cs; try { cs = getComputedStyle(el); } catch(e){ return false; }
    if (cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity||'1')===0) return false;
    const r = el.getBoundingClientRect();
    return r.width>=6 && r.height>=6 && r.bottom>0 && r.top<innerHeight*4; };
  for (const el of document.querySelectorAll('input,button,a[href],[role="button"],select,textarea')) {
    if ((el.type||'').toLowerCase()==='hidden') continue;
    if (!vis(el)) continue;
    el.setAttribute('data-ai-idx', i);
    out.push({ idx:i, tag:el.tagName.toLowerCase(), type:(el.type||'').toLowerCase(),
      name:el.name||'', id:el.id||'', placeholder:el.placeholder||'',
      aria:((el.getAttribute('aria-label')||'')+'').slice(0,80),
      label:((el.innerText||el.value||'')+'').trim().slice(0,80),
      filled: !!(el.value && (''+el.value).trim()),
      autocomplete:(el.getAttribute('autocomplete')||'') });
    i++; if (i >= 60) break;
  }
  return out;
}"""


@dataclass
class LoginSession:
    id: str
    monitor_id: int
    user_id: int
    url: str
    secrets: dict                       # {'username': ..., 'password': ...}
    status: str = "running"             # running | need_code | done | error
    prompt: str = ""
    log: list = field(default_factory=list)
    error: str = ""
    screenshot: bytes | None = None
    result_state: dict | None = None
    created_at: float = field(default_factory=time.monotonic)
    _code: str | None = None
    _code_event: asyncio.Event | None = None
    _task: object = None          # strong ref so the agent task isn't GC'd

    def submit_code(self, code: str) -> None:
        self._code = (code or "").strip()
        if self._code_event is None:
            self._code_event = asyncio.Event()
        self._code_event.set()

    async def _wait_for_code(self) -> str | None:
        if self._code_event is None:
            self._code_event = asyncio.Event()
        # If the code was already submitted (status-set/submit race), take it now.
        if self._code is None:
            try:
                await asyncio.wait_for(self._code_event.wait(), timeout=CODE_WAIT_SECONDS)
            except asyncio.TimeoutError:
                return None
        code, self._code = self._code, None
        self._code_event = asyncio.Event()   # reset for any subsequent code
        return code


_SESSIONS: dict[str, LoginSession] = {}


def _gc() -> None:
    now = time.monotonic()
    for sid in [s for s, v in _SESSIONS.items() if now - v.created_at > SESSION_TTL]:
        _SESSIONS.pop(sid, None)


def get_session(sid: str, user_id: int) -> LoginSession | None:
    _gc()
    s = _SESSIONS.get(sid)
    return s if (s and s.user_id == user_id) else None


async def _new_context(engine: str, proxy: str | None):
    """Yield (closer, context) for the requested engine. Camoufox for stealth
    logins (Glassdoor/Indeed), else a plain Playwright browser."""
    kwargs: dict = {"headless": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if engine == "camoufox":
        from camoufox.async_api import AsyncCamoufox
        cm = AsyncCamoufox(humanize=True, **kwargs)
        browser = await cm.__aenter__()
        ctx = await browser.new_context()
        ctx.set_default_timeout(15000)

        async def close():
            try:
                await cm.__aexit__(None, None, None)
            except Exception:
                pass
        return close, ctx
    from playwright.async_api import async_playwright
    pw = await async_playwright().__aenter__()
    btype = getattr(pw, engine if engine in ("chromium", "firefox", "webkit") else "chromium")
    browser = await btype.launch(**kwargs)
    ctx = await browser.new_context()
    ctx.set_default_timeout(15000)

    async def close():
        for c in (browser.close, pw.stop):
            try:
                await c()
            except Exception:
                pass
    return close, ctx


async def run_agent(
    session: LoginSession,
    action_fn: Callable[..., Awaitable[dict | None]],
    persist_fn: Callable[[dict], Awaitable[None]],
    *,
    engine: str = "chromium",
    proxy: str | None = None,
    wait_until: str = "domcontentloaded",
) -> None:
    """The agent loop: observe → decide (action_fn) → act, pausing for a code.
    persist_fn(state) stores the captured session on success."""
    available = [k for k, v in session.secrets.items() if v]
    history: list[str] = []
    code_pending = False
    close = None
    try:
        close, ctx = await _new_context(engine, proxy)
        page = await ctx.new_page()
        session.log.append(f"Opening {session.url}")
        await page.goto(session.url, wait_until=wait_until, timeout=45000)

        for _ in range(MAX_STEPS):
            await _settle(page)
            try:
                elements = await page.evaluate(_OBSERVE_JS)
            except Exception:
                elements = []
            try:
                session.screenshot = await page.screenshot(full_page=False, type="png")
            except Exception:
                pass

            action = await action_fn(elements=elements, screenshot_png=session.screenshot,
                                     available=available, history=history, code_pending=code_pending)
            code_pending = False
            if not action:
                session.status, session.error = "error", "The assistant couldn't decide a next step."
                return
            a = action.get("action")
            _raw_idx = action.get("index", -1)
            idx = int(_raw_idx) if _raw_idx is not None else -1   # NB: index 0 is valid
            sel = f'[data-ai-idx="{idx}"]'
            history.append(f"{a}{('#' + str(idx)) if idx >= 0 else ''}"
                           f"{(' ' + action['secret']) if action.get('secret') else ''}")

            if a == "done":
                break
            if a == "fail":
                session.status = "error"
                session.error = action.get("reason") or "Login could not proceed."
                return
            if a == "type":
                val = session.secrets.get(action.get("secret") or "", "")
                session.log.append(f"Enter {action.get('secret') or 'value'}")
                if val and idx >= 0:
                    try:
                        await page.fill(sel, val)
                    except Exception:
                        pass
            elif a == "type_text":
                session.log.append("Enter a value")
                try:
                    await page.fill(sel, action.get("text") or "")
                except Exception:
                    pass
            elif a == "click":
                session.log.append(f"Click “{_elem_label(action, idx)}”")
                try:
                    await page.click(sel, timeout=8000)
                except Exception:
                    pass
            elif a == "await_code":
                session.prompt = ("Enter the one-time code the site just sent you "
                                  "(email / SMS / authenticator).")
                session.status = "need_code"
                session.log.append("Waiting for your one-time code…")
                code = await session._wait_for_code()
                if code is None:
                    session.status, session.error = "error", "Timed out waiting for the code."
                    return
                session.status = "running"
                if idx >= 0:
                    try:
                        await page.fill(sel, code)
                    except Exception:
                        pass
                code_pending = True
            await page.wait_for_timeout(900)
        else:
            session.status, session.error = "error", "Gave up after too many steps."
            return

        # Success — capture + persist the session cookies.
        state = await ctx.storage_state()
        session.result_state = state
        await persist_fn(state)
        session.status = "done"
        session.log.append("Logged in — session saved.")
    except Exception as exc:  # noqa: BLE001
        log.warning("ai-login agent error: %s", exc)
        if session.status not in ("done",):
            session.status, session.error = "error", f"{type(exc).__name__}: {exc}"
    finally:
        if close:
            await close()


def _elem_label(action: dict, idx: int) -> str:
    return action.get("reason") or f"element {idx}"


async def _settle(page) -> None:
    """Let the page settle a moment before observing it."""
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    try:
        await page.wait_for_timeout(700)
    except Exception:
        pass


def create_session(monitor_id: int, user_id: int, url: str, secrets: dict) -> LoginSession:
    _gc()
    s = LoginSession(id=uuid.uuid4().hex, monitor_id=monitor_id, user_id=user_id,
                     url=url, secrets=secrets)
    s._code_event = asyncio.Event()
    _SESSIONS[s.id] = s
    return s
