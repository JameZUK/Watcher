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
    solve_captcha_fn: Callable[..., Awaitable[list[int] | None]] | None = None,
) -> None:
    """The agent loop: observe → decide (action_fn) → act, pausing for a code.
    persist_fn(state) stores the captured session on success.

    solve_captcha_fn(target, rows, cols, png) -> cells lets the agent attempt a
    reCAPTCHA image challenge before treating it as a dead end."""
    available = [k for k, v in session.secrets.items() if v]
    history: list[str] = []
    code_pending = False
    captcha_tries = 0
    close = None
    try:
        close, ctx = await _new_context(engine, proxy)
        page = await ctx.new_page()
        session.log.append(f"Opening {session.url}")
        await page.goto(session.url, wait_until=wait_until, timeout=45000)

        last_sig = None
        stuck = 0
        none_retry = 0
        empty_retry = 0
        code_entered = False
        for _ in range(MAX_STEPS):
            # Follow popups: federated logins (Glassdoor → Indeed, "Continue with
            # Google/Apple") open the real credential form in a NEW window. If we
            # kept observing the opener it would look frozen forever.
            page = _active_page(ctx, page)
            await _settle(page)

            # A reCAPTCHA image challenge can't be driven via the element list —
            # solve it (best effort) with the vision model before observing.
            if solve_captcha_fn and captcha_tries < 4 and await _has_recaptcha_challenge(page):
                captcha_tries += 1
                session.log.append("Solving image captcha…")
                def _set_shot(b):
                    session.screenshot = b
                solved = await solve_recaptcha(page, solve_captcha_fn,
                                               log_cb=session.log.append, shot_cb=_set_shot)
                session.log.append("Captcha passed — continuing." if solved
                                   else "Couldn't pass the captcha this time.")
                await _settle(page)
                continue

            try:
                elements = await page.evaluate(_OBSERVE_JS)
            except Exception:
                elements = []
            # A page mid-render shows nothing yet — give it a couple more beats
            # before asking the model to decide (or wrongly declaring a dead end).
            if not elements and empty_retry < 3:
                empty_retry += 1
                await page.wait_for_timeout(1200)
                continue
            empty_retry = 0
            try:
                session.screenshot = await page.screenshot(full_page=False, type="png")
            except Exception:
                pass
            labels = {e.get("idx"): (e.get("label") or e.get("placeholder") or e.get("aria")
                                     or e.get("name") or f"element {e.get('idx')}") for e in elements}
            elem_by_idx = {e.get("idx"): e for e in elements}

            # Success after the one-time code: the OAuth popup closes and we land
            # back on the signed-in site with no credential fields left — capture
            # the session rather than waiting for the model to notice.
            try:
                _u = (page.url or "").lower()
            except Exception:
                _u = ""
            if (code_entered and elements
                    and not any((e.get("type") in ("password", "email")) for e in elements)
                    and not any(m in _u for m in ("/auth", "login", "signin", "sign-in"))):
                break

            # Stuck detection: if the interactive elements (incl. their filled-ness)
            # don't change across steps, our actions aren't doing anything — almost
            # always an anti-bot / bot-detection wall on the login page.
            try:
                _cur_url = page.url or ""
            except Exception:
                _cur_url = ""
            sig = (_cur_url, tuple((e.get("tag"), e.get("type"), e.get("label"), e.get("filled"))
                                   for e in elements))
            stuck = stuck + 1 if (sig == last_sig and history) else 0
            last_sig = sig
            if stuck >= 2 and await _is_bot_wall(page):
                session.status, session.error = "error", _WALL_MSG
                return
            if stuck >= 3:  # input no longer changes the page — captcha / dead end
                session.status, session.error = "error", _STUCK_MSG
                return

            action = await action_fn(elements=elements, screenshot_png=session.screenshot,
                                     available=available, history=history, code_pending=code_pending)
            code_pending = False
            if not action:
                if none_retry < 1:            # a transient hiccup — try once more
                    none_retry += 1
                    await page.wait_for_timeout(800)
                    continue
                session.status = "error"
                session.error = (_WALL_MSG if await _is_bot_wall(page)
                                 else "The assistant couldn't work out the next step on this page.")
                return
            none_retry = 0
            a = action.get("action")
            _raw_idx = action.get("index", -1)
            idx = int(_raw_idx) if _raw_idx is not None else -1   # NB: index 0 is valid
            label = labels.get(idx, f"element {idx}")
            sel = f'[data-ai-idx="{idx}"]'
            history.append(f"{a}{('#' + str(idx)) if idx >= 0 else ''}"
                           f"{(' ' + action['secret']) if action.get('secret') else ''}")

            if a == "done":
                break
            if a == "fail":
                session.status = "error"
                session.error = (action.get("reason") or "Login could not proceed.")
                if await _is_bot_wall(page):
                    session.error = _WALL_MSG
                return
            if a == "type":
                val = session.secrets.get(action.get("secret") or "", "")
                session.log.append(f"Enter {action.get('secret') or 'value'} into “{label[:40]}”")
                if val and idx >= 0:
                    try:
                        await page.fill(sel, val)
                    except Exception:
                        pass
            elif a == "type_text":
                # Guard rail: the model sometimes free-types (and even invents) an
                # email into a credential field. If the target clearly is one, fill
                # the real stored secret instead of whatever text it produced.
                cred = _credential_for_field(elem_by_idx.get(idx, {}), session.secrets)
                if cred:
                    session.log.append(f"Enter {cred} into “{label[:40]}”")
                    val = session.secrets.get(cred, "")
                    if val and idx >= 0:
                        try:
                            await page.fill(sel, val)
                        except Exception:
                            pass
                else:
                    session.log.append(f"Enter a value into “{label[:40]}”")
                    try:
                        await page.fill(sel, action.get("text") or "")
                    except Exception:
                        pass
            elif a == "click":
                session.log.append(f"Click “{label[:50]}”")
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
                code_entered = True
            await _safe_sleep(page, 900)
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


