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
import math
import random
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..config import settings
from .login_flows import merge_storage_state

log = logging.getLogger("watcher.ailogin")


def _dlog(msg: str, *args) -> None:
    """Verbose per-event login tracing (clicks, navigations, replaced sessions).
    Silent unless WATCHER_AI_LOGIN_DEBUG=true — it's one line per click."""
    if settings.ai_login_debug:
        log.info(msg, *args)

MAX_STEPS = 14
CODE_WAIT_SECONDS = 300
SESSION_TTL = 900
# Cap concurrent live login browsers per user (each is a real headless browser =
# significant RAM + a process). Bounds resource exhaustion, especially via the
# credential-free manual mode. The oldest over the cap are torn down on a new start.
MAX_SESSIONS_PER_USER = 3

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
    mode: str = "ai"              # "ai" (agent drives) | "manual" (user drives)
    _code: str | None = None
    _code_event: asyncio.Event | None = None
    _task: object = None          # strong ref so the agent task isn't GC'd
    _interim_state: dict | None = None   # cookies snapshotted while the SSO popup
    #                                      was still open (e.g. indeed.com), merged
    #                                      into the final capture
    _events: list = field(default_factory=list)   # queued manual input events
    _finish: bool = False         # user asked to capture the session and finish
    _mouse: tuple = None          # last cursor position (for human-like movement)
    _native_human: bool = False   # engine humanises input itself (Camoufox)
    _shot_needs_stop: bool = False  # this page never stops loading; halt before shot
    _shot_url: str = ""           # url the above was decided for
    _shot_url_since: float = 0.0  # when we first saw _shot_url (grace before halting)
    _unattended: bool = False     # auto re-login: no human, fail instead of handoff

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


def engine_error_message(exc: object) -> str | None:
    """Turn a raw browser-launch failure (e.g. Playwright's 36-line 'missing
    libraries' wall for WebKit on a host without its system deps) into one clear
    line. Returns None if the error isn't a recognised launch problem."""
    s = str(exc).lower()
    if any(m in s for m in ("missing dependencies", "host system is missing",
                            "missing libraries", "executable doesn't exist",
                            "playwright install")):
        return ("This rendering engine isn't available in the current environment "
                "(its system libraries aren't installed). Use Chromium, Firefox or "
                "Camoufox here — or run Watcher in its Docker image, which bundles "
                "every browser. (WebKit needs deps that this host is missing.)")
    return None


_SESSIONS: dict[str, LoginSession] = {}


def _gc() -> None:
    now = time.monotonic()
    for sid in [s for s, v in _SESSIONS.items() if now - v.created_at > SESSION_TTL]:
        _SESSIONS.pop(sid, None)


def get_session(sid: str, user_id: int) -> LoginSession | None:
    _gc()
    s = _SESSIONS.get(sid)
    return s if (s and s.user_id == user_id) else None


# A fixed login viewport so the live screenshot ALWAYS shows the whole interactive
# width — without this, Camoufox renders wider than its 1280px screenshot and the
# right of the page (e.g. a login modal) is cropped off the manual view.
_LOGIN_VIEWPORT = {"width": 1280, "height": 800}


