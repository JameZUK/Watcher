"""Page-level operations shared by Playwright and Camoufox.

Both engines expose a Playwright `Page`, so login replay, waiting, actions,
and capture are identical regardless of the underlying browser.
"""

from __future__ import annotations

import asyncio
import json as _json
import random
from urllib.parse import urlparse

from ..auth.login_flows import resolve_secrets
from ..config import settings
from ..models import Monitor
from .base import VISIBLE_TEXT_JS, RenderResult

# --- "uBlock-style" cleanup: drop ad/tracker requests + hide cookie banners ---
# This is a built-in equivalent (works headless across Chromium + Camoufox via
# the Playwright API) rather than a managed browser extension. Suffix-matched.
_BLOCK_HOSTS = frozenset({
    "doubleclick.net", "googlesyndication.com", "googletagmanager.com",
    "google-analytics.com", "googleadservices.com", "googletagservices.com",
    "adservice.google.com", "amazon-adsystem.com", "adnxs.com", "adsrvr.org",
    "adform.net", "criteo.com", "criteo.net", "taboola.com", "outbrain.com",
    "pubmatic.com", "rubiconproject.com", "openx.net", "casalemedia.com",
    "33across.com", "moatads.com", "2mdn.net", "serving-sys.com",
    "smartadserver.com", "teads.tv", "indexww.com", "yieldmo.com",
    "bidswitch.net", "advertising.com", "scorecardresearch.com",
    "quantserve.com", "quantcast.com", "demdex.net", "omtrdc.net",
    "hotjar.com", "mouseflow.com", "fullstory.com", "mixpanel.com",
    "amplitude.com", "segment.com", "segment.io", "clarity.ms",
    "bat.bing.com", "ads-twitter.com", "analytics.tiktok.com",
    "connect.facebook.net", "snap.licdn.com",
})

# Cosmetic CSS hiding the common consent platforms + generic cookie banners,
# and undoing the scroll-lock they apply to <body>.
_COOKIE_CSS = (
    "#onetrust-consent-sdk,#onetrust-banner-sdk,.onetrust-pc-dark-filter,"
    "#CybotCookiebotDialog,#CybotCookiebotDialogBodyUnderlay,#CookiebotWidget,"
    "#usercentrics-root,[id^=\"usercentrics\"],#uc-banner,"
    "#didomi-host,#didomi-notice,.didomi-popup-open,"
    ".qc-cmp2-container,.qc-cmp2-cleanslate,.qc-cmp-cleanslate,"
    "[id^=\"sp_message_container\"],.sp_veil,#truste-consent-track,#consent_blackbar,"
    ".fc-consent-root,.fc-dialog-overlay,"
    ".cc-window,.cookie-consent,.cookie-banner,.cookie-notice,.cookie-popup,"
    "#cookie-banner,#cookie-notice,#cookieConsent,#cookie-law-info-bar,"
    "#gdpr-consent,#gdpr-cookie-message,.gdpr-banner,[class*=\"CookieConsent\"],"
    "[class*=\"ConsentBanner\"],[class*=\"consent-banner\" i],[class*=\"cookie-banner\" i],"
    "[data-testid*=\"cookie\" i],[data-testid*=\"consent\" i],[data-nosnippet][class*=\"consent\" i]"
    "{display:none !important;visibility:hidden !important;}"
    "html,body{overflow:auto !important;position:static !important;}"
)

