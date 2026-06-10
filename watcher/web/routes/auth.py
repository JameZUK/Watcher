"""Registration, login (with optional TOTP 2FA), logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...app_settings import get_app_settings
from ...auth.otp import user_secret, verify
from ...auth.users import authenticate, create_user, get_by_email
from ...config import settings
from ...db import get_session
from ...models import User
from .. import templates
from ..ratelimit import allow, client_ip, reset

router = APIRouter()


def _rate_ok(request: Request, scope: str) -> bool:
    return allow(f"{scope}:{client_ip(request)}",
                 limit=settings.login_max_attempts, window=settings.login_window_seconds)


async def _registration_allowed(session: AsyncSession) -> bool:
    """Allow registration when an admin has opened it (or the env default), or to
    bootstrap the very first account when no users exist yet."""
    if (await session.execute(select(func.count()).select_from(User))).scalar_one() == 0:
        return True
    app = await get_app_settings(session)
    return bool(app.registration_open or settings.registration_open)


async def _complete_login(request: Request, session: AsyncSession, app, user: User):
    from ...auth.security import new_session_token
    if user.session_token is None:          # backfill for pre-existing accounts
        user.session_token = new_session_token()
        await session.commit()
    request.session.clear()                 # rotate the session on auth (anti-fixation)
    request.session["user_id"] = user.id
    request.session["sv"] = user.session_token
    # If 2FA is mandatory and this account hasn't set it up, send them to do so.
    if app.force_otp and not user.otp_enabled:
        return RedirectResponse("/account?err=otp_required", status_code=303)
    return RedirectResponse("/", status_code=303)


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"mode": "login"})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not _rate_ok(request, "login"):
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "login", "error": "Too many attempts — please wait and try again.", "email": email},
            status_code=429,
        )
    user = await authenticate(session, email, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "login", "error": "Invalid email or password.", "email": email},
            status_code=401,
        )
    reset(f"login:{client_ip(request)}")
    if user.otp_enabled:
        # Hold the (password-verified) user pending a valid 2FA code.
        request.session.clear()
        request.session["otp_uid"] = user.id
        return templates.TemplateResponse(request, "login.html", {"mode": "otp"})
    app = await get_app_settings(session)
    return await _complete_login(request, session, app, user)


@router.post("/login/otp")
async def login_otp(
    request: Request,
    code: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    uid = request.session.get("otp_uid")
    if not uid:
        return RedirectResponse("/login", status_code=303)
    if not _rate_ok(request, "otp"):
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "otp", "error": "Too many attempts — please wait and try again."}, status_code=429)
    user = await session.get(User, uid)
    if user is None or not user.is_active or not verify(user_secret(user), code):
        # Per-pending-user counter so rotating source IPs can't grind codes.
        fails = request.session.get("otp_fails", 0) + 1
        if fails >= 5:
            request.session.pop("otp_uid", None)
            request.session.pop("otp_fails", None)
            return templates.TemplateResponse(
                request, "login.html",
                {"mode": "login", "error": "Too many codes — please sign in again."}, status_code=401)
        request.session["otp_fails"] = fails
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "otp", "error": "Invalid authentication code."}, status_code=401)
    request.session.pop("otp_fails", None)
    reset(f"otp:{client_ip(request)}")
    app = await get_app_settings(session)
    return await _complete_login(request, session, app, user)


@router.get("/register")
async def register_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"mode": "register"})


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    email = email.strip().lower()
    if not _rate_ok(request, "register"):
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "register", "error": "Too many attempts — please wait and try again.", "email": email},
            status_code=429,
        )
    if not await _registration_allowed(session):
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "register", "error": "Registration is closed. Ask an administrator for an account.", "email": email},
            status_code=403,
        )
    if not (8 <= len(password) <= 1024):  # upper bound guards against argon2 DoS
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "register", "error": "Password must be 8–1024 characters.", "email": email},
            status_code=400,
        )
    if await get_by_email(session, email):
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "register", "error": "That email is already registered.", "email": email},
            status_code=400,
        )
    user = await create_user(session, email, password)
    app = await get_app_settings(session)
    return await _complete_login(request, session, app, user)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