async def _new_context(engine: str, proxy: str | None):
    """Yield (closer, context) for the requested engine. Camoufox for stealth
    logins (Glassdoor/Indeed), else a plain Playwright browser."""
    kwargs: dict = {"headless": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if engine == "camoufox":
        from camoufox.async_api import AsyncCamoufox
        # window=() pins Camoufox's window so innerWidth matches the screenshot
        # (it otherwise picks a random, wider fingerprint size → cropped view).
        # humanize=<float> caps cursor-move duration: the default (~1.9s/move) made
        # manual clicks lag and the queue back up; 0.4 keeps motion human but snappy.
        cm = AsyncCamoufox(humanize=0.4,
                           window=(_LOGIN_VIEWPORT["width"], _LOGIN_VIEWPORT["height"]),
                           **kwargs)
        browser = await cm.__aenter__()
        ctx = await browser.new_context(viewport=dict(_LOGIN_VIEWPORT))
        ctx.set_default_timeout(15000)
        # SSRF guard: this browser can be DRIVEN by the user (manual remote control),
        # so block any navigation/request to an internal host at the network layer.
        from ..engines._common import install_ssrf_guard
        await install_ssrf_guard(ctx)

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
    ctx = await browser.new_context(viewport=dict(_LOGIN_VIEWPORT))
    ctx.set_default_timeout(15000)
    # SSRF guard (see the Camoufox branch): the user can drive this browser, so
    # block requests/navigations to internal hosts at the network layer.
    from ..engines._common import install_ssrf_guard
    await install_ssrf_guard(ctx)

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
    unattended: bool = False,
) -> None:
    """The agent loop: observe → decide (action_fn) → act, pausing for a code.
    persist_fn(state) stores the captured session on success.

    solve_captcha_fn(target, rows, cols, png) -> cells lets the agent attempt a
    reCAPTCHA image challenge before treating it as a dead end.

    unattended=True is for automatic re-login (no human watching): instead of
    handing off to manual control or waiting for a one-time code, the agent fails
    cleanly (status='error') the moment it would need a person — so a credential
    login self-heals but a captcha/OTP login gives up immediately."""
    available = [k for k, v in session.secrets.items() if v]
    session._unattended = unattended
    session._native_human = (engine == "camoufox")   # Camoufox humanises input itself
    history: list[str] = []
    code_pending = False
    captcha_tries = 0
    code_entered = False
    post_code_grace = 0
    wall_after_code = 0
    popup_seen = False
    close = None
    ctx = None
    try:
        close, ctx = await _new_context(engine, proxy)
        page = await ctx.new_page()
        session.log.append(f"Opening {session.url}")
        # Navigate resiliently: return as soon as the server responds ("commit")
        # and NEVER hard-fail on a slow/blocked load — otherwise the user sees a
        # blank modal for 45s and then an error (e.g. Glassdoor's Cloudflare wall
        # on chromium never satisfies a 'load' wait). The loop below streams the
        # page as it renders, and waits for it to settle for the AI path.
        try:
            # domcontentloaded fires when the DOM is parsed — it does NOT wait for
            # images/sub-resources/'load', so a slow tracker can't hang us, and the
            # screenshot has real content to capture.
            await page.goto(session.url, wait_until="domcontentloaded", timeout=30000)
        except Exception as exc:
            session.log.append(f"(page still loading — {type(exc).__name__})")
        await _safe_sleep(page, 500)
        await _shot(session, page)          # show something immediately

        last_sig = None
        stuck = 0
        none_retry = 0
        empty_retry = 0
        ai_steps = 0
        while True:
            # Unattended re-login has no human to take over, so a handoff is a hard
            # stop: _handoff() sets status='error' (keeping mode='ai'); bail here.
            if unattended and session.status == "error":
                return
            if _expired(session):
                if session.status != "done":
                    session.status, session.error = "error", "Login timed out."
                return
            # Follow popups: federated logins (Glassdoor → Indeed, "Continue with
            # Google/Apple") open the real credential form in a NEW window. If we
            # kept observing the opener it would look frozen forever.
            page = _active_page(ctx, page)

            # Capture-and-finish can be requested at any time (incl. manual mode).
            if session._finish:
                # Apply any input the user queued just before pressing finish, so
                # a last click/keystroke isn't dropped.
                drain = 0
                while session._events and drain < 60:
                    await _manual_step(session, page)
                    drain += 1
                await _finish_capture(session, ctx, persist_fn)
                return
            # Manual remote control: while the user drives, relay their input and
            # stream screenshots — don't run the AI agent.
            if session.mode == "manual":
                await _manual_step(session, page)
                continue
            # AI is driving. Cap automatic steps, then hand off to the user.
            if ai_steps >= MAX_STEPS:
                _handoff(session, "the assistant ran out of automatic steps")
                continue
            ai_steps += 1

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
                await _safe_sleep(page, 1200)
                continue
            empty_retry = 0
            await _shot(session, page)
            labels = {e.get("idx"): (e.get("label") or e.get("placeholder") or e.get("aria")
                                     or e.get("name") or f"element {e.get('idx')}") for e in elements}
            elem_by_idx = {e.get("idx"): e for e in elements}

            # Track whether a federated-login popup ever opened — its CLOSING is
            # the clearest signal that login completed (a rejection keeps the popup
            # open showing an error/the wall).
            try:
                _n_open = len([p for p in ctx.pages if not p.is_closed()])
            except Exception:
                _n_open = 1
            if _n_open > 1:
                popup_seen = True
                # Keep the freshest snapshot of the SSO popup's cookies while it's
                # open (merge so we never lose an earlier domain's cookies).
                if code_entered:
                    try:
                        snap = await ctx.storage_state()
                        session._interim_state = merge_storage_state(session._interim_state, snap)
                    except Exception:
                        pass

            # Post-code state handling: signed in (success), bounced back to the
            # social-login wall (rejected), or mid-transition (wait it out).
            try:
                _u = (page.url or "").lower()
            except Exception:
                _u = ""
            if code_entered and elements:
                _on_auth = any(m in _u for m in ("/auth", "login", "signin", "sign-in"))
                _has_cred = any(e.get("type") in ("password", "email") for e in elements)
                _has_code = any(_is_code_field(e) for e in elements)
                _social_wall = any(
                    ("continue with apple" in (e.get("label") or "").lower())
                    or ("continue with google" in (e.get("label") or "").lower())
                    or ("apple or email" in (e.get("label") or "").lower())
                    for e in elements)
                if popup_seen:
                    # Popup-based federated login: the popup closing IS the success.
                    if _n_open <= 1:
                        if await _finalize_success(session, ctx, persist_fn):
                            return
                        _handoff(session, "the login didn't fully complete")
                        continue
                    if _social_wall:   # popup still open, back at the wall → rejected
                        wall_after_code += 1
                        if wall_after_code >= 2:
                            _handoff(session, "the login was rejected after the code")
                            continue
                else:
                    # Direct (same-page) login: judge by the UI — nothing login-ish
                    # left means we're in.
                    if not _has_cred and not _has_code and not _on_auth and not _social_wall:
                        if await _finalize_success(session, ctx, persist_fn):
                            return
                        _handoff(session, "the login didn't fully complete")
                        continue
                    if _social_wall:
                        wall_after_code += 1
                        if wall_after_code >= 2:
                            _handoff(session, "the login was rejected after the code")
                            continue

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
                _handoff(session, "an anti-bot / captcha check is blocking the agent")
                continue
            if stuck >= 3:  # input no longer changes the page — captcha / dead end
                if code_entered and popup_seen and _popup_closed(ctx) and await _salvage_session(
                        session, ctx, persist_fn):
                    return
                _handoff(session, "the page stopped responding to the agent")
                continue

            action = await action_fn(elements=elements, screenshot_png=session.screenshot,
                                     available=available, history=history, code_pending=code_pending)
            code_pending = False
            if not action:
                if none_retry < 1:            # a transient hiccup — try once more
                    none_retry += 1
                    await _safe_sleep(page, 800)
                    continue
                if code_entered and post_code_grace < 2:   # login may still be settling
                    post_code_grace += 1
                    await _safe_sleep(page, 2500)
                    continue
                if code_entered and popup_seen and _popup_closed(ctx) and await _salvage_session(
                        session, ctx, persist_fn):
                    return
                _handoff(session, "the assistant couldn't work out the next step")
                continue
            none_retry = 0
            a = action.get("action")
            _raw_idx = action.get("index", -1)
            idx = int(_raw_idx) if _raw_idx is not None else -1   # NB: index 0 is valid
            label = labels.get(idx, f"element {idx}")
            sel = f'[data-ai-idx="{idx}"]'
            history.append(f"{a}{('#' + str(idx)) if idx >= 0 else ''}"
                           f"{(' ' + action['secret']) if action.get('secret') else ''}")
            try:
                _pages_before = len([p for p in ctx.pages if not p.is_closed()])
            except Exception:
                _pages_before = 1

            if a == "done":
                if await _finalize_success(session, ctx, persist_fn):
                    return
                _handoff(session, "the login didn't fully complete")
                continue
            if a == "fail":
                # Right after the code the page is often mid-transition (a brief
                # "verifying…" / blank state) — don't accept the model's give-up
                # until we've let the login settle and re-checked for success.
                if code_entered and post_code_grace < 2:
                    post_code_grace += 1
                    await _safe_sleep(page, 2500)
                    continue
                if code_entered and popup_seen and _popup_closed(ctx) and await _salvage_session(
                        session, ctx, persist_fn):
                    return
                _handoff(session, action.get("reason") or "the assistant couldn't proceed")
                continue
            if a == "type":
                if idx < 0:
                    # No field to type into yet (e.g. the social-chooser wall) —
                    # advance the email login instead of wasting the step.
                    did = await _advance_email_login(page, elements, session.secrets)
                    session.log.append(f"Open the email login ({did})." if did
                                       else "No field to type into yet.")
                else:
                    val = session.secrets.get(action.get("secret") or "", "")
                    session.log.append(f"Enter {action.get('secret') or 'value'} into “{label[:40]}”")
                    if val:
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
                if _is_social_login(elem_by_idx.get(idx, {})):
                    # Never go down a Google/Apple SSO path — the user gave email +
                    # password. Steer to the email login; only allow the SSO click
                    # if there's genuinely no email alternative on the page.
                    did = await _advance_email_login(page, elements, session.secrets)
                    if did:
                        session.log.append(f"Using the email login instead of “{label[:30]}” ({did}).")
                    else:
                        session.log.append(f"Click “{label[:50]}”")
                        try:
                            await page.click(sel, timeout=8000)
                        except Exception:
                            pass
                else:
                    session.log.append(f"Click “{label[:50]}”")
                    try:
                        await page.click(sel, timeout=8000)
                    except Exception:
                        pass
            elif a == "await_code":
                if unattended:   # no human to read the code — give up cleanly now
                    _handoff(session, "a one-time code is required")
                    continue
                session.prompt = ("Enter the one-time code the site just sent you "
                                  "(email / SMS / authenticator).")
                session.status = "need_code"
                session.log.append("Waiting for your one-time code…")
                code = await session._wait_for_code()
                if code is None:
                    _handoff(session, "no one-time code was entered")
                    continue
                session.status = "running"
                n_inputs = sum(1 for e in elements if e.get("tag") == "input")
                if idx >= 0:
                    await _enter_code(page, sel, code)
                session.log.append(f"Code entered ({n_inputs} field(s) on the page).")
                # NB: we entered AND submitted the code ourselves, so we must NOT set
                # code_pending — that tells the model "a code is waiting, go fill the
                # code field", and on the now-moved-on page it finds none and fails
                # with "No input fields or buttons visible to enter the code".
                code_entered = True
                await _safe_sleep(page, 1500)  # let the code submission take effect
                # Snapshot cookies NOW, while the SSO popup (e.g. indeed.com) is
                # still open — the identity provider often clears its cookies once
                # the popup closes, so the final capture would miss them.
                try:
                    session._interim_state = await ctx.storage_state()
                except Exception:
                    pass
            else:
                await _safe_sleep(page, 900)
            # If this step opened a federated-login popup, wait for it to register
            # and navigate before the next loop, so we follow it (not the opener).
            await _await_popup(ctx, _pages_before, page)
            continue
    except Exception as exc:  # noqa: BLE001
        log.warning("ai-login agent error: %s", exc)
        # Once the code is in and the login popup has closed, the login has
        # completed — any error here is teardown noise (a page closing under us,
        # etc.), not a real failure. Capture the session rather than reporting it.
        salvaged = False
        if session.status != "done" and code_entered and ctx is not None and _popup_closed(ctx):
            salvaged = await _salvage_session(session, ctx, persist_fn)
        if not salvaged and session.status not in ("done",):
            session.status = "error"
            session.error = engine_error_message(exc) or f"{type(exc).__name__}: {exc}"
    finally:
        if close:
            await close()