# GENERIC, site-agnostic consent handler (no per-site selectors): find overlay
# elements that look like a cookie/consent prompt, click their accept/agree
# button (so the banner closes properly and any gated content loads), and hide
# whatever remains. Restores the scroll-lock such banners impose. Returns counts.
_BANNER_HEURISTIC_JS = r"""() => {
  const out = { clicked: 0, hidden: 0, walls: [] };
  // Consent wording (multilingual): EN + common FR/DE/ES/IT/PT/NL terms. Most
  // non-English banners still contain the word "cookie(s)", but include native
  // privacy words so language-only banners are still caught.
  const consentRx = /(cookie|consent|gdpr|ccpa|we value your privacy|your privacy|tracking technolog|privacy|opt[- ]?out|data protection|datenschutz|privatsph|zustimmung|confidentialit|t[ée]moins|donn[ée]es|privacidad|consentimiento|riservatezza|privacidade|we and our( up to)?( \d+)? partner|store and\/or access|legitimate interest|manage (your )?(choices|preferences|consent)|personal data)/i;
  // Accept: start-anchored English (safe short words) OR a word-boundary match of
  // distinctive non-English accept verbs (FR/DE/ES/IT/PT/NL), which often put the
  // verb LAST ("Tout accepter", "Alle akzeptieren") so a start anchor would miss.
  const acceptRx  = /(^(accept all|accept|agree|allow all|allow|got it|ok|okay|yes|i (accept|agree|understand)|understood|continue|enable all|i'?m ok|save.*accept|agree.*(close|continue))\b)|(\b(accepter|j'?accepte|tout accepter|accepter et fermer|akzeptieren|zustimmen|einverstanden|annehmen|verstanden|alle erlauben|aceptar|acepto|de acuerdo|permitir todo|accetta|acconsento|ho capito|aceitar|concordo|accepteren|akkoord|alles accepteren|toestaan)\b)/i;
  // Dismiss/close for non-blocking bars and post-accept toasts (e.g. GOV.UK's
  // "Hide cookie message"), plus native close verbs. Only used inside a banner.
  const dismissRx = /^(hide( this)?( message| cookie message)?|close|dismiss|no thanks|continue to (the )?(site|website)|fermer|schlie[sß]en|cerrar|chiudi|sluiten|×|✕|✖)$/i;
  // Reject/"manage settings" — excluded from acceptable buttons (multilingual).
  const rejectRx  = /(reject|decline|deny|do not|don'?t|refuse|manage|customi[sz]|more option|preferenc|settings|necessary only|essential only|learn more|why|without accept|continue without|reject all|refuser|g[ée]rer|param[èe]tr|personnaliser|ablehnen|einstellung|verwalten|nur (notwendige|essenzielle)|auswahl|konfigurier|mehr erfahren|rechazar|configurar|gestionar|ajustes|rifiuta|gestisci|impostazioni|personalizza|recusar|defini[çc][õo]es|weigeren|instellingen|beheren)/i;
  const vis = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 80 && r.height >= 28 && r.bottom > 0 && r.top < (innerHeight + r.height);
  };
  const findBtn = (c, rx, exclude) => {
    for (const b of c.querySelectorAll('button,a[href],[role="button"],input[type="button"],input[type="submit"]')) {
      const br = b.getBoundingClientRect();
      if (br.width < 4 || br.height < 4) continue;   // skip hidden/zero-size (e.g. a post-accept display:none button)
      const bt = ((b.innerText || b.value || b.getAttribute('aria-label') || '')).trim();
      if (!bt || bt.length > 40) continue;
      if (rx.test(bt) && !(exclude && exclude.test(bt))) return b;
    }
    return null;
  };
  // 1) candidate banners. Two shapes qualify:
  //    (a) positioned overlays (fixed/sticky/high-z absolute), OR
  //    (b) wide bars anchored to the top/bottom edge that carry an accept or
  //        dismiss button — the button is what distinguishes a real cookie bar
  //        (e.g. GOV.UK's static top banner) from ordinary page content that
  //        merely mentions "cookies"/"privacy".
  const cands = [];
  const sel = 'div,section,aside,dialog,form,[role="dialog"],[role="alertdialog"],'
            + '[data-module*="cookie" i],[class*="cookie" i],[class*="consent" i],[id*="cookie" i],[id*="consent" i]';
  for (const el of document.querySelectorAll(sel)) {
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (!vis(el)) continue;
    const t = (el.innerText || '');
    if (t.length < 8 || t.length > 2200) continue;
    const r = el.getBoundingClientRect();
    const positioned = cs.position === 'fixed' || cs.position === 'sticky'
                       || (cs.position === 'absolute' && (parseInt(cs.zIndex || '0', 10) >= 50));
    const wide = r.width >= innerWidth * 0.55;
    // a real cookie *bar* hugs an edge, spans the width, and is short — not a
    // full-height content wrapper that merely contains the word "cookie".
    const edged = (r.top <= 12 || r.bottom >= innerHeight - 12)
                  && r.top < innerHeight && wide && r.height <= innerHeight * 0.5;
    const accept  = findBtn(el, acceptRx, rejectRx);
    const dismiss = accept ? null : findBtn(el, dismissRx, null);
    // Qualify as a consent overlay if it's WORDED like one, OR it pairs an
    // accept-all button with a reject/manage control — the universal, language-
    // agnostic CMP signature (catches TCF dialogs like Le Figaro's English
    // "Make a choice for your data… / Refuse all").
    const hasManage = !!findBtn(el, rejectRx, null);
    if (!(consentRx.test(t) || (accept && hasManage))) continue;
    // Shapes: positioned overlay; OR a wide edge bar; OR a block that FILLS its
    // viewport — the last covers a dialog that fills a dedicated CMP <iframe>
    // (inside it the dialog is position:static, so the per-frame pass sees a
    // large static block). Edge/fill shapes require an actionable button so we
    // never touch ordinary content; positioned overlays may also be hidden.
    const fills = (r.width * r.height) >= innerWidth * innerHeight * 0.5;
    if (positioned) { /* ok */ }
    else if ((edged || fills) && (accept || dismiss)) { /* ok */ }
    else continue;
    el.__btn = accept || dismiss;
    cands.push(el);
  }
  // keep only outermost candidates (avoid acting on nested copies)
  const overlays = cands.filter((el) => !cands.some((o) => o !== el && o.contains(el)));
  for (const c of overlays.slice(0, 8)) {
    const btn = c.__btn || findBtn(c, acceptRx, rejectRx) || findBtn(c, dismissRx, null);
    if (btn) { try { btn.click(); out.clicked++; continue; } catch (e) {} }
    // No actionable accept/dismiss button: hide it for a clean screenshot. But if
    // it's a BLOCKING wall (covers much of the viewport) record its HTML — hiding
    // doesn't grant consent, so content gated behind it may still be missing; the
    // optional AI fallback can learn the real accept selector from this snippet.
    const rr = c.getBoundingClientRect();
    if ((rr.width * rr.height) >= innerWidth * innerHeight * 0.35) {
      const html = (c.outerHTML || '').replace(/<(script|style|svg|path|noscript)[\s\S]*?<\/\1>/gi, '');
      out.walls.push(html.slice(0, 4000));
    }
    try { c.style.setProperty('display', 'none', 'important'); out.hidden++; } catch (e) {}
  }
  // 2) generic dismissable overlays/banners that aren't consent prompts — sign-in
  // promos, newsletter/app nags, toasts (e.g. BBC's "Close sign in banner").
  // Content-safe: we ONLY click an explicit close/dismiss control, never hide, so
  // real content/modals (lightboxes, age gates) are left untouched.
  const closeRx = /^(close|no thanks|no,? thanks|not now|maybe later|dismiss|skip|×|✕|✖|⨯|✗)$/i;
  // "Close <thing>" only for UI-ish things (so we never click "Close account").
  const closePhraseRx = /^(close|dismiss|hide)\b.*\b(banner|message|dialog|popup|pop-?up|notification|notice|modal|overlay|bar|sign[- ]?in|promo|panel|toast|alert|prompt)\b/i;
  const ariaCloseRx = /^(close|dismiss|no thanks)\b/i;
  const findClose = (el) => {
    for (const b of el.querySelectorAll('button,a[href],[role="button"]')) {
      const br = b.getBoundingClientRect();
      if (br.width < 4 || br.height < 4) continue;
      const txt = ((b.innerText || b.value || '')).trim();
      const aria = ((b.getAttribute('aria-label') || b.getAttribute('title') || '')).trim();
      if ((txt && txt.length <= 40 && (closeRx.test(txt) || closePhraseRx.test(txt)))
          || (aria && (ariaCloseRx.test(aria) || closePhraseRx.test(aria)))) return b;
    }
    return null;
  };
  const closers = new Set();
  const cont = 'div,section,aside,[role="dialog"],[role="alertdialog"],[aria-modal="true"],[role="banner"],[class*="banner" i],[class*="promo" i]';
  for (const el of document.querySelectorAll(cont)) {
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    const modal = el.matches('[role="dialog"],[role="alertdialog"],[aria-modal="true"]');
    // a banner is wide, not tiny, and not most of the page (that'd be content)
    const bannerish = r.width >= innerWidth * 0.4 && r.height >= 40 && r.height <= innerHeight * 0.7;
    if (modal) { if (r.width < 200 || r.height < 120) continue; }
    else if (!bannerish) continue;
    const b = findClose(el);
    if (b) closers.add(b);
  }
  for (const b of closers) { try { b.click(); out.clicked++; } catch (e) {} }
  // undo scroll-lock the banner may have applied
  document.documentElement.style.setProperty('overflow', 'auto', 'important');
  if (document.body) {
    document.body.style.setProperty('overflow', 'auto', 'important');
    if (getComputedStyle(document.body).position === 'fixed')
      document.body.style.setProperty('position', 'static', 'important');
  }
  return out;
}"""

