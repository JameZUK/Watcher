"""Registration, login, logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Open registration only if explicitly enabled, or to bootstrap the first
    (admin) account when no users exist yet."""
    if settings.registration_open:
        return True
    return (await session.execute(select(func.count()).select_from(User))).scalar_one() == 0


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
    reset(f"login:{client_ip(request)}")  # successful auth clears the throttle
    request.session.clear()  # rotate the session on auth (anti-fixation)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


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
    request.session.clear()  # rotate the session on auth (anti-fixation)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
