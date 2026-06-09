"""Camoufox-backed renderer (stealth Firefox) for anti-bot targets."""

from __future__ import annotations

import time

from ..auth.login_flows import mark_session, session_is_valid
from ..config import settings
from ..models import Monitor
from ._common import apply_actions, capture, do_wait, replay_login
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

        # `result` is captured *before* teardown so that a crash while closing
        # the browser (common with anti-bot challenge JS killing the driver)
        # never masks an otherwise-usable capture.
        result: RenderResult | None = None
        try:
            async with AsyncCamoufox(**launch_kwargs) as browser:
                context_kwargs: dict = {
                    "viewport": {
                        "width": monitor.viewport_width,
                        "height": monitor.viewport_height,
                    },
                }
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

                if flow and flow.steps:
                    try:
                        state = await context.storage_state()
                        mark_session(flow, state)
                        result.session_state = state
                    except Exception:
                        pass

                try:
                    await context.close()
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