# Persistent auto-dismisser: installs a MutationObserver that re-runs the
# heuristic whenever the DOM changes, for a short window — so CMP banners that
# MOUNT LATE (seconds after load, e.g. Le Figaro) are still dismissed before the
# screenshot, without adding fixed latency to every render. Idempotent per frame.
_AUTODISMISS_JS = (
    "(() => { if (window.__wConsentObs) return; window.__wConsentObs = 1;\n"
    "  const sweep = " + _BANNER_HEURISTIC_JS + ";\n"
    "  const run = () => { try { sweep(); } catch (e) {} };\n"
    "  run();\n"
    "  let t = null;\n"
    "  const obs = new MutationObserver(() => { if (t) return; t = setTimeout(() => { t = null; run(); }, 250); });\n"
    "  try { obs.observe(document.documentElement, { childList: true, subtree: true }); } catch (e) {}\n"
    "  setTimeout(() => { try { obs.disconnect(); } catch (e) {} }, 8000);\n"
    "})()"
)


async def install_consent_autodismiss(page, monitor: Monitor) -> None:
    """Install a short-lived MutationObserver (main + same-origin child frames)
    that keeps dismissing consent banners as they mount — catches late CMPs that
    a single post-load sweep would miss."""
    if not getattr(monitor, "block_annoyances", True):
        return
    try:
        await page.evaluate(_AUTODISMISS_JS)
    except Exception:
        pass
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            await frame.evaluate(_AUTODISMISS_JS)
        except Exception:
            pass


async def setup_blocking(context, monitor: Monitor) -> None:
    """Install the per-render network route handler on the CONTEXT before navigation.

    One handler does two jobs (Playwright only runs the *last* registered route
    matching a URL, so SSRF and ad-blocking must share a single handler):

      1. **Render-time SSRF guard** (always on, unless ``allow_private_targets``):
         abort any request — including HTTP redirects and JS-initiated navigations —
         whose host resolves to a private/internal address. This closes the
         DNS-rebinding / redirect-to-internal residual that the one-shot
         pre-navigation check in the runner cannot catch (the browser re-resolves
         DNS itself and follows redirects on its own).
      2. **Ad/tracker blocking** (when the monitor enables it): abort known ad hosts
         for cleaner screenshots and fewer false-positive diffs.

    Cookie banners are hidden separately in capture(), after load, via hide_banners().
    """
    from ..netsec import host_resolves_internal

    ssrf_on = not settings.allow_private_targets
    adblock_on = bool(getattr(monitor, "block_annoyances", True))
    if not ssrf_on and not adblock_on:
        return
    # Per-host DNS verdict cache: a page makes many requests to a handful of hosts,
    # so resolve each host at most once per render (off the event loop).
    dns_internal: dict[str, bool] = {}

    async def _route(route):
        try:
            host = (urlparse(route.request.url).hostname or "").lower()
        except Exception:
            host = ""
        # 1) SSRF: block internal destinations (redirects/navigations included).
        if ssrf_on and host:
            try:
                verdict = dns_internal.get(host)
                if verdict is None:
                    verdict = await asyncio.to_thread(host_resolves_internal, host)
                    dns_internal[host] = verdict
                if verdict:
                    await route.abort()
                    return
            except Exception:
                pass
        # 2) Ad/tracker block.
        if adblock_on and host and any(host == d or host.endswith("." + d) for d in _BLOCK_HOSTS):
            try:
                await route.abort()
                return
            except Exception:
                pass
        try:
            await route.continue_()
        except Exception:
            pass

    try:
        await context.route("**/*", _route)
    except Exception:
        pass


async def install_ssrf_guard(context) -> None:
    """Install a context route that aborts any request — redirects and JS/user-
    initiated navigations included — whose host resolves to a private/internal
    address. Used by the AI-login browser, which the manual remote-control feature
    lets an authenticated user DRIVE: without this, a click on an internal link or a
    typed `http://169.254.169.254` / `http://127.0.0.1` URL would navigate the
    server-side browser into the internal network and stream it back / harvest its
    cookies. No-op when allow_private_targets is set (trusted/internal deployments).

    (The render engines fold the same check into setup_blocking; this standalone
    installer is for contexts that don't run ad-blocking.)"""
    if settings.allow_private_targets:
        return
    from ..netsec import host_resolves_internal
    dns_internal: dict[str, bool] = {}

    async def _route(route):
        try:
            host = (urlparse(route.request.url).hostname or "").lower()
            if host:
                verdict = dns_internal.get(host)
                if verdict is None:
                    verdict = await asyncio.to_thread(host_resolves_internal, host)
                    dns_internal[host] = verdict
                if verdict:
                    await route.abort()
                    return
        except Exception:
            pass
        try:
            await route.continue_()
        except Exception:
            pass

    try:
        await context.route("**/*", _route)
    except Exception:
        pass


async def click_consent(page, monitor: Monitor) -> None:
    """Click the monitor's configured consent/dismiss selectors in order (accept
    a cookie banner, close a modal, tick a captcha checkbox, …). Best-effort:
    a missing/un-clickable selector is skipped. Runs in the main frame and any
    child frame that contains the selector (consent dialogs are often iframed)."""
    for sel in getattr(monitor, "consent_clicks", None) or []:
        sel = (sel or "").strip()
        if not sel:
            continue
        clicked = False
        try:
            await page.click(sel, timeout=2500)
            clicked = True
        except Exception:
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    await frame.click(sel, timeout=1500)
                    clicked = True
                    break
                except Exception:
                    pass
        if clicked:
            try:
                await page.wait_for_timeout(500)   # let the next step settle
            except Exception:
                pass


