"""Camoufox-backed renderer (stealth Firefox) for anti-bot targets.

Camoufox forbids resizing a live viewport (it locks the window size at launch
for fingerprint consistency). So the desktop pass runs with Camoufox's normal
stealth behaviour, and the mobile preview is captured by a *second* Camoufox
instance launched at a mobile window size with a `no_viewport` context — the
only supported way to control Camoufox's render size. The mobile pass is
best-effort: if it fails or is blocked, the desktop capture is unaffected.
"""

from __future__ import annotations

import time

from ..auth.login_flows import mark_session, session_is_valid
from ..config import settings
from ..models import Monitor
from ._common import (
    _settle_for_content,
    apply_actions,
    capture,
    do_wait,
    replay_login,
    setup_blocking,
)
from .base import RenderResult


class CamoufoxRenderer:
    async def render(self, monitor: Monitor) -> RenderResult:
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            return RenderResult(
                ok=False,
                error="Camoufox engine is not installed. Use the Docker image "
                      "(it bundles Camoufox) or run `pip install camoufox && python -m camoufox fetch`.",
            )

        start = time.monotonic()
        flow = monitor.login_flow
        reuse_session = session_is_valid(flow)

        launch_kwargs: dict = {"headless": True, "humanize": True}
        if monitor.proxy:
            launch_kwargs["proxy"] = {"server": monitor.proxy}

        result: RenderResult | None = None
        desktop_state: dict | None = None
        try:
            # --- Desktop pass: normal Camoufox stealth (gets past anti-bot) ---
            async with AsyncCamoufox(**launch_kwargs) as browser:
                context_kwargs: dict = {}
                if reuse_session and flow.session_state:
                    context_kwargs["storage_state"] = flow.session_state
                context = await browser.new_context(**context_kwargs)
                context.set_default_timeout(settings.render_timeout_seconds * 1000)
                page = await context.new_page()
                await setup_blocking(context, monitor)

                if flow and flow.steps and not reuse_session:
                    await replay_login(page, monitor)

                response = await page.goto(
                    monitor.url,
                    wait_until=monitor.wait_until,
                    timeout=monitor.wait_timeout_ms,
                )
                await do_wait(page, monitor)
                await apply_actions(page, monitor)

                # mobile=False: the in-page resize is a no-op on Camoufox.
                result = await capture(page, response, monitor, mobile=False)

                try:
                    desktop_state = await context.storage_state()
                    # Refresh persisted state whenever a flow exists (incl. pure
                    # cookie-injection flows), so injected clearance stays fresh.
                    if flow is not None:
                        mark_session(flow, desktop_state)
                        result.session_state = desktop_state
                except Exception:
                    pass

                try:
                    await context.close()
                except Exception:
                    pass

            # --- Mobile pass: separate instance at a mobile window size ---
            if result is not None and result.ok:
                try:
                    mobile_png = await self._capture_mobile(monitor, launch_kwargs, desktop_state)
                    if mobile_png:
                        result.screenshot_mobile_png = mobile_png
                except Exception:
                    pass

            if result is None:  # defensive — capture() never returns None today
                return RenderResult(ok=False, error="No content captured",
                                    render_ms=int((time.monotonic() - start) * 1000))
            result.render_ms = int((time.monotonic() - start) * 1000)
            return result
        except Exception as exc:  # noqa: BLE001
            if result is not None:
                result.render_ms = int((time.monotonic() - start) * 1000)
                return result
            return RenderResult(
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                render_ms=int((time.monotonic() - start) * 1000),
            )

    async def _capture_mobile(
        self, monitor: Monitor, launch_kwargs: dict, storage_state: dict | None
    ) -> bytes | None:
        """Capture a mobile-viewport screenshot via a dedicated Camoufox window.

        Camoufox honours the render size only when the size is fixed at launch
        (`window=`) AND the context uses `no_viewport=True`.
        """
        from camoufox.async_api import AsyncCamoufox

        w, h = settings.mobile_viewport_width, settings.mobile_viewport_height
        async with AsyncCamoufox(window=(w, h), **launch_kwargs) as browser:
            ctx_kwargs: dict = {"no_viewport": True}
            if storage_state:
                ctx_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**ctx_kwargs)
            context.set_default_timeout(settings.render_timeout_seconds * 1000)
            page = await context.new_page()
            await setup_blocking(context, monitor)
            await page.goto(
                monitor.url, wait_until=monitor.wait_until, timeout=monitor.wait_timeout_ms
            )
            await do_wait(page, monitor)
            await _settle_for_content(page)

            # Don't store a blocked/empty challenge page as the "mobile" preview.
            try:
                chars = await page.evaluate(
                    "() => ((document.body && document.body.innerText) || '').trim().length"
                )
            except Exception:
                chars = 0
            png = None
            if chars >= 100:
                png = await page.screenshot(full_page=True, type="png")

            try:
                await context.close()
            except Exception:
                pass
            return png
