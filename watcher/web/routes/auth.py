"""Registration, login, logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import authenticate, create_user, get_by_email
from ...db import get_session
from .. import templates

router = APIRouter()


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
    user = await authenticate(session, email, password)
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"mode": "login", "error": "Invalid email or password.", "email": email},
            status_code=401,
        )
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