async def hide_banners(page, monitor: Monitor) -> list | None:
    """Inject the consent-banner-hiding CSS AFTER load (reliable, unlike a
    document-start init script) so banners are gone from both the text and the
    screenshot. Re-applied to child frames where they exist.

    Returns trimmed HTML of any BLOCKING consent wall the heuristic could only
    hide (not accept) — content gated behind it may still be missing, so the
    optional AI fallback can learn a real accept selector. None if all clear."""
    if not getattr(monitor, "block_annoyances", True):
        return None

    walls: list[str] = []

    async def _run(target):
        try:
            res = await target.evaluate(_BANNER_HEURISTIC_JS)
            if isinstance(res, dict) and res.get("walls"):
                walls.extend(res["walls"])
        except Exception:
            pass

    async def _sweep():
        try:
            await page.add_style_tag(content=_COOKIE_CSS)
        except Exception:
            pass
        await _run(page)
        # Consent dialogs are frequently rendered inside their own iframe; run the
        # same generic handler + CSS inside every child frame.
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            await _run(frame)
            try:
                await frame.add_style_tag(content=_COOKIE_CSS)
            except Exception:
                pass

    # Two passes: a second consent layer (or post-accept toast) can appear after
    # the first is dismissed.
    await _sweep()
    try:
        await page.wait_for_timeout(400)
    except Exception:
        pass
    await _sweep()
    # De-dup; cap to keep the AI prompt small/cheap.
    return list(dict.fromkeys(walls))[:2] or None


# Detect a LARGE positioned overlay still covering the page after every automatic
# pass — a paywall / full-screen interstitial the heuristic couldn't dismiss.
# High-precision: positioned + big + has a control, so it's an obstruction, not a
# header/toolbar/widget or static article content. Its HTML feeds the AI fallback.
_OBSTRUCTION_JS = r"""() => {
  const vis = (el) => { let cs; try{cs=getComputedStyle(el);}catch(e){return false;}
    if (cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity||'1')===0) return false;
    return true; };
  const cands = [];
  for (const el of document.querySelectorAll('div,section,aside,dialog,[role="dialog"],[role="alertdialog"]')) {
    let cs; try{cs=getComputedStyle(el);}catch(e){continue;}
    if(!vis(el))continue;
    const positioned = cs.position==='fixed'||cs.position==='sticky'||(cs.position==='absolute'&&parseInt(cs.zIndex||'0',10)>=50);
    if(!positioned)continue;                                   // an obstruction overlays content
    const r = el.getBoundingClientRect();
    const big = (r.width*r.height)>=innerWidth*innerHeight*0.35 && r.width>=innerWidth*0.5 && r.height>=innerHeight*0.3;
    if(!big)continue;
    const t=(el.innerText||''); if(t.length<8||t.length>4000)continue;
    if(!el.querySelector('button,a[href],[role="button"],input'))continue;   // has an actionable control
    cands.push(el);
  }
  const overlays = cands.filter(el => !cands.some(o => o!==el && o.contains(el)));
  const out=[];
  for(const c of overlays.slice(0,1)){
    let html=(c.outerHTML||'').replace(/<(script|style|svg|path|noscript)[\s\S]*?<\/\1>/gi,'');
    out.push(html.slice(0,4000));
  }
  return out;
}"""


async def detect_obstructions(page, monitor: Monitor) -> list | None:
    """HTML of any large positioned overlay still covering the page after the
    automatic passes (a paywall / interstitial the heuristic couldn't dismiss),
    main + same-origin frames. Feeds the AI dismiss-selector fallback. None if clear."""
    if not getattr(monitor, "block_annoyances", True):
        return None
    found: list[str] = []
    for frame in page.frames:
        try:
            found += await frame.evaluate(_OBSTRUCTION_JS) or []
        except Exception:
            pass
        if found:
            break
    return list(dict.fromkeys(found))[:1] or None


# Reveal the full page before a full-page screenshot: force lazy media to load,
# and release any leftover scroll-lock (a banner/modal locks the page to ~100vh
# with clipped overflow; if that isn't fully released the screenshot is a tall,
# mostly-blank image with content only in the first fold — e.g. BBC News). Only
# un-clamps top-level LAYOUT wrappers that are clamped to ~one viewport, so media
# components with intentional heights are left alone.
_REVEAL_CONTENT_JS = r"""() => {
  for (const el of document.querySelectorAll('img[loading="lazy"],iframe[loading="lazy"]')) {
    try { el.loading = 'eager'; } catch (e) {}
  }
  const vh = innerHeight;
  const sel = 'html,body,body > div,body > div > div,main,[role="main"],'
            + '#root,#app,#__next,[class*="page" i],[class*="app" i],[class*="layout" i],[id*="main" i]';
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.height > vh + 4 || r.height < 100) continue;     // only ~viewport-clamped shells
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    el.style.setProperty('height', 'auto', 'important');
    el.style.setProperty('max-height', 'none', 'important');
    el.style.setProperty('min-height', '0', 'important');
    if (cs.overflowY === 'hidden' || cs.overflow === 'hidden' || cs.overflow === 'clip')
      el.style.setProperty('overflow', 'visible', 'important');
    if (cs.position === 'fixed') el.style.setProperty('position', 'static', 'important');
    n++;
  }
  return n;
}"""


async def reveal_full_content(page, monitor: Monitor) -> None:
    """Release leftover scroll-locks and force lazy media to load so a full-page
    screenshot captures the whole page, not a tall mostly-blank image. Best-effort;
    runs on the main frame + same-origin child frames, then waits briefly for the
    now-eager images to fetch. No-op when the monitor opts out."""
    if not getattr(monitor, "block_annoyances", True):
        return
    for frame in page.frames:
        try:
            await frame.evaluate(_REVEAL_CONTENT_JS)
        except Exception:
            pass
    try:
        await page.wait_for_timeout(900)   # let the now-eager lazy images load
    except Exception:
        pass


async def replay_login(page, monitor: Monitor) -> None:
    """Run the monitor's login flow steps, substituting decrypted secrets."""
    flow = monitor.login_flow
    if not flow or not flow.steps:
        return
    secrets = resolve_secrets(flow)
    for step in flow.steps:
        action = step.get("action")
        if action == "goto":
            await page.goto(step["url"], wait_until="networkidle")
        elif action == "fill":
            value = secrets.get(step.get("secret", ""), step.get("value", ""))
            await page.fill(step["selector"], value)
        elif action == "click":
            await page.click(step["selector"])
        elif action == "wait":
            if step.get("selector"):
                await page.wait_for_selector(step["selector"])
            else:
                await page.wait_for_timeout(int(step.get("ms", 1000)))


