"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import init_db
from .scheduler import schedule_all, start_scheduler, stop_scheduler
from .web import STATIC_DIR
from .web.routes import api, auth, changes, dashboard, monitors, settings_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    if settings.patch_playwright:
        from ._playwright_patch import apply as _patch_playwright
        logging.getLogger("watcher").info(_patch_playwright())
    await init_db()
    await schedule_all()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()


def create_app() -> FastAPI:
    app = FastAPI(title="Watcher", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(monitors.router)
    app.include_router(changes.router)
    app.include_router(settings_routes.router)
    app.include_router(api.router)

    @app.exception_handler(StarletteHTTPException)
    async def _auth_redirect(request: Request, exc: StarletteHTTPException):
        # Redirect unauthenticated browser requests to the login page.
        if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
