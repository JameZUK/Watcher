"""Rendering engines. Resolve a Renderer for a monitor's configured engine."""

from __future__ import annotations

from ..models import Engine, Monitor
from .base import RenderResult, Renderer
from .camoufox import CamoufoxRenderer
from .playwright_engine import PlaywrightRenderer

__all__ = ["RenderResult", "Renderer", "get_renderer", "render_monitor"]


def get_renderer(engine: Engine) -> Renderer:
    if engine == Engine.camoufox:
        return CamoufoxRenderer()
    return PlaywrightRenderer(browser=engine.value)


async def render_monitor(monitor: Monitor) -> RenderResult:
    """Render a monitor with its configured engine."""
    renderer = get_renderer(monitor.engine)
    return await renderer.render(monitor)