async def apply_actions(page, monitor: Monitor) -> None:
    """Apply optional pre-capture actions (scroll, click, dismiss banners)."""
    for act in monitor.actions or []:
        kind = act.get("action")
        if kind == "scroll":
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(300)
        elif kind == "click":
            try:
                await page.click(act["selector"], timeout=3000)
            except Exception:
                pass
        elif kind == "dismiss":
            for sel in act.get("selectors", []):
                try:
                    await page.click(sel, timeout=1500)
                except Exception:
                    pass
        elif kind == "wait":
            await page.wait_for_timeout(int(act.get("ms", 500)))


async def _settle_for_content(page, *, min_chars: int = 150, timeout_ms: int = 9000) -> None:
    """Wait until the page has a meaningful amount of visible text, so we don't
    capture a transient anti-bot challenge / loading screen instead of content."""
    try:
        await page.wait_for_function(
            "(n) => ((document.body && document.body.innerText) || '').trim().length >= n",
            arg=min_chars,
            timeout=timeout_ms,
        )
    except Exception:
        pass


async def _settle_for_render(page, *, timeout_ms: int = 12000) -> None:
    """Wait until the page has rendered real content — either meaningful visible
    text OR a populated DOM. More tolerant than `_settle_for_content` for SPA /
    shadow-DOM pages (e.g. modern e-commerce) whose `innerText` stays low even when
    fully rendered, so the mobile pass doesn't screenshot a half-hydrated page."""
    try:
        await page.wait_for_function(
            """() => {
              const b = document.body; if (!b) return false;
              const text = ((b.innerText || '')).trim().length;
              const els = b.querySelectorAll('*').length;
              return text >= 150 || els >= 60;
            }""",
            timeout=timeout_ms,
        )
    except Exception:
        pass


async def mobile_screenshot_or_none(page) -> bytes | None:
    """Screenshot the current (mobile-viewport) page, unless it's an anti-bot
    challenge interstitial. Shared by both engines' mobile pass so they behave
    identically.

    We only reach the mobile pass after the desktop pass already succeeded and
    wasn't anti-bot-walled, using the same cookies — so the mobile page is real
    content unless it's specifically a challenge page. We therefore reject ONLY a
    recognised challenge interstitial, never on a low `innerText` alone: SPA
    hydration is async and shadow-DOM text isn't counted by `innerText`, so a
    perfectly-rendered product page can momentarily read as empty. Gating on the
    text length (the old behaviour) intermittently discarded good mobile captures,
    leaving the UI to fall back to the desktop image."""
    if _looks_blocked(None, await _body_text(page)):
        return None
    return await full_page_png(page)


async def mobile_sections_or_none(page) -> list[bytes] | None:
    """Like `mobile_screenshot_or_none`, but returns the whole-page sections (or None
    if the page looks like a challenge interstitial)."""
    if _looks_blocked(None, await _body_text(page)):
        return None
    return await full_page_sections(page)


# Generic content-block extractor (no per-site selectors): find repeated-sibling
# groups (>=3 children sharing a tag+class signature) whose VISIBLE items carry a
# meaningful amount of text — i.e. list/feed items like reviews, job rows, product
# cards, articles. Returns each item's text + full-PAGE bounding box (doc coords, CSS
# px) plus the page dimensions / devicePixelRatio, so a later step can locate "which
# item changed" by content and crop it from this render's own screenshot. Bounded.
_ELEMENT_MAP_JS = r"""() => {
  const norm = t => (t || '').replace(/\s+/g, ' ').trim();
  const sig = el => el.tagName.toLowerCase() + '|' +
      ((el.className || '').toString().trim().split(/\s+/).slice(0, 2).join('.'));
  const vis = el => { let cs; try { cs = getComputedStyle(el); } catch (e) { return false; }
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 40 && r.height >= 18; };
  const sx = window.scrollX || 0, sy = window.scrollY || 0;
  const out = []; const seen = new WeakSet();
  for (const parent of document.querySelectorAll('div,ul,ol,section,main,table,tbody,article')) {
    const kids = parent.children;
    if (kids.length < 3) continue;
    const groups = {};
    for (const k of kids) { const g = sig(k); (groups[g] = groups[g] || []).push(k); }
    for (const g of Object.values(groups)) {
      if (g.length < 3) continue;
      for (const item of g) {
        if (seen.has(item) || !vis(item)) continue;
        seen.add(item);
        const text = norm(item.innerText || item.textContent || '');
        if (text.length < 25 || text.length > 2000) continue;
        const r = item.getBoundingClientRect();
        out.push({ t: text.slice(0, 600),
                   x: Math.round(r.left + sx), y: Math.round(r.top + sy),
                   w: Math.round(r.width), h: Math.round(r.height) });
        if (out.length >= 400) break;
      }
      if (out.length >= 400) break;
    }
    if (out.length >= 400) break;
  }
  return { pw: Math.round(document.documentElement.scrollWidth),
           ph: Math.round(document.documentElement.scrollHeight),
           dpr: window.devicePixelRatio || 1, blocks: out };
}"""


# Plausible common phone CSS viewport sizes (logical px). Randomising the mobile
# capture size per check avoids a FIXED mobile fingerprint — a stealth tell for
# Camoufox, whose desktop window is already randomised. Safe because change
# detection/localization is content-anchored (DOM), not pixel-aligned, so a varying
# mobile width doesn't break anything.
_MOBILE_SIZES = ((360, 800), (375, 812), (390, 844), (393, 852), (412, 915), (414, 896))


def random_mobile_size() -> tuple[int, int]:
    return random.choice(_MOBILE_SIZES)


