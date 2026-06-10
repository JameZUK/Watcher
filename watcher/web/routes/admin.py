"""Admin: user management + access-control settings (signup, force 2FA)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...app_settings import get_app_settings
from ...auth.security import hash_password
from ...auth.users import get_by_email, require_admin
from ...db import get_session
from ...models import User
from .. import templates

router = APIRouter()


def _bool(form, key):
    return form.get(key) in ("on", "true", "1", "yes")


async def _active_admin_count(session: AsyncSession) -> int:
    return (await session.execute(
        select(func.count()).select_from(User).where(User.is_admin, User.is_active))).scalar_one()


@router.get("/admin/users")
async def users_page(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    users = (await session.execute(select(User).order_by(User.id))).scalars().all()
    app = await get_app_settings(session)
    return templates.TemplateResponse(
        request, "admin_users.html",
        {"user": admin, "users": users, "app": app,
         "msg": request.query_params.get("msg"), "err": request.query_params.get("err")},
    )


@router.post("/admin/users")
async def create_user_admin(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    if not email or not (8 <= len(password) <= 1024):
        return RedirectResponse("/admin/users?err=invalid", status_code=303)
    if await get_by_email(session, email):
        return RedirectResponse("/admin/users?err=exists", status_code=303)
    session.add(User(email=email, password_hash=hash_password(password),
                     is_admin=_bool(form, "is_admin")))
    await session.commit()
    return RedirectResponse("/admin/users?msg=created", status_code=303)


async def _owned_user(session, admin, user_id) -> User | None:
    u = await session.get(User, int(user_id))
    return u  # any admin may manage any user


@router.post("/admin/users/{user_id}/admin")
async def toggle_admin(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    u = await session.get(User, user_id)
    if u is None:
        return RedirectResponse("/admin/users?err=missing", status_code=303)
    # Don't allow removing the last active admin.
    if u.is_admin and await _active_admin_count(session) <= 1:
        return RedirectResponse("/admin/users?err=last_admin", status_code=303)
    u.is_admin = not u.is_admin
    await session.commit()
    return RedirectResponse("/admin/users?msg=updated", status_code=303)


@router.post("/admin/users/{user_id}/active")
async def toggle_active(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    u = await session.get(User, user_id)
    if u is None or u.id == admin.id:
        return RedirectResponse("/admin/users?err=self", status_code=303)
    if u.is_active and u.is_admin and await _active_admin_count(session) <= 1:
        return RedirectResponse("/admin/users?err=last_admin", status_code=303)
    u.is_active = not u.is_active
    await session.commit()
    return RedirectResponse("/admin/users?msg=updated", status_code=303)


@router.post("/admin/users/{user_id}/password")
async def reset_password(
    request: Request,
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    new = form.get("password") or ""
    u = await session.get(User, user_id)
    if u is None or not (8 <= len(new) <= 1024):
        return RedirectResponse("/admin/users?err=invalid", status_code=303)
    u.password_hash = hash_password(new)
    await session.commit()
    return RedirectResponse("/admin/users?msg=password", status_code=303)


@router.post("/admin/users/{user_id}/reset-otp")
async def reset_otp(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Clear a user's 2FA (e.g. they lost their authenticator)."""
    u = await session.get(User, user_id)
    if u is not None:
        u.otp_enabled = False
        u.otp_secret_enc = None
        await session.commit()
    return RedirectResponse("/admin/users?msg=otp_reset", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    u = await session.get(User, user_id)
    if u is None or u.id == admin.id:
        return RedirectResponse("/admin/users?err=self", status_code=303)
    if u.is_admin and await _active_admin_count(session) <= 1:
        return RedirectResponse("/admin/users?err=last_admin", status_code=303)
    await session.delete(u)   # monitors/groups cascade via FK
    await session.commit()
    return RedirectResponse("/admin/users?msg=deleted", status_code=303)


@router.post("/admin/access")
async def save_access(
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    app = await get_app_settings(session)
    app.registration_open = _bool(form, "registration_open")
    app.force_otp = _bool(form, "force_otp")
    await session.commit()
    return RedirectResponse("/admin/users?msg=access", status_code=303)