_WALL_MSG = (
    "The site is showing an anti-bot / bot-detection check on its login page, so "
    "automated login can't get through it. Log in manually in your own browser "
    "and paste a session cookie instead (Session cookies, below)."
)

_CODE_FAIL_MSG = (
    "After the one-time code the site returned to its login screen — the code was "
    "rejected or the automated login was blocked (anti-bot scoring). Try again, or "
    "log in manually in your own browser and paste a session cookie instead "
    "(Session cookies, below)."
)

_SESSION_REJECTED_MSG = (
    "The login finished, but reusing the saved session still lands on a sign-in "
    "page — the site rejected the automated login (anti-bot scoring) or needs more "
    "than cookies. Log in manually in your own browser and paste a session cookie "
    "instead (Session cookies, below)."
)

_STUCK_MSG = (
    "The login page stopped responding to the agent — almost always a captcha or"
    "'press & hold' anti-bot check, which can't be automated. Log in manually in "
    "your own browser and paste a session cookie instead (Session cookies, below)."
)


def _is_code_field(el: dict) -> bool:
    """Does this input look like a one-time-code / OTP field?"""
    if el.get("tag") != "input":
        return False
    if "one-time-code" in (el.get("autocomplete") or "").lower():
        return True
    blob = " ".join(str(el.get(k, "")) for k in
                    ("name", "id", "placeholder", "aria", "label")).lower()
    return any(w in blob for w in ("one-time", "otp", "passcode", "verification code",
                                   "security code", "enter code", "enter the code"))


