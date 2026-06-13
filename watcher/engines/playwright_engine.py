"""Playwright-backed renderer (Chromium / Firefox / WebKit)."""

from __future__ import annotations

import time

from ..auth.login_flows import mark_session, session_is_valid
from ..config import settings
from ..models import Monitor
from ._common import (
    _settle_for_render,
    apply_actions,
    capture,
    capture_element_map,
    click_consent,
    do_wait,
    hide_banners,
    install_consent_autodismiss,
    mobile_sections_or_none,
    navigate,
    replay_login,
    reveal_full_content,
    setup_blocking,
    warm_up_if_blocked,
)
from .base import RenderResult
from ..proxy_pool import effective_proxy

# Mobile preview is captured in a dedicated, UA-emulated context (not a viewport
# resize) so sites that serve different markup to phones — Amazon, etc. — render
# their real mobile layout instead of desktop markup squashed to a narrow width.
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1"
)


class PlaywrightRenderer:
    def __init__(self, browser: str = "chromium") -> None:
        self.browser = browser

    async def render(self, monitor: Monitor) -> RenderResult:
        # Imported lazily so the package imports without browsers installed.
        from playwright.async_api import async_playwright

        start = time.monotonic()
        flow = monitor.login_flow
        reuse_session = session_is_valid(flow)

        _proxy = effective_proxy(monitor)
        proxy = {"server": _proxy} if _proxy else None

        # Captured before teardown so a close-time driver crash can't mask it.
        result: RenderResult | None = None
        try:
            async with async_playwright() as pw:
                browser_type = getattr(pw, self.browser)
                browser = await browser_type.launch(headless=True)
                try:
                    context_kwargs: dict = {
                        "viewport": {
                            "width": monitor.viewport_width,
                            "height": monitor.viewport_height,
                        },
                    }
                    if proxy:
                        context_kwargs["proxy"] = proxy
                    if reuse_session and flow.session_state:
                        context_kwargs["storage_state"] = flow.session_state

                    # Retina-crisp captures; fall back if the engine rejects it.
                    context_kwargs["device_scale_factor"] = settings.screenshot_scale
                    try:
                        context = await browser.new_context(**context_kwargs)
                    except Exception:
                        context_kwargs.pop("device_scale_factor", None)
                        context = await browser.new_context(**context_kwargs)
                    context.set_default_timeout(settings.render_timeout_seconds * 1000)
                    page = await context.new_page()
                    await setup_blocking(context, monitor)

                    if flow and flow.steps and not reuse_session:
                        await replay_login(page, monitor)

                    response = await navigate(page, monitor)
                    # Site-root warm-up to clear a cold-deep-link anti-bot wall.
                    warmed = await warm_up_if_blocked(page, response, monitor)
                    if warmed is not None:
                        response = warmed
                    await do_wait(page, monitor)
                    await apply_actions(page, monitor)

                    # Desktop capture only — the mobile preview is taken separately,
                    # with phone emulation, below.
                    result = await capture(page, response, monitor, mobile=False)

                    # Persist (refresh) session state whenever a login flow exists —
                    # not only for step-based logins. This keeps an injected
                    # clearance cookie (e.g. DataDome) rolling forward on every
                    # successful check instead of going stale.
                    mobile_state = context_kwargs.get("storage_state")
                    if flow is not None:
                        try:
                            state = await context.storage_state()
                            mark_session(flow, state)
                            result.session_state = state
                            mobile_state = state
                        except Exception:
                            pass

                    try:
                        await context.close()
                    except Exception:
                        pass

                    # Mobile preview in a phone-emulated context (best-effort).
                    if result is not None and result.ok and result.screenshot_png:
                        try:
                            msecs, mmap = await self._capture_mobile(browser, monitor, mobile_state)
                            if msecs:
                                result.screenshot_mobile_sections = msecs
                                result.screenshot_mobile_png = msecs[0]
                                result.element_map_mobile = mmap
                        except Exception:
                            pass
                finally:
                    # Always reap the browser PROCESS. The async_playwright context
                    # manager stops the driver, but a mid-render exception would
                    # otherwise leak the launched browser (memory/process growth).
                    try:
                        await browser.close()
                    except Exception:
                        pass

            result.render_ms = int((time.monotonic() - start) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            # If we already captured the page, a teardown error is non-fatal.
            if result is not None:
                result.render_ms = int((time.monotonic() - start) * 1000)
                return result
            return RenderResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                render_ms=int((time.monotonic() - start) * 1000),
            )

    async def _capture_mobile(self, browser, monitor: Monitor,
                              storage_state: dict | None) -> tuple[list[bytes] | None, dict | None]:
        """Capture a mobile-viewport screenshot (whole page, as sections) plus a
        content-anchored element map, in a phone-emulated context (mobile UA + touch
        where supported), re-navigating so UA-sensitive sites serve their real mobile
        layout. Returns (sections|None, element_map|None)."""
        base: dict = {
            "viewport": {"width": settings.mobile_viewport_width,
                         "height": settings.mobile_viewport_height},
            "user_agent": _MOBILE_UA,
            "device_scale_factor": settings.screenshot_scale,
        }
        _mproxy = effective_proxy(monitor)
        if _mproxy:
            base["proxy"] = {"server": _mproxy}
        if storage_state:
            base["storage_state"] = storage_state

        # Full mobile emulation (is_mobile/has_touch) is Chromium-only — fall back
        # to UA + viewport on Firefox/WebKit.
        context = None
        for extra in ({"is_mobile": True, "has_touch": True}, {}):
            try:
                context = await browser.new_context(**base, **extra)
                break
            except Exception:
                context = None
        if context is None:
            return None, None

        try:
            context.set_default_timeout(settings.render_timeout_seconds * 1000)
            page = await context.new_page()
            await setup_blocking(context, monitor)
            # Warm up past an anti-bot wall like the desktop pass — this is a fresh
            # navigation, so a cold deep-link can be gated even though desktop cleared
            # it, leaving the mobile render contentless (and its element map empty).
            resp = await navigate(page, monitor)
            warmed = await warm_up_if_blocked(page, resp, monitor)
            if warmed is not None:
                resp = warmed
            await do_wait(page, monitor)
            await _settle_for_render(page)
            await click_consent(page, monitor)
            await install_consent_autodismiss(page, monitor)
            await hide_banners(page, monitor)
            await reveal_full_content(page, monitor)
            # Capture unless it's a challenge interstitial (not on innerText alone —
            # SPA/shadow-DOM pages read as low-text even when fully rendered).
            secs = await mobile_sections_or_none(page)
            mmap = await capture_element_map(page) if secs else None
            return secs, mmap
        finally:
            try:
                await context.close()
            except Exception:
                pass
