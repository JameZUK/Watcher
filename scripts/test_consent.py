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
SITES = [
    "https://www.bbc.co.uk/news",          # BBC styled-component banner
    "https://www.theguardian.com/uk",      # Sourcepoint (cross-origin iframe)
    "https://www.independent.co.uk",       # Sourcepoint iframe
    "https://www.reuters.com",             # OneTrust / anti-bot
    "https://www.amazon.co.uk",            # bespoke top sheet
    "https://www.ebay.co.uk",              # bottom bar
    "https://www.gov.uk",                  # STATIC top banner (no position:fixed)
    "https://www.theverge.com",            # Concert/Vox CMP
    "https://www.imdb.com",                # bottom consent bar
    "https://stackoverflow.com",           # Cloudflare Turnstile
    "https://www.glassdoor.co.uk",         # Cloudflare-protected
    "https://www.glassdoor.com",           # Cloudflare-protected
    "https://uk.jbl.com",                  # Cloudflare-protected store
    "https://www.nytimes.com",             # Fides/own CMP
    "https://www.cnet.com",                # OneTrust
    "https://www.booking.com",             # bespoke
    "https://www.expedia.co.uk",           # OneTrust
    "https://www.target.com",              # anti-bot + consent
    "https://www.aboutcookies.org",        # control: a cookie-themed article (must NOT be hidden as a banner)
]

# Mirrors the heuristic's candidate detection (positioned OR edge-anchored bar
# with an accept/dismiss button) so a STILL-visible consent bar is reported.
DETECT_JS = r"""() => {
  const consentRx = /(cookie|consent|gdpr|ccpa|we value your privacy|your privacy|tracking technolog|privacy|opt[- ]?out|data protection)/i;
  const acceptRx  = /^(accept|agree|allow|got it|ok|okay|yes|i (accept|agree|understand)|understood|continue|enable all|allow all|accept all)\b/i;
  const dismissRx = /^(hide( this)?( message| cookie message)?|close|dismiss|no thanks|continue to (the )?(site|website)|×|✕|✖)$/i;
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
    if(!consentRx.test(t))continue;
    const r=el.getBoundingClientRect();
    const positioned = cs.position==='fixed'||cs.position==='sticky'||(cs.position==='absolute'&&parseInt(cs.zIndex||'0',10)>=50);
    const wide = r.width>=innerWidth*0.55;
    const edged = (r.top<=12||r.bottom>=innerHeight-12)&&r.top<innerHeight&&wide&&r.height<=innerHeight*0.5;
    if(!positioned && !(edged && (hasBtn(el,acceptRx)||hasBtn(el,dismissRx)))) continue;
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
        await C.click_consent(page, m)
        await C.hide_banners(page, m)
        await page.wait_for_timeout(800)
        after = await _detect_all_frames(page)
        if shot:
            try:
                await page.screenshot(path=shot, full_page=False)
            except Exception:
                pass
        if chars < 100:
            return "BLOCKED (no content)", before, after
        if not before:
            return "n/a (no banner)", before, after
        return ("PASS" if not after else "FAIL"), before, after
    except Exception as e:
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