def _is_social_login(el: dict) -> bool:
    """A third-party SSO button (Continue with Google/Apple/Facebook/…).

    NB: Glassdoor's email path is confusingly labelled "Continue with Apple or
    email" — that opens the email form, so anything mentioning "email" is NOT
    treated as social.
    """
    t = (el.get("label") or el.get("aria") or "").lower()
    if "email" in t:
        return False
    return any(p in t for p in (
        "continue with google", "continue with apple", "continue with facebook",
        "continue with microsoft", "sign in with google", "sign in with apple",
        "sign in with facebook", "sign in with microsoft"))


async def _advance_email_login(page, elements: list[dict], secrets: dict) -> str | None:
    """Push the email/password login forward WITHOUT using an SSO provider — used
    to override the model when it wrongly reaches for "Continue with Google/Apple".
    Returns a short description of what it did, or None if there was no email path.
    """
    def sel(e):
        return f'[data-ai-idx="{e.get("idx")}"]'

    # 1) fill an empty email/username field
    for e in elements:
        if (e.get("tag") == "input" and not e.get("filled")
                and _credential_for_field(e, secrets) == "username"):
            try:
                await page.fill(sel(e), secrets.get("username", ""))
                return "email"
            except Exception:
                pass
    # 2) fill an empty password field
    for e in elements:
        if (e.get("tag") == "input" and not e.get("filled")
                and _credential_for_field(e, secrets) == "password"):
            try:
                await page.fill(sel(e), secrets.get("password", ""))
                return "password"
            except Exception:
                pass
    # 3) click a non-social submit/continue button to advance
    for e in elements:
        t = (e.get("label") or "").lower().strip()
        if e.get("type") in ("submit", "button") and t in (
                "continue", "next", "sign in", "log in", "verify", "submit"):
            try:
                await page.click(sel(e), timeout=6000)
                return "continue"
            except Exception:
                pass
    # 4) click an explicit "…email" gateway button (e.g. "Continue with Apple or email")
    for e in elements:
        t = (e.get("label") or "").lower()
        if "email" in t and "google" not in t and any(
                w in t for w in ("continue", "sign in", "log in", "use", "with")):
            try:
                await page.click(sel(e), timeout=6000)
                return "email option"
            except Exception:
                pass
    return None


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
                await _safe_sleep(page, 1500)
                if await _recaptcha_token(page):
                    return True
                continue
            prompt = (await desc.first.inner_text(timeout=4000)).replace("\n", " ").strip()
            # "Select all squares with motorcycles" -> "motorcycles"
            target = prompt.split(" with ", 1)[-1].strip() or prompt
            tiles = fr.locator(".rc-imageselect-tile")
            n = await tiles.count()
            if n == 0:
                await _safe_sleep(page, 1500)
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
                    await _safe_sleep(page, 250)
                except Exception:
                    pass
        await _shot()  # show the picks before verifying
        try:
            await fr.locator("#recaptcha-verify-button").click(timeout=4000)
        except Exception:
            pass
        await _safe_sleep(page, 2800)
    return bool(await _recaptcha_token(page))


