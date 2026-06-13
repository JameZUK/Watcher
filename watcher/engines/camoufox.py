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
    random_mobile_size,
    replay_login,
    reveal_full_content,
    setup_blocking,
    warm_up_if_blocked,
)
from .base import RenderResult
from ..proxy_pool import effective_proxy


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
        _proxy = effective_proxy(monitor)
        if _proxy:
            launch_kwargs["proxy"] = {"server": _proxy}

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

                response = await navigate(page, monitor)
                # A cold deep-link can hit an anti-bot wall (e.g. Glassdoor's
                # "Humans only"); a site-root warm-up that banks a clearance
                # cookie usually clears it. No-op when the first hit succeeded.
                warmed = await warm_up_if_blocked(page, response, monitor)
                if warmed is not None:
                    response = warmed
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
            # Skip it when the desktop pass was an anti-bot wall (401/403/429):
            # a second full Camoufox launch would just re-run the anti-bot gauntlet
            # to screenshot a block page — wasted time and extra block risk.
            if (result is not None and result.ok
                    and result.http_status not in (401, 403, 429)):
                try:
                    mobile_secs, mmap = await self._capture_mobile(monitor, launch_kwargs, desktop_state)
                    if mobile_secs:
                        result.screenshot_mobile_sections = mobile_secs
                        result.screenshot_mobile_png = mobile_secs[0]
                        result.element_map_mobile = mmap
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
    ) -> tuple[list[bytes] | None, dict | None]:
        """Capture a mobile-viewport screenshot (whole page, as sections) plus a
        content-anchored element map, via a dedicated Camoufox window. Returns
        (sections|None, element_map|None).

        Camoufox honours the render size only when the size is fixed at launch
        (`window=`) AND the context uses `no_viewport=True`. The size is RANDOMISED
        per check (a plausible phone size) so the mobile pass isn't a fixed-size
        fingerprint — safe because localization is content-anchored, not pixel-aligned.
        """
        from camoufox.async_api import AsyncCamoufox

        w, h = random_mobile_size()
        async with AsyncCamoufox(window=(w, h), **launch_kwargs) as browser:
            ctx_kwargs: dict = {"no_viewport": True}
            if storage_state:
                ctx_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**ctx_kwargs)
            context.set_default_timeout(settings.render_timeout_seconds * 1000)
            page = await context.new_page()
            await setup_blocking(context, monitor)
            # Warm up past an anti-bot wall the same way the desktop pass does — the
            # mobile pass is a fresh navigation, so a cold deep-link can be turned
            # away (Glassdoor/Cloudflare) even though desktop cleared it. Without this
            # the mobile render is a gate page → no content blocks → no element map.
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

            try:
                await context.close()
            except Exception:
                pass
            return secs, mmap
