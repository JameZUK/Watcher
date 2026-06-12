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
from .web.routes import (
    account,
    admin,
    api,
    auth,
    changes,
    dashboard,
    groups,
    monitors,
    settings_routes,
    status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _host_only(value: str) -> str:
    """Extract a lowercased hostname from a URL or ``//host:port`` form,
    correctly handling IPv6 brackets and ports (for the CSRF host comparison)."""
    from urllib.parse import urlsplit
    try:
        return (urlsplit(value).hostname or "").lower()
    except ValueError:
        return ""


def _port_of(value: str) -> int | None:
    """The explicit port in a URL / ``//host:port`` form, or None when implicit."""
    from urllib.parse import urlsplit
    try:
        return urlsplit(value).port
    except ValueError:
        return None


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

    # NB: @app.middleware runs in REVERSE registration order (last = outermost).
    # Define csrf → body-size → security-headers so execution is
    # security-headers(outer) → body-size → csrf → route — i.e. error responses
    # (403/413) still get the security headers, and body-size runs before csrf.

    @app.middleware("http")
    async def _csrf_origin_guard(request: Request, call_next):
        """CSRF defense for cookie-authed state changes. Fails CLOSED: a browser
        always sends Origin (or at least Referer) on a cross-origin unsafe
        request, so a cookie-authed POST/PUT/PATCH/DELETE with neither header, or
        with a mismatched host, is rejected. The /api/* routes are token-authed
        (no ambient session cookie) so they're exempt."""
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.url.path.startswith("/api/"):
            src = request.headers.get("origin") or request.headers.get("referer")
            src_host = _host_only(src) if src else ""
            host_hdr = "//" + request.headers.get("host", "")
            # Host header hostname (bracket/port-safe) + operator-configured hosts.
            allowed = {_host_only(host_hdr)} | settings.trusted_host_set
            ok = bool(src_host) and src_host in allowed
            # A different port on the same host is a different origin (e.g. another
            # app on localhost:9000 forging requests). When the Host we received
            # carries an explicit port and the request's Origin does too, require a
            # match. Proxied deployments forward a port-less Host, so this is a no-op
            # there (and operator-trusted hosts aren't port-restricted).
            if ok and src_host == _host_only(host_hdr):
                sp, hp = _port_of(src), _port_of(host_hdr)
                if sp is not None and hp is not None and sp != hp:
                    ok = False
            if not ok:
                return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        return await call_next(request)

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
        elif request.method in ("POST", "PUT", "PATCH") and "chunked" in request.headers.get(
                "transfer-encoding", "").lower():
            # No Content-Length to check → would bypass the cap. This app has no
            # streaming-upload endpoints, so require a declared length.
            return JSONResponse({"detail": "Length Required."}, status_code=411)
        return await call_next(request)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Defensive response headers (clickjacking, MIME-sniffing, referrer leak,
        and HSTS when served over TLS)."""
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        # CSP: locks down object/base/frame and the resource origins. script/style
        # keep 'unsafe-inline'/'unsafe-eval' because the UI uses the Tailwind CDN
        # (a JIT runtime) + inline Alpine — autoescape remains the primary XSS
        # defence; this adds clickjacking/base-uri/object-src hardening on top.
        resp.headers.setdefault("Content-Security-Policy", (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.tailwindcss.com https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        ))
        if settings.secure_cookies:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Don't let the browser serve a stale authenticated HTML page (avoids
        # confusing "old UI" caching; static assets/images set their own caching).
        if resp.headers.get("content-type", "").startswith("text/html"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(monitors.router)
    app.include_router(groups.router)
    app.include_router(changes.router)
    app.include_router(settings_routes.router)
    app.include_router(account.router)
    app.include_router(admin.router)
    app.include_router(api.router)
    app.include_router(status.router)

    @app.exception_handler(StarletteHTTPException)
    async def _auth_redirect(request: Request, exc: StarletteHTTPException):
        # Honour an explicit redirect (e.g. the force-2FA gate → /account).
        loc = (exc.headers or {}).get("Location")
        if loc:
            return RedirectResponse(url=loc, status_code=303)
        # Redirect unauthenticated browser requests to the login page.
        if exc.status_code == 401 and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/login", status_code=303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