def _active_page(ctx, current):
    """The freshest real (non-blank) open page — where the login flow moved to.

    Federated logins open the credential form in a popup; once it completes and
    closes, the freshest remaining page is the (now logged-in) opener. We ignore
    'about:blank' so a just-opened popup mid-navigation doesn't read as empty —
    _await_popup() makes sure a real popup has registered + navigated before we
    get here, so we never fall back to the opener and re-open a second popup.
    """
    try:
        open_pages = [p for p in ctx.pages if not p.is_closed()]
    except Exception:
        return current
    if not open_pages:
        return current
    real = [p for p in open_pages if (p.url and not p.url.startswith("about:"))]
    return (real or open_pages)[-1]


async def _await_popup(ctx, n_before: int, page) -> None:
    """If the action just taken spawned a popup, wait for it to register and
    navigate to a real URL — so the next _active_page() follows it instead of
    briefly falling back to the opener and triggering a duplicate popup."""
    for _ in range(4):  # ~0.6s: did a popup open at all?
        try:
            n = len([p for p in ctx.pages if not p.is_closed()])
        except Exception:
            return
        if n > n_before:
            break
        await _safe_sleep(page, 150)
    else:
        return  # nothing opened
    for _ in range(14):  # ~2.1s: let it leave about:blank
        try:
            pages = [p for p in ctx.pages if not p.is_closed()]
        except Exception:
            return
        if any(p.url and not p.url.startswith("about:") for p in pages[n_before:]):
            return
        await _safe_sleep(page, 150)


async def _enter_code(page, sel: str, code: str) -> None:
    """Type a one-time code with real keystrokes, then submit with Enter.

    page.fill() sets .value directly and skips the keypress events that OTP
    widgets rely on — especially multi-box inputs that auto-advance per digit —
    so the code silently doesn't register. Focus + keyboard.type fixes both the
    single-field and split-box cases. We submit with Enter only and leave any
    explicit Continue/Verify click to the model on the next step — a blind button
    click here risks hitting a social-login button ("Continue with Google").
    """
    try:
        await page.focus(sel)
        await page.keyboard.type(code, delay=60)
    except Exception:
        try:
            await page.fill(sel, code)
        except Exception:
            pass
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


def _popup_closed(ctx) -> bool:
    """True when the login flow is back to a single page — the federated-login
    popup has closed, which (after a code) means login completed."""
    try:
        return len([p for p in ctx.pages if not p.is_closed()]) <= 1
    except Exception:
        return False


async def _verify_session(ctx, url: str) -> bool:
    """Reload the target with the captured session and check it no longer demands a
    login. Catches a login the site silently rejected (anti-bot): the popup closes
    but reusing the session still lands on a sign-in page. On any error verifying,
    returns True (don't fail a real success on a fluke)."""
    p = None
    els = None
    try:
        p = await ctx.new_page()
        await p.goto(url, wait_until="domcontentloaded", timeout=30000)
        await _settle(p)
        els = await p.evaluate(_OBSERVE_JS)
    except Exception:
        els = None
    if p is not None:
        try:
            await p.close()
        except Exception:
            pass
    if els is None:
        return True
    # strong logged-out signals: a password field or the social-login wall
    return not (any(e.get("type") == "password" for e in els) or any(
        any(s in (e.get("label") or "").lower()
            for s in ("continue with google", "continue with apple", "apple or email"))
        for e in els))


