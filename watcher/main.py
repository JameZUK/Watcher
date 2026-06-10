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
    if settings.secret_is_weak():
        msg = ("WATCHER_SECRET_KEY is unset/default — session cookies are forgeable and "
               "stored secrets are decryptable. Set a strong WATCHER_SECRET_KEY.")
        if settings.allow_insecure:
            logging.getLogger("watcher").critical("%s (continuing: WATCHER_ALLOW_INSECURE=1)", msg)
        else:
            raise RuntimeError(msg + " Refusing to start (set WATCHER_ALLOW_INSECURE=1 to override in dev).")
    if not settings.secure_cookies:
        logging.getLogger("watcher").warning(
            "WATCHER_SECURE_COOKIES is off — session cookies are sent over plain HTTP. "
            "Set it (and terminate TLS) before public exposure.")
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
    app = FastAPI(
        title="Watcher",
        lifespan=lifespan,
        # Don't expose the API surface to anonymous users unless explicitly enabled.
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie,
        max_age=settings.session_max_age,
        same_site="lax",
        https_only=settings.secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Defensive response headers (clickjacking, MIME-sniffing, referrer leak,
        and HSTS when served over TLS)."""
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if settings.secure_cookies:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    @app.middleware("http")
    async def _body_size_limit(request: Request, call_next):
        """Reject oversized request bodies before they're read/parsed (memory DoS)."""
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > settings.max_request_bytes:
                    return JSONResponse({"detail": "Request body too large."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)
        return await call_next(request)

    @app.middleware("http")
    async def _csrf_origin_guard(request: Request, call_next):
        """CSRF defense for cookie-authed state changes. Fails CLOSED: a browser
        always sends Origin (or at least Referer) on a cross-origin unsafe
        request, so a cookie-authed POST/PUT/PATCH/DELETE with neither header, or
        with a mismatched host, is rejected. The /api/* routes are token-authed
        (no ambient session cookie) so they're exempt."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.url.path.startswith("/api/"):
            from urllib.parse import urlparse
            src = request.headers.get("origin") or request.headers.get("referer")
            src_host = (urlparse(src).hostname or "").lower() if src else ""
            # Host header without port; plus any operator-configured public hosts.
            host_hdr = (request.headers.get("host", "")).split(":", 1)[0].lower()
            allowed = {host_hdr} | settings.trusted_host_set
            if src_host not in allowed or not src_host:
                return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        return await call_next(request)

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