async def capture_element_map(page) -> dict | None:
    """Capture a compact, content-anchored map of the page's repeated content blocks
    with their full-page bounding boxes (this render's own coordinates). Generic — no
    per-site selectors. Best-effort; returns None on failure or an empty page.

    Shape: {"pw","ph","dpr", "blocks":[{"k": content-key, "x","y","w","h", "s": snippet}]}
    where the key is a digit-masked text hash so counts/dates that tick every load
    don't make an otherwise-identical block look new.
    """
    import hashlib
    import re

    async def _extract():
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
        try:
            return await page.evaluate(_ELEMENT_MAP_JS)
        except Exception:
            return None

    # Retry once after a short beat if the first pass finds nothing — the repeated
    # content (reviews/listings/cards) may still be hydrating, especially on the
    # separately-rendered mobile pass. Cheap: only the empty case pays the wait.
    data = await _extract()
    if not (isinstance(data, dict) and data.get("blocks")):
        try:
            await page.wait_for_timeout(800)
        except Exception:
            pass
        data = await _extract()
    if not isinstance(data, dict) or not data.get("blocks"):
        return None
    blocks = []
    for b in data["blocks"]:
        t = b.get("t") or ""
        key = hashlib.sha1(re.sub(r"\d+", "#", t.lower()).encode("utf-8")).hexdigest()[:12]
        blocks.append({"k": key, "x": b.get("x"), "y": b.get("y"),
                       "w": b.get("w"), "h": b.get("h"), "s": t[:160]})
    return {"pw": data.get("pw"), "ph": data.get("ph"), "dpr": data.get("dpr"), "blocks": blocks}


async def navigate(page, monitor: Monitor):
    """Navigate to the monitor URL, tolerating a wait condition that never settles.

    `networkidle` (a very common setting) frequently never fires on ad/tracker-
    heavy sites — analytics, ads and long-poll connections keep the network busy —
    so a strict goto times out at `wait_timeout_ms` even though the page loaded
    fine in a fraction of a second. Rather than failing the whole check, fall back
    to `domcontentloaded` and capture what loaded. `do_wait`/`_settle_for_content`
    downstream still wait for real content. Returns the navigation response (or
    None if even the fallback couldn't produce one)."""
    from playwright.async_api import TimeoutError as PWTimeout
    try:
        return await page.goto(monitor.url, wait_until=monitor.wait_until,
                               timeout=monitor.wait_timeout_ms)
    except PWTimeout:
        if monitor.wait_until == "domcontentloaded":
            raise  # already the most lenient wait — a genuine navigation failure
        try:
            return await page.goto(monitor.url, wait_until="domcontentloaded",
                                   timeout=monitor.wait_timeout_ms)
        except PWTimeout:
            return None  # last resort: proceed with whatever the page already has


async def do_wait(page, monitor: Monitor) -> None:
    if monitor.wait_selector:
        try:
            await page.wait_for_selector(monitor.wait_selector, timeout=monitor.wait_timeout_ms)
        except Exception:
            pass


# Signatures of anti-bot interstitials that a clearance cookie typically clears.
_CHALLENGE_TEXT = (
    "humans only", "just a moment", "checking your browser",
    "verify you are human", "cf-browser-verification", "enable javascript and cookies",
)


async def _body_text(page) -> str:
    try:
        return await asyncio.wait_for(
            page.evaluate("() => (document.body && document.body.innerText) || ''"),
            timeout=3,
        )
    except Exception:
        return ""


def _looks_blocked(response, body_text: str) -> bool:
    """A cold deep-link hit an anti-bot wall (HTTP 401/403/429 or a known
    challenge interstitial) rather than real content."""
    if response is not None and getattr(response, "status", None) in (401, 403, 429):
        return True
    low = (body_text or "").lower()
    # Short page + a challenge phrase → interstitial, not content.
    return len(low) < 1500 and any(sig in low for sig in _CHALLENGE_TEXT)


async def warm_up_if_blocked(page, response, monitor: Monitor):
    """Recover a cold deep-link that anti-bot turned away.

    Many sites (Cloudflare, and Glassdoor's review pagination) reject a request
    that lands straight on a deep URL with an empty cookie jar, but serve the
    same page once the browser holds a clearance cookie obtained from the site
    root. If the first navigation looks blocked, visit the origin to pick up
    that cookie, then retry the target with a same-site referer (in the SAME
    context, so the cookie carries over). Returns the new response on a retry,
    else None. Never raises — a failed warm-up just leaves the original result.
    """
    if not _looks_blocked(response, await _body_text(page)):
        return None
    u = urlparse(monitor.url)
    if not u.scheme or not u.netloc:
        return None
    origin = f"{u.scheme}://{u.netloc}/"
    if origin.rstrip("/") == monitor.url.rstrip("/"):
        return None  # the target IS the root — nothing to warm up from
    try:
        await page.goto(origin, wait_until=monitor.wait_until, timeout=monitor.wait_timeout_ms)
        await asyncio.sleep(1.5)
        return await page.goto(
            monitor.url, wait_until=monitor.wait_until,
            timeout=monitor.wait_timeout_ms, referer=origin,
        )
    except Exception:
        return None


# WebP rejects any image with a side longer than this — so even after the megapixel
# downscale, a very long (document-like) page must be clamped to it or the encode fails.
_WEBP_MAX_DIM = 16383


async def _settle_for_paint(page) -> None:
    """Nudge the page to a painted state before capture: top of page, fonts loaded, a
    couple of animation frames committed. (Tall pages are captured by scrolling strips
    in `_capture_full_png`, which forces per-region paint on its own.)"""
    try:
        await page.evaluate(
            """async () => {
              window.scrollTo(0, 0);
              if (document.fonts && document.fonts.ready) { try { await document.fonts.ready; } catch (e) {} }
              await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
            }""")
        await page.wait_for_timeout(150)
    except Exception:
        pass


def _whiteness(webp_bytes: bytes) -> float:
    """Fraction of a capture that is ~white (cheap blank-detector). 0.0 on error."""
    try:
        from io import BytesIO

        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(BytesIO(webp_bytes)).convert("RGB")
        im.thumbnail((48, 48))
        px = list(im.getdata())
        return sum(1 for q in px if min(q) > 245) / max(1, len(px))
    except Exception:
        return 0.0