async def _salvage_session(session, ctx, persist_fn) -> bool:
    """Capture + persist the session when login looks complete but the flow ended
    on an error. Verifies the session actually works first; returns True only if
    it does (and there were cookies to save)."""
    if not await _verify_session(ctx, session.url):
        return False
    try:
        state = await ctx.storage_state()
    except Exception:
        return False
    state = merge_storage_state(session._interim_state, state) or state
    if not state.get("cookies"):
        return False
    session.result_state = state
    try:
        await persist_fn(state)
    except Exception:
        return False
    session.status = "done"
    session.error = None
    session.log.append("Logged in — session saved.")
    return True


def _expired(session) -> bool:
    return (time.monotonic() - session.created_at) > SESSION_TTL


def _handoff(session, reason: str) -> None:
    """Hand control to the user instead of hard-failing — the core of the manual
    fallback. The AI agent pauses; the modal lets the user drive the live page.

    In unattended mode (auto re-login) there's no one to take over, so fail cleanly
    instead — the run_agent loop sees status='error' and stops."""
    if getattr(session, "_unattended", False):
        session.mode = "ai"
        session.status = "error"
        session.error = f"{reason} — needs a human (captcha/OTP); auto re-login can't continue"
        session.log.append(f"Auto re-login stopped — {reason}.")
        return
    session.mode = "manual"
    session.status = "running"
    session.error = None
    session.prompt = (f"{reason} — take over: click/type on the page below, finish "
                      "the login, then press ‘Capture session & finish’.")
    session.log.append(f"Handed control to you — {reason}.")


_ALLOWED_KEYS = {
    "Enter", "Tab", "Backspace", "Escape", "Delete", "Home", "End",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "PageUp", "PageDown",
}


async def _viewport_css(page) -> tuple[float, float]:
    """The real CSS viewport (what full_page=False screenshots cover and what
    page.mouse uses). page.viewport_size is None under Camoufox's no-viewport
    mode, so read window.innerWidth/innerHeight from the page itself."""
    try:
        vw, vh = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
        if vw and vh:
            return float(vw), float(vh)
    except Exception:
        pass
    vs = (page.viewport_size if hasattr(page, "viewport_size") else None) or {"width": 1280, "height": 720}
    return float(vs["width"]), float(vs["height"])