_WALL_MSG = (
    "The site is showing an anti-bot / bot-detection check on its login page, so "
    "automated login can't get through it. Log in manually in your own browser "
    "and paste a session cookie instead (Session cookies, below)."
)

_STUCK_MSG = (
    "The login page stopped responding to the agent — almost always a captcha or "
    "'press & hold' anti-bot check, which can't be automated. Log in manually in "
    "your own browser and paste a session cookie instead (Session cookies, below)."
)


def _credential_for_field(el: dict, secrets: dict) -> str | None:
    """If this element is clearly a username/email or password field and we hold
    that secret, return which one — so we never free-type (or invent) credentials."""
    t = (el.get("type") or "").lower()
    blob = " ".join(str(el.get(k, "")) for k in
                    ("name", "id", "autocomplete", "placeholder", "aria", "label")).lower()
    if secrets.get("password") and ("password" in t or "password" in blob):
        return "password"
    if secrets.get("username") and (t == "email" or any(
            w in blob for w in ("email", "e-mail", "username", "user-name",
                                "userid", "user_id", "login", "__email"))):
        return "username"
    return None


async def _is_bot_wall(page) -> bool:
    """Heuristic: are we on a hard anti-bot interstitial (a Cloudflare/PerimeterX
    challenge), as opposed to a normal login form?

    Deliberately body-text based: Glassdoor's *working* login wall has a
    ``reason=bot-detection`` URL param yet is perfectly clickable, so URL matching
    gives false positives. A genuine challenge page shouts it in the body text.
    """
    try:
        txt = (await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').slice(0,3000)")).lower()
    except Exception:
        txt = ""
    if any(m in txt for m in (
            "just a moment", "verify you are human", "verifying you are human",
            "checking your browser", "are you a robot", "unusual traffic",
            "px-captcha", "cf-challenge", "enable javascript and cookies to continue",
            "press & hold", "press and hold", "complete the security check",
            "human verification", "solve this puzzle")):
        return True
    try:  # a captcha widget mounted in an iframe (hCaptcha / reCAPTCHA / PerimeterX)
        return bool(await page.evaluate(
            "() => !!document.querySelector("
            "'iframe[src*=\"hcaptcha\"],iframe[src*=\"recaptcha\"],iframe[src*=\"captcha\"],"
            "iframe[title*=\"captcha\" i],#px-captcha,[id*=\"px-captcha\"]')"))
    except Exception:
        return False


async def _recaptcha_token(page) -> str:
    try:
        return await page.evaluate(
            "() => { const t = document.querySelector('textarea[name=\"g-recaptcha-response\"],"
            "#g-recaptcha-response'); return (t && t.value) || ''; }")
    except Exception:
        return ""


def _recaptcha_challenge_frame(page):
    """The reCAPTCHA image-challenge iframe, if one is currently showing."""
    for fr in page.frames:
        if "/recaptcha/" in (fr.url or "") and "bframe" in (fr.url or ""):
            return fr
    return None


async def _has_recaptcha_challenge(page) -> bool:
    """True only when an image challenge is actually visible (not an idle/invisible
    reCAPTCHA whose bframe merely exists in the DOM)."""
    fr = _recaptcha_challenge_frame(page)
    if fr is None:
        return False
    try:
        d = fr.locator(".rc-imageselect-desc-no-canonical, .rc-imageselect-desc")
        return bool(await d.count()) and await d.first.is_visible()
    except Exception:
        return False


async def _open_recaptcha_if_needed(page) -> None:
    """If only the 'I'm not a robot' checkbox is showing, tick it to trigger the
    challenge (some forms gate the challenge behind the anchor)."""
    for fr in page.frames:
        if "/recaptcha/" in (fr.url or "") and "anchor" in (fr.url or ""):
            try:
                box = fr.locator("#recaptcha-anchor")
                if await box.count() and (await box.get_attribute("aria-checked")) == "false":
                    await box.click(timeout=4000)
                    await page.wait_for_timeout(2000)
            except Exception:
                pass
            return


async def solve_recaptcha(page, solve_fn, log_cb=None, shot_cb=None, *, max_rounds: int = 6) -> bool:
    """Best-effort: drive a reCAPTCHA image challenge with a vision model.

    `solve_fn(target, rows, cols, png) -> list[int]` returns the 1-based cells to
    click. `shot_cb(png)` (optional) receives a live screenshot so the UI can show
    the challenge being solved. Returns True if a response token gets minted (the
    challenge passed). reCAPTCHA Enterprise also behaviour-scores the browser, so
    even correct picks can be rejected and re-challenged — we cap attempts.
    """
    def _log(m):
        if log_cb:
            log_cb(m)

    async def _shot():
        if shot_cb:
            try:
                shot_cb(await page.screenshot(full_page=False, type="png"))
            except Exception:
                pass

    await _open_recaptcha_if_needed(page)
    for _ in range(max_rounds):
        if await _recaptcha_token(page):
            return True
        fr = _recaptcha_challenge_frame(page)
        if fr is None:
            return bool(await _recaptcha_token(page))
        try:
            desc = fr.locator(".rc-imageselect-desc-no-canonical, .rc-imageselect-desc")
            if not await desc.count():
                await page.wait_for_timeout(1500)
                if await _recaptcha_token(page):
                    return True
                continue
            prompt = (await desc.first.inner_text(timeout=4000)).replace("\n", " ").strip()
            # "Select all squares with motorcycles" -> "motorcycles"
            target = prompt.split(" with ", 1)[-1].strip() or prompt
            tiles = fr.locator(".rc-imageselect-tile")
            n = await tiles.count()
            if n == 0:
                await page.wait_for_timeout(1500)
                continue
            side = 4 if n > 9 else 3
            rows = cols = side
            grid = fr.locator(".rc-imageselect-table-44, .rc-imageselect-table-33, "
                              ".rc-imageselect-table-42, table").first
            png = await grid.screenshot(timeout=6000)
        except Exception as exc:
            _log(f"captcha read error: {type(exc).__name__}")
            return False

        await _shot()  # show the challenge in the UI
        cells = await solve_fn(target, rows, cols, png)
        _log(f"Captcha “{target}”: picking {len(cells or [])} of {n} squares")
        if not cells:
            return False
        for c in cells:
            if 1 <= c <= n:
                try:
                    await tiles.nth(c - 1).click(timeout=4000)
                    await page.wait_for_timeout(250)
                except Exception:
                    pass
        await _shot()  # show the picks before verifying
        try:
            await fr.locator("#recaptcha-verify-button").click(timeout=4000)
        except Exception:
            pass
        await page.wait_for_timeout(2800)
    return bool(await _recaptcha_token(page))


def _active_page(ctx, current):
    """The freshest real (non-blank) open page — where the login flow moved to.

    Federated logins open the credential form in a popup; once it completes and
    closes, the freshest remaining page is the (now logged-in) opener.
    """
    try:
        open_pages = [p for p in ctx.pages if not p.is_closed()]
    except Exception:
        return current
    if not open_pages:
        return current
    real = [p for p in open_pages if (p.url and not p.url.startswith("about:"))]
    return (real or open_pages)[-1]


async def _safe_sleep(page, ms: int) -> None:
    """wait_for_timeout that tolerates the page having just closed (e.g. an OAuth
    popup that vanished the instant login completed)."""
    try:
        await page.wait_for_timeout(ms)
    except Exception:
        pass


async def _settle(page) -> None:
    """Let the page settle before observing it. Heavy login walls (and the
    popups they spawn) can take a couple of seconds to render their controls,
    so wait for the network to go quiet before falling back to a fixed pause."""
    try:
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(900)
    except Exception:
        pass


def create_session(monitor_id: int, user_id: int, url: str, secrets: dict) -> LoginSession:
    _gc()
    s = LoginSession(id=uuid.uuid4().hex, monitor_id=monitor_id, user_id=user_id,
                     url=url, secrets=secrets)
    s._code_event = asyncio.Event()
    _SESSIONS[s.id] = s
    return s