def _stitch_strips(strips: dict, dpr: float) -> bytes:
    """Stitch viewport strips ({scrollY_css: png_bytes}) into one full-page PNG."""
    from io import BytesIO

    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    items = sorted((int(y), Image.open(BytesIO(b)).convert("RGB")) for y, b in strips.items())
    if not items:
        return b""
    w = items[0][1].width
    h = max(int(round(y * dpr)) + im.height for y, im in items)
    # Clamp the canvas to the same hard height cap _slice_sections applies AFTER
    # stitching — otherwise a pathological tall page allocates the full (e.g.
    # ~1.8 GB) canvas here before the crop, OOM-ing the box under render concurrency.
    hard_cap = int((settings.max_screenshot_height_px or 0) * dpr)
    if hard_cap and h > hard_cap:
        h = hard_cap
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    for y, im in items:
        top = int(round(y * dpr))
        if top >= h:
            continue                      # strip starts past the cap — skip
        canvas.paste(im, (0, top))        # PIL clips any overhang at the canvas edge
    out = BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


# Above this device-pixel height a single full_page screenshot blanks out in Chromium
# (the rasteriser can't paint the whole tall page at once), so we scroll + stitch.
_TALL_CAPTURE_PX = 10000


async def _capture_full_png(page, dpr: float) -> bytes:
    """Capture the whole page as one PNG. A `full_page` screenshot of a very tall page
    comes back mostly blank (Chromium raster limit) even though every viewport paints
    fine, so for tall pages we scroll a (tall, where the engine allows it) viewport down
    in strips, screenshot each, and stitch. Short pages take the fast full_page path."""
    try:
        info = await page.evaluate(
            "() => ({h: Math.ceil(document.documentElement.scrollHeight),"
            " w: Math.ceil(document.documentElement.scrollWidth),"
            " vw: window.innerWidth, vh: window.innerHeight})")
    except Exception:
        info = {}
    H = int(info.get("h") or 0)
    vw = int(info.get("vw") or 1280)
    vh0 = int(info.get("vh") or 800) or 800
    if H <= 0 or H * dpr <= _TALL_CAPTURE_PX:
        return await page.screenshot(full_page=True, type="png")   # short → fast path

    # Use a tall viewport to cut the strip count where the engine allows resizing
    # (Playwright); Camoufox forbids it, so fall back to the current viewport height.
    chunk = vh0
    resized = False
    try:
        await page.set_viewport_size({"width": vw, "height": 8000})
        await page.wait_for_timeout(150)
        chunk = 8000
        resized = True
    except Exception:
        chunk = vh0
    strips: dict = {}
    try:
        H = int(await page.evaluate("() => Math.ceil(document.documentElement.scrollHeight)")) or H
        y = 0
        while y < H and len(strips) < 60:
            try:
                await page.evaluate("(v) => window.scrollTo(0, v)", y)
                await page.wait_for_timeout(110)
                ay = int(await page.evaluate("() => Math.round(window.scrollY)"))
            except Exception:
                ay = y
            if ay not in strips:
                strips[ay] = await page.screenshot(type="png")
            if ay + chunk >= H:
                break
            y = ay + chunk
        try:
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass
    finally:
        if resized:
            try:
                await page.set_viewport_size({"width": vw, "height": vh0})
            except Exception:
                pass
    if not strips:
        return await page.screenshot(full_page=True, type="png")
    return await asyncio.to_thread(_stitch_strips, strips, dpr)


async def full_page_sections(page) -> list[bytes]:
    """Capture the WHOLE page as an ordered list of readable, full-width sections.

    The page is captured with `full_page=True` (which correctly scrolls/stitches long
    pages — a `clip` without it is silently constrained to the viewport, which used to
    truncate any page taller than the cap to one screen), then sliced top-to-bottom
    into bands of `screenshot_section_height_px` CSS px. Each band is downscaled to the
    megapixel budget + WebP's dimension limit and encoded — so the full page is kept,
    each piece stays sharp at full width, and nothing is cut off. Stacking the sections
    reconstructs the whole page. Bytes may be WebP (Pillow reads either).

    Heavy pages occasionally capture before they paint (all text in the DOM, screen
    still white). We settle for paint first, and if the capture still comes out blank
    while the page clearly has content, we re-capture once after a longer settle."""
    async def _shot(dpr):
        raw = await _capture_full_png(page, max(dpr, 1.0))
        return await asyncio.to_thread(_slice_sections, raw, max(dpr, 1.0))

    try:
        dpr = float(await page.evaluate("() => window.devicePixelRatio || 1"))
    except Exception:
        dpr = 1.0
    await _settle_for_paint(page)
    secs = await _shot(dpr)
    # Paint-race guard: near-blank capture but the page has real text → re-shoot once.
    try:
        chars = await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').trim().length")
    except Exception:
        chars = 0
    if secs and chars >= 500:
        white = await asyncio.to_thread(_whiteness, secs[0])
        if white >= 0.92:
            await page.wait_for_timeout(900)
            await _settle_for_paint(page)
            retry = await _shot(dpr)
            if retry and (await asyncio.to_thread(_whiteness, retry[0])) < white:
                secs = retry
    return secs


async def full_page_png(page) -> bytes:
    """The single top section of the full page — back-compat for callers/thumbnails
    that want one image. See `full_page_sections` for the whole-page capture."""
    secs = await full_page_sections(page)
    return secs[0] if secs else b""


def _encode_webp(im) -> bytes:
    """Downscale a PIL image to the megapixel budget, clamp to WebP's dimension limit,
    and encode as WebP bytes."""
    from io import BytesIO

    from PIL import Image
    w, h = im.width, im.height
    scale = 1.0
    budget = (settings.max_screenshot_megapixels or 0) * 1_000_000
    if budget and w * h > budget:             # retina/long captures → fit the budget
        scale = (budget / (w * h)) ** 0.5
    longest = max(w, h)
    if longest * scale > _WEBP_MAX_DIM:       # …and stay within WebP's dimension limit
        scale = min(scale, _WEBP_MAX_DIM / longest)
    if scale < 1.0:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    out = BytesIO()
    im.convert("RGB").save(out, format="WEBP",
                           quality=settings.screenshot_webp_quality, method=4)
    return out.getvalue()