async def _click_scale(page, session) -> tuple[float, float]:
    """The CSS size that the live SCREENSHOT actually represents — clicks must map
    to this, NOT innerWidth. Camoufox screenshots a 1280px CROP of a wider page, so
    scaling by innerWidth would push every click far to the right. The screenshot
    is the page top-left at 1:1 (CSS px × devicePixelRatio), so divide out the dpr."""
    shot = session.screenshot
    if shot and len(shot) >= 24 and shot[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = struct.unpack(">II", shot[16:24])     # PNG IHDR width/height
            dpr = float(await page.evaluate("() => window.devicePixelRatio || 1")) or 1.0
            if w and h:
                return w / dpr, h / dpr
        except Exception:
            pass
    return await _viewport_css(page)


async def _human_move(page, session, x: float, y: float) -> None:
    """Glide the cursor to (x, y) with real motion that ends EXACTLY on target.

    Camoufox humanises mouse movement ITSELF — and it does so PER mouse.move call,
    so passing Playwright's `steps` (N intermediate moves) makes it ~1s × N (20s+!).
    So on Camoufox we issue a single move and let it humanise; on the plain engines
    we use Playwright's fast `steps` interpolation (+ a slight curve)."""
    x0, y0 = session._mouse or (x, y)
    try:
        if session._native_human:                    # Camoufox: one humanised move
            await page.mouse.move(x, y)
        else:
            dist = math.hypot(x - x0, y - y0)
            steps = max(4, min(24, int(dist / 22)))
            if dist > 60:                            # gentle curve toward the target
                mx = x0 + (x - x0) * 0.55 + random.uniform(-14, 14)
                my = y0 + (y - y0) * 0.55 + random.uniform(-14, 14)
                await page.mouse.move(mx, my, steps=max(3, steps // 2))
            await page.mouse.move(x, y, steps=steps)
    except Exception:
        pass
    session._mouse = (x, y)


async def _human_press(page, x: float, y: float) -> None:
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.05, 0.13))   # human press duration
    await page.mouse.up()


async def _type_human(page, text: str) -> None:
    """Type with per-key delays (and occasional longer pauses) — not an instant dump."""
    for ch in text[:500]:
        try:
            await page.keyboard.type(ch)
        except Exception:
            return
        await asyncio.sleep(random.uniform(0.05, 0.16)
                            if random.random() > 0.08 else random.uniform(0.25, 0.5))


async def _apply_event(page, ev: dict, session) -> None:
    """Relay one user input event to the live page (manual remote control).

    Coordinates arrive as fractions (0..1) of the live SCREENSHOT, so we scale by
    the screenshot's CSS size (which can be a crop of the page under Camoufox)."""
    t = ev.get("type")
    vw, vh = await _click_scale(page, session)

    def px(fx, fy):
        return (max(0.0, min(1.0, float(fx))) * vw, max(0.0, min(1.0, float(fy))) * vh)

    if t in ("click", "dblclick"):
        x, y = px(ev.get("fx", 0), ev.get("fy", 0))
        _dlog("ai-login[%s] %s @ (%.0f,%.0f) on %s", session.id[:8], t, x, y,
              (getattr(page, "url", "") or "")[:70])
        await _human_move(page, session, x, y)
        await _human_press(page, x, y)
        if t == "dblclick":
            await asyncio.sleep(random.uniform(0.06, 0.12))
            await _human_press(page, x, y)
    elif t == "move":
        x, y = px(ev.get("fx", 0), ev.get("fy", 0))
        await _human_move(page, session, x, y)
    elif t == "drag":                                # slider captchas, etc.
        x, y = px(ev.get("fx", 0), ev.get("fy", 0))
        x2, y2 = px(ev.get("fx2", ev.get("fx", 0)), ev.get("fy2", ev.get("fy", 0)))
        await _human_move(page, session, x, y)
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.12))
        await _human_move(page, session, x2, y2)
        await asyncio.sleep(random.uniform(0.05, 0.12))
        await page.mouse.up()
    elif t == "type":
        await _type_human(page, str(ev.get("text", "")))
    elif t == "key":
        k = str(ev.get("key", ""))
        if k in _ALLOWED_KEYS:
            await page.keyboard.press(k)
            await asyncio.sleep(random.uniform(0.04, 0.1))
    elif t == "scroll":
        try:
            await page.mouse.wheel(0, float(ev.get("dy", 0)))
        except Exception:
            pass


def _page_count(page) -> int:
    try:
        return len([p for p in page.context.pages if not p.is_closed()])
    except Exception:
        return -1


async def _shot_clip(page):
    """Capture exactly the visible page (innerWidth × innerHeight), so the live
    view never crops the page (Camoufox renders wider than the viewport) and never
    pads it with blank space (Camoufox's innerHeight < the viewport). Reading the
    sizes each frame means it auto-adapts to whatever window the engine chose.
    Bounded so a mid-navigation evaluate can't block the stream."""
    try:
        vw, vh = await asyncio.wait_for(
            page.evaluate("() => [Math.ceil(window.innerWidth), Math.ceil(window.innerHeight)]"),
            timeout=1.2)
        if vw and vh:
            return {"x": 0, "y": 0, "width": int(vw), "height": int(vh)}
    except Exception:
        pass
    return None


async def _shot(session, page) -> None:
    """Capture the live view. page.screenshot() refuses to capture while the page
    is 'loading' (it waits for fonts/stability) — and real sites (Glassdoor) keep
    loading forever via trackers, so it times out and nothing shows. So: try a
    quick normal capture; if that times out, halt pending network with window.stop()
    and capture the current rendered state. We remember per-URL that a page needs
    the stop so we don't pay the timeout every frame. The clip keeps the view to
    exactly the visible page on every engine/window size."""
    now = time.monotonic()
    try:
        cur = page.url or ""
    except Exception:
        cur = ""
    if cur != session._shot_url:          # new page → re-probe the fast path
        _dlog("ai-login[%s] page → %s (pages=%d)", session.id[:8], cur[:90],
              _page_count(page))
        session._shot_url = cur
        session._shot_url_since = now
        session._shot_needs_stop = False
    clip = await _shot_clip(page)
    kw = {"clip": clip} if clip else {"full_page": False}
    if not session._shot_needs_stop:
        try:
            session.screenshot = await page.screenshot(type="png", timeout=1500, **kw)
            return
        except Exception:
            # Only escalate to window.stop() once the URL has been STABLE a moment.
            # Halting mid-navigation aborts a redirect (e.g. the OAuth handoff
            # oauth2/code → secure.indeed.com), which hangs the login. While the URL
            # is fresh, just skip the frame and let the redirect proceed.
            if (now - session._shot_url_since) < 3.0:
                return
            session._shot_needs_stop = True
    try:
        await page.evaluate("() => { try { window.stop(); } catch (e) {} }")
    except Exception:
        pass
    try:
        session.screenshot = await page.screenshot(type="png", timeout=4000, **kw)
    except Exception:
        pass


