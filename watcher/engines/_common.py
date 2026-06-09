"""Page-level operations shared by Playwright and Camoufox.

Both engines expose a Playwright `Page`, so login replay, waiting, actions,
and capture are identical regardless of the underlying browser.
"""

from __future__ import annotations

import json as _json

from ..auth.login_flows import resolve_secrets
from ..models import Monitor
from .base import VISIBLE_TEXT_JS, RenderResult


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


async def capture(page, response, monitor: Monitor) -> RenderResult:
    """Capture HTML, visible text, screenshot, and selector/JSON value."""
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

    # Full-page screenshot at the desktop viewport (PNG).
    try:
        result.screenshot_png = await page.screenshot(full_page=True, type="png")
    except Exception:
        result.screenshot_png = None

    # Second full-page screenshot at a mobile viewport, for device-appropriate
    # previews. Re-uses the already-loaded page (just resizes), so no extra
    # navigation. Responsive sites reflow via media queries.
    try:
        await page.set_viewport_size({"width": 390, "height": 844})
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