def _slice_sections(png: bytes, dpr: float) -> list[bytes]:
    """Slice a full-page PNG into readable, full-width WebP sections (CPU-bound — run
    off the event loop). Falls back to [png] on any error."""
    try:
        from io import BytesIO

        from PIL import Image
        # Our own screenshots, not untrusted input — Pillow's decompression-bomb guard
        # is a false positive on tall document captures.
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(BytesIO(png))
        im.load()
    except Exception:
        return [png]
    try:
        # Overall safety crop (memory bound) before slicing.
        hard_cap = int((settings.max_screenshot_height_px or 0) * dpr)
        if hard_cap and im.height > hard_cap:
            im = im.crop((0, 0, im.width, hard_cap))
        band = int((settings.screenshot_section_height_px or 0) * dpr)
        max_n = settings.max_screenshot_sections or 1
        if band <= 0 or im.height <= band:    # short page → one section
            return [_encode_webp(im)]
        sections: list[bytes] = []
        y = 0
        while y < im.height and len(sections) < max_n:
            sections.append(_encode_webp(im.crop((0, y, im.width, min(y + band, im.height)))))
            y += band
        return sections or [_encode_webp(im)]
    except Exception:
        try:
            return [_encode_webp(im)]
        except Exception:
            return [png]


# Back-compat alias: a few call sites and tests import this name.
def _compress_screenshot(png: bytes, max_height_px: int = 0) -> bytes:
    """Single-image compress (top section). Retained for back-compat."""
    from io import BytesIO

    from PIL import Image
    if not settings.max_screenshot_megapixels:
        return png
    try:
        Image.MAX_IMAGE_PIXELS = None
        im = Image.open(BytesIO(png))
        im.load()
        if max_height_px and im.height > max_height_px:
            im = im.crop((0, 0, im.width, max_height_px))
        return _encode_webp(im)
    except Exception:
        return png


async def capture(page, response, monitor: Monitor, mobile: bool = True) -> RenderResult:
    """Capture HTML, visible text, screenshot, and selector/JSON value.

    `mobile` controls whether a second mobile-viewport screenshot is taken by
    resizing the page. Camoufox forbids runtime viewport resizing, so it passes
    mobile=False and captures the mobile view via a separate instance instead.
    """
    result = RenderResult()
    result.http_status = response.status if response else None
    result.content_type = (
        (response.headers.get("content-type", "") if response else "") or ""
    ).split(";")[0].strip()

    # JSON endpoints: capture the parsed body as the value, skip screenshot.
    if "json" in (result.content_type or ""):
        body = await page.content()
        try:
            text = await response.text() if response else body
            parsed = _json.loads(text)
            result.rendered_text = _json.dumps(parsed, indent=2, sort_keys=True)
        except Exception:
            result.rendered_text = body
        result.html = body
        return result

    # Wait for meaningful content to appear before capturing. This handles
    # anti-bot challenge interstitials (DataDome/Cloudflare) that swap in the
    # real page via JS a moment after load, as well as late client rendering.
    await _settle_for_content(page)

    # Accept/dismiss via configured clicks first (reveals content behind hard
    # consent walls), then hide whatever banners remain — so they pollute neither
    # the captured text nor the screenshot / visual diff.
    await click_consent(page, monitor)
    # Keep dismissing banners as they mount (catches late CMPs during the rest of
    # capture), then do the first explicit sweep.
    await install_consent_autodismiss(page, monitor)
    try:
        await hide_banners(page, monitor)
    except Exception:
        pass

    try:
        result.title = (await page.title() or "").strip() or None
    except Exception:
        result.title = None

    # Strip ignored elements (ads, timestamps, etc.) from the live DOM so they
    # affect neither the text, the HTML, nor the screenshot.
    for sel in monitor.ignore_selectors or []:
        try:
            await page.evaluate(
                "(s) => document.querySelectorAll(s).forEach(e => e.remove())", sel
            )
        except Exception:
            pass

    result.html = await page.content()
    try:
        result.rendered_text = await page.evaluate(VISIBLE_TEXT_JS)
    except Exception:
        result.rendered_text = None

    # Element extraction for selector watches.
    if monitor.selector:
        try:
            el = await page.query_selector(monitor.selector)
            if el is not None:
                if monitor.selector_attr:
                    result.extracted_value = await el.get_attribute(monitor.selector_attr)
                else:
                    result.extracted_value = (await el.inner_text()).strip()
        except Exception:
            result.extracted_value = None

    # Final sweep right before the screenshot so a banner that mounted during
    # text extraction is gone from the image too. Its walls (any blocking banner
    # we could only hide, not accept) PLUS any large positioned overlay still
    # covering the page (a paywall/interstitial the heuristic couldn't dismiss)
    # feed the runner's optional AI dismiss-selector fallback.
    try:
        walls = await hide_banners(page, monitor) or []
        obstr = await detect_obstructions(page, monitor) or []
        merged = list(dict.fromkeys([*walls, *obstr]))[:2]
        result.unhandled_consent_html = merged or None
    except Exception:
        result.unhandled_consent_html = None

    # Reveal the full page (force lazy media, release leftover scroll-locks) so
    # the full-page screenshot isn't a tall mostly-blank image.
    await reveal_full_content(page, monitor)

    # Full-page screenshot at the desktop viewport, sliced into readable sections.
    try:
        result.screenshot_sections = await full_page_sections(page)
        result.screenshot_png = result.screenshot_sections[0] if result.screenshot_sections else None
    except Exception:
        result.screenshot_sections = None
        result.screenshot_png = None

    # Content-anchored element map at the desktop layout (this render's own coords) —
    # for later resolution-independent change localization. Captured here (after the
    # desktop screenshot, before any mobile resize) so its bboxes match that capture.
    try:
        result.element_map = await capture_element_map(page)
    except Exception:
        result.element_map = None

    # Second full-page screenshot at a mobile viewport, for device-appropriate
    # previews. Re-uses the already-loaded page (just resizes), so no extra
    # navigation. Responsive sites reflow via media queries.
    if not mobile:
        return result
    try:
        await page.set_viewport_size(
            {"width": settings.mobile_viewport_width, "height": settings.mobile_viewport_height}
        )
        await page.wait_for_timeout(450)
        await reveal_full_content(page, monitor)   # re-reveal at the mobile size
        result.screenshot_mobile_sections = await full_page_sections(page)
        result.screenshot_mobile_png = (
            result.screenshot_mobile_sections[0] if result.screenshot_mobile_sections else None)
    except Exception:
        result.screenshot_mobile_sections = None
        result.screenshot_mobile_png = None
    finally:
        try:
            await page.set_viewport_size(
                {"width": monitor.viewport_width, "height": monitor.viewport_height}
            )
        except Exception:
            pass

    return result