async def _manual_step(session, page) -> None:
    """One manual-control tick: stream a fresh frame, then apply queued input.

    Screenshot FIRST so the view stays live even when the next action is slow
    (a Camoufox humanised move blocks ~1.5s) — and so _click_scale always has a
    current frame to map clicks against."""
    await _shot(session, page)
    evs, session._events = session._events[:12], session._events[12:]
    # collapse runs of hover 'move' events to just the latest — no point replaying
    # a stale trail, and it keeps the queue from backing up.
    compact = []
    for ev in evs:
        if ev.get("type") == "move" and compact and compact[-1].get("type") == "move":
            compact[-1] = ev
        else:
            compact.append(ev)
    acted = False
    for ev in compact:
        try:
            await _apply_event(page, ev, session)
        except Exception:
            pass
        if ev.get("type") != "move":
            acted = True
    # One refresh AFTER the whole batch — not after every click. Re-shooting a
    # still-loading page (e.g. a reCAPTCHA wall) costs ~1.5s each, so per-event
    # shots let a burst of clicks back the queue up to many seconds of lag, which
    # makes the view feel dead and provokes more clicking. The 500ms poll keeps
    # the view live between ticks regardless.
    if acted:
        await _shot(session, page)
    await _safe_sleep(page, 120)


async def _finalize_success(session, ctx, persist_fn) -> bool:
    """The agent thinks login completed — verify it actually grants access, then
    capture + persist. Returns True if done; False means it didn't really work
    (caller hands off to manual) and nothing is saved."""
    session.log.append("Checking the saved session works…")
    works = await _verify_session(ctx, session.url)
    try:
        state = await ctx.storage_state()
    except Exception:
        state = {"cookies": [], "origins": []}
    state = merge_storage_state(session._interim_state, state) or state
    if not works:
        return False
    session.result_state = state
    await persist_fn(state)
    session.status = "done"
    cks = state.get("cookies") or []
    doms = sorted({(c.get("domain") or "?") for c in cks})
    session.log.append(f"Logged in — session saved ({len(cks)} cookies"
                       f"{(' across ' + ', '.join(doms)) if doms else ''}).")
    return True


async def _finish_capture(session, ctx, persist_fn) -> None:
    """User pressed ‘Capture session & finish’ — verify, capture (incl. SSO-popup
    cookies), persist, and report whether the session looks logged-in."""
    works = await _verify_session(ctx, session.url)
    try:
        state = await ctx.storage_state()
    except Exception:
        state = {"cookies": [], "origins": []}
    state = merge_storage_state(session._interim_state, state) or state
    session.result_state = state
    await persist_fn(state)
    session.status = "done"
    cks = state.get("cookies") or []
    doms = sorted({(c.get("domain") or "?") for c in cks})
    note = "" if works else " (warning: a reload still showed a sign-in page — it may not be valid)"
    session.log.append(
        f"Session captured ({len(cks)} cookies across {', '.join(doms) or 'no domains'}){note}.")


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


def _kill_session(v: LoginSession) -> None:
    """Cancel a live session's agent task and drop it from the registry."""
    if v._task is not None:
        try:
            v._task.cancel()
        except Exception:
            pass
    _SESSIONS.pop(v.id, None)


def create_session(monitor_id: int, user_id: int, url: str, secrets: dict) -> LoginSession:
    _gc()
    # One login browser per monitor: tear down any prior session for it. Two
    # stealth browsers hitting the same site at once from one IP trips bot-detection
    # and stalls the OAuth handoff — exactly the "stuck" symptom.
    for old in [v for v in _SESSIONS.values() if v.monitor_id == monitor_id]:
        _dlog("ai-login: replacing prior session %s for monitor %s", old.id[:8], monitor_id)
        _kill_session(old)
    # Bound concurrent live login browsers per user (each is a real headless browser).
    # Evict the oldest over the cap so a user can't accumulate many (esp. via the
    # credential-free manual mode) and exhaust the box's RAM/process budget.
    user_live = sorted((v for v in _SESSIONS.values() if v.user_id == user_id),
                       key=lambda v: v.created_at)
    for old in user_live[:max(0, len(user_live) - (MAX_SESSIONS_PER_USER - 1))]:
        _dlog("ai-login: evicting oldest session %s for user %s (per-user cap)",
              old.id[:8], user_id)
        _kill_session(old)
    s = LoginSession(id=uuid.uuid4().hex, monitor_id=monitor_id, user_id=user_id,
                     url=url, secrets=secrets)
    s._code_event = asyncio.Event()
    _SESSIONS[s.id] = s
    return s
