"""Page-level operations shared by Playwright and Camoufox.

Both engines expose a Playwright `Page`, so login replay, waiting, actions,
and capture are identical regardless of the underlying browser.
"""

from __future__ import annotations

import json as _json
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
    """Abort ad/tracker network requests for this render (set up on the context
    BEFORE navigation). Cookie banners are hidden separately in capture() — after
    load — via hide_banners(). No-op when the monitor opts out."""
    if not getattr(monitor, "block_annoyances", True):
        return

    async def _route(route):
        try:
            host = (urlparse(route.request.url).hostname or "").lower()
            if host and any(host == d or host.endswith("." + d) for d in _BLOCK_HOSTS):
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


async def do_wait(page, monitor: Monitor) -> None:
    if monitor.wait_selector:
        try:
            await page.wait_for_selector(monitor.wait_selector, timeout=monitor.wait_timeout_ms)
        except Exception:
            pass


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

    # Full-page screenshot at the desktop viewport (PNG).
    try:
        result.screenshot_png = await page.screenshot(full_page=True, type="png")
    except Exception:
        result.screenshot_png = None

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
        result.screenshot_mobile_png = await page.screenshot(full_page=True, type="png")
    except Exception:
        result.screenshot_mobile_png = None
    finally:
        try:
            await page.set_viewport_size(
                {"width": monitor.viewport_width, "height": monitor.viewport_height}
            )
        except Exception:
            pass

    return result
