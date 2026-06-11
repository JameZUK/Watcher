"""Recursively-runnable test harness for the universal cookie/consent handler.

Renders a wide range of real sites — including Cloudflare/anti-bot-protected ones
(Glassdoor, JBL, StackOverflow) — through the ACTUAL engine helpers
(setup_blocking -> click_consent -> hide_banners) and reports, per site, whether
a visible consent overlay remained afterwards.

Uses Camoufox (stealth Firefox) by default so protected sites actually load; plain
Chromium gets served Cloudflare challenge pages and never reveals real content.

    PYTHONPATH=. .venv/bin/python scripts/test_consent.py            # camoufox
    PYTHONPATH=. .venv/bin/python scripts/test_consent.py chromium   # chromium
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import watcher.engines._common as C

# A deliberately broad range: GDPR/CCPA CMPs, static top bars, bottom bars,
# in-iframe CMPs, and Cloudflare/anti-bot-protected stores & job sites.
# A SECOND, deliberately different set: non-English EU sites (French/German/
# Spanish/Italian — guaranteed GDPR banners with NON-English accept buttons),
# heavy ad/CMP tabloids, EU retail, and more anti-bot targets. Wikipedia is a
# clean control that must stay untouched.
SITES = [
    "https://www.lemonde.fr",              # FR · Didomi ("Accepter")
    "https://www.lefigaro.fr",             # FR · own CMP
    "https://www.spiegel.de",              # DE · Sourcepoint ("Akzeptieren")
    "https://www.zeit.de",                 # DE · own CMP
    "https://www.bild.de",                 # DE · Sourcepoint
    "https://elpais.com",                  # ES · Didomi ("Aceptar")
    "https://www.repubblica.it",           # IT · ("Accetta")
    "https://www.dailymail.co.uk",         # UK tabloid · heavy ads + CMP
    "https://www.mirror.co.uk",            # UK · Reach/Sourcepoint
    "https://www.argos.co.uk",             # UK retail · OneTrust
    "https://www.currys.co.uk",            # UK retail
    "https://www.ikea.com/gb/en/",         # OneTrust full-screen
    "https://www.zalando.co.uk",           # EU fashion · Usercentrics
    "https://www.etsy.com",                # own CMP
    "https://www.nike.com",                # Akamai anti-bot
    "https://www.indeed.com",              # Cloudflare anti-bot
    "https://www.ticketmaster.co.uk",      # anti-bot + consent
    "https://www.cloudflare.com",          # own cookie banner
    "https://www.wikipedia.org",           # control: NO banner, must stay clean
]

# Mirrors the heuristic's candidate detection (positioned OR edge-anchored bar
# with an accept/dismiss button) so a STILL-visible consent bar is reported.
DETECT_JS = r"""() => {
  const consentRx = /(cookie|consent|gdpr|ccpa|we value your privacy|your privacy|tracking technolog|privacy|opt[- ]?out|data protection|datenschutz|privatsph|zustimmung|confidentialit|t[ée]moins|donn[ée]es|privacidad|consentimiento|riservatezza|privacidade|we and our( up to)?( \d+)? partner|store and\/or access|legitimate interest|manage (your )?(choices|preferences|consent)|personal data)/i;
  const acceptRx  = /(^(accept all|accept|agree|allow all|allow|got it|ok|okay|yes|continue|enable all)\b)|(\b(accepter|j'?accepte|tout accepter|akzeptieren|zustimmen|einverstanden|annehmen|aceptar|acepto|accetta|acconsento|aceitar|accepteren|akkoord|toestaan)\b)/i;
  const rejectRx  = /(reject|decline|deny|do not|refuse|manage|customi[sz]|preferenc|settings|without accept|continue without|reject all|refuser|g[ée]rer|param[èe]tr|ablehnen|einstellung|verwalten|rechazar|configurar|rifiuta|gestisci|weigeren|instellingen)/i;
  const dismissRx = /^(hide( this)?( message| cookie message)?|close|dismiss|no thanks|continue to (the )?(site|website)|fermer|schlie[sß]en|cerrar|chiudi|sluiten|×|✕|✖)$/i;
  const vis = (el) => { let cs; try{cs=getComputedStyle(el);}catch(e){return false;}
    if (cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity||'1')===0) return false;
    const r = el.getBoundingClientRect();
    return r.width>=80 && r.height>=28 && r.bottom>0 && r.top<innerHeight; };
  const hasBtn = (c,rx) => { for (const b of c.querySelectorAll('button,a[href],[role="button"],input[type="button"],input[type="submit"]')) {
      const br=b.getBoundingClientRect(); if(br.width<4||br.height<4)continue;
      const bt=((b.innerText||b.value||b.getAttribute('aria-label')||'')).trim();
      if (bt && bt.length<=40 && rx.test(bt)) return true; } return false; };
  const found=[];
  const sel='div,section,aside,dialog,form,[role="dialog"],[role="alertdialog"],[data-module*="cookie" i],[class*="cookie" i],[class*="consent" i],[id*="cookie" i],[id*="consent" i]';
  for (const el of document.querySelectorAll(sel)) {
    let cs; try{cs=getComputedStyle(el);}catch(e){continue;}
    if(!vis(el))continue;
    const t=(el.innerText||''); if(t.length<8||t.length>2200)continue;
    const r=el.getBoundingClientRect();
    const positioned = cs.position==='fixed'||cs.position==='sticky'||(cs.position==='absolute'&&parseInt(cs.zIndex||'0',10)>=50);
    const wide = r.width>=innerWidth*0.55;
    const edged = (r.top<=12||r.bottom>=innerHeight-12)&&r.top<innerHeight&&wide&&r.height<=innerHeight*0.5;
    const acc = hasBtn(el,acceptRx);
    // worded like consent, OR the accept-all + manage/reject CMP signature
    if(!(consentRx.test(t) || (acc && hasBtn(el,rejectRx)))) continue;
    const fills = (r.width*r.height) >= innerWidth*innerHeight*0.5;
    if(positioned) { /* ok */ }
    else if((edged||fills) && (acc||hasBtn(el,dismissRx))) { /* ok */ }
    else continue;
    found.push(t.replace(/\s+/g,' ').slice(0,60));
  }
  return [...new Set(found)].slice(0,4);
}"""


async def _detect_all_frames(page):
    found = []
    for frame in page.frames:
        try:
            found += await frame.evaluate(DETECT_JS)
        except Exception:
            pass
    return list(dict.fromkeys(found))[:4]


async def _content_chars(page):
    try:
        return await page.evaluate(
            "() => ((document.body && document.body.innerText) || '').trim().length"
        )
    except Exception:
        return 0


async def test_site(new_context, url, shot=None):
    m = SimpleNamespace(block_annoyances=True, consent_clicks=[])
    ctx = await new_context()
    try:
        await C.setup_blocking(ctx, m)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(4000)
        chars = await _content_chars(page)
        before = await _detect_all_frames(page)
        # Mirror the real capture sequence: click → install auto-dismiss observer
        # → sweep. Then give late-mounting CMPs (e.g. Le Figaro) time to appear so
        # the observer + a final sweep can clear them, and re-check.
        await C.click_consent(page, m)
        await C.install_consent_autodismiss(page, m)
        walls = await C.hide_banners(page, m)
        await page.wait_for_timeout(4000)        # let late CMPs mount
        walls = await C.hide_banners(page, m) or walls
        await page.wait_for_timeout(600)
        after = await _detect_all_frames(page)
        if shot:
            try:
                await page.screenshot(path=shot, full_page=False)
            except Exception:
                pass
        test_site.last_walls = walls
        if chars < 100:
            return "BLOCKED (no content)", before, after
        if after:                                 # banner present at the end — incl. late ones
            return "FAIL", before, after
        if not before:
            return "n/a (no banner)", before, after
        return "PASS", before, after
    except Exception as e:
        test_site.last_walls = None
        return f"ERROR {type(e).__name__}: {e}", [], []
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def run_camoufox(sites):
    from camoufox.async_api import AsyncCamoufox

    results = []
    async with AsyncCamoufox(headless=True, humanize=True) as browser:
        async def new_context():
            return await browser.new_context()
        for i, url in enumerate(sites):
            shot = f"/tmp/consent/{i:02d}.png"
            res, before, after = await test_site(new_context, url, shot=shot)
            results.append((res, url))
            print(f"{res:22} {url}   [shot:{shot}]")
            if after:
                print(f"    REMAINING: {after}")
            elif before:
                print(f"    (dismissed: {before[0]})")
            walls = getattr(test_site, "last_walls", None)
            if walls:
                print(f"    ⚠ WALL only HIDDEN (not accepted) — content may be gated: {walls[0][:70]}")
    return results


async def run_chromium(sites):
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        async def new_context():
            return await browser.new_context()
        for i, url in enumerate(sites):
            shot = f"/tmp/consent/{i:02d}.png"
            res, before, after = await test_site(new_context, url, shot=shot)
            results.append((res, url))
            print(f"{res:22} {url}   [shot:{shot}]")
            if after:
                print(f"    REMAINING: {after}")
            elif before:
                print(f"    (dismissed: {before[0]})")
            walls = getattr(test_site, "last_walls", None)
            if walls:
                print(f"    ⚠ WALL only HIDDEN (not accepted) — content may be gated: {walls[0][:70]}")
        await browser.close()
    return results


async def main():
    os.makedirs("/tmp/consent", exist_ok=True)
    engine = sys.argv[1] if len(sys.argv) > 1 else "camoufox"
    runner = run_chromium if engine == "chromium" else run_camoufox
    print(f"=== engine: {engine} ===")
    results = await runner(SITES)
    n_fail = sum(1 for r, _ in results if r == "FAIL")
    n_pass = sum(1 for r, _ in results if r == "PASS")
    n_na = sum(1 for r, _ in results if r.startswith("n/a"))
    n_blk = sum(1 for r, _ in results if r.startswith("BLOCKED"))
    n_err = sum(1 for r, _ in results if r.startswith("ERROR"))
    print(f"\nSUMMARY: {n_pass} pass · {n_fail} FAIL · {n_na} no-banner · "
          f"{n_blk} blocked · {n_err} error")
    if n_fail:
        print("FAILS:")
        for r, u in results:
            if r == "FAIL":
                print(f"  {u}")


if __name__ == "__main__":
    asyncio.run(main())
