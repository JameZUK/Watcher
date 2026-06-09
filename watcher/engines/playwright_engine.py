"""Playwright-backed renderer (Chromium / Firefox / WebKit)."""

from __future__ import annotations

import time

from ..auth.login_flows import mark_session, session_is_valid
from ..config import settings
from ..models import Monitor
from ._common import apply_actions, capture, do_wait, replay_login
from .base import RenderResult


class PlaywrightRenderer:
    def __init__(self, browser: str = "chromium") -> None:
        self.browser = browser

    async def render(self, monitor: Monitor) -> RenderResult:
        # Imported lazily so the package imports without browsers installed.
        from playwright.async_api import async_playwright

        start = time.monotonic()
        flow = monitor.login_flow
        reuse_session = session_is_valid(flow)

        proxy = {"server": monitor.proxy} if monitor.proxy else None

        # Captured before teardown so a close-time driver crash can't mask it.
        result: RenderResult | None = None
        try:
            async with async_playwright() as pw:
                browser_type = getattr(pw, self.browser)
                browser = await browser_type.launch(headless=True)
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

                context = await browser.new_context(**context_kwargs)
                context.set_default_timeout(settings.render_timeout_seconds * 1000)
                page = await context.new_page()

                if flow and flow.steps and not reuse_session:
                    await replay_login(page, monitor)

                response = await page.goto(
                    monitor.url,
                    wait_until=monitor.wait_until,
                    timeout=monitor.wait_timeout_ms,
                )
                await do_wait(page, monitor)
                await apply_actions(page, monitor)

                result = await capture(page, response, monitor)

                # Persist session state if a login flow is configured.
                if flow and flow.steps:
                    try:
                        state = await context.storage_state()
                        mark_session(flow, state)
                        result.session_state = state
                    except Exception:
                        pass

                for closer in (context.close, browser.close):
                    try:
                        await closer()
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
