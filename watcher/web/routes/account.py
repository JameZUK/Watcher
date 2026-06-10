"""Self-service account page: profile, password, and two-factor (TOTP)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.otp import new_secret, provisioning_uri, qr_svg, user_secret, verify
from ...auth.security import encrypt_secret, hash_password, verify_password
from ...auth.users import get_by_email, get_current_user
from ...db import get_session
from ...models import User
from .. import templates

router = APIRouter()


@router.get("/account")
async def account_page(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # A pending (not-yet-enabled) OTP setup lives in the session until confirmed.
    pending = request.session.get("otp_setup")
    otp_qr = otp_uri = None
    if pending and not user.otp_enabled:
        otp_uri = provisioning_uri(pending, user.email)
        otp_qr = qr_svg(otp_uri)
    return templates.TemplateResponse(
        request, "account.html",
        {"user": user, "otp_secret": pending, "otp_qr": otp_qr, "otp_uri": otp_uri,
         "msg": request.query_params.get("msg"), "err": request.query_params.get("err")},
    )


@router.post("/account/profile")
async def update_profile(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    user.display_name = (form.get("display_name") or "").strip()[:120] or None
    new_email = (form.get("email") or "").strip().lower()
    if new_email and new_email != user.email:
        existing = await get_by_email(session, new_email)
        if existing and existing.id != user.id:
            return RedirectResponse("/account?err=email_taken", status_code=303)
        user.email = new_email
    await session.commit()
    return RedirectResponse("/account?msg=profile", status_code=303)


@router.post("/account/password")
async def change_password(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    current = form.get("current_password") or ""
    new = form.get("new_password") or ""
    if not verify_password(current, user.password_hash):
        return RedirectResponse("/account?err=bad_password", status_code=303)
    if not (8 <= len(new) <= 1024):
        return RedirectResponse("/account?err=weak_password", status_code=303)
    user.password_hash = hash_password(new)
    await session.commit()
    return RedirectResponse("/account?msg=password", status_code=303)


@router.post("/account/otp/start")
async def otp_start(
    request: Request,
    user: User = Depends(get_current_user),
):
    # Generate a provisional secret, held in the session until the user confirms
    # a valid code (so a half-finished setup can't lock them out).
    request.session["otp_setup"] = new_secret()
    return RedirectResponse("/account?msg=otp_scan", status_code=303)


@router.post("/account/otp/enable")
async def otp_enable(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    secret = request.session.get("otp_setup")
    form = await request.form()
    if not secret or not verify(secret, form.get("code")):
        return RedirectResponse("/account?err=otp_code", status_code=303)
    user.otp_secret_enc = encrypt_secret(secret)
    user.otp_enabled = True
    await session.commit()
    request.session.pop("otp_setup", None)
    return RedirectResponse("/account?msg=otp_on", status_code=303)


@router.post("/account/otp/disable")
async def otp_disable(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    # Require the current password OR a valid code to turn 2FA off.
    ok = verify_password(form.get("password") or "", user.password_hash) or \
        verify(user_secret(user), form.get("code"))
    if not ok:
        return RedirectResponse("/account?err=otp_off", status_code=303)
    user.otp_enabled = False
    user.otp_secret_enc = None
    await session.commit()
    request.session.pop("otp_setup", None)
    return RedirectResponse("/account?msg=otp_off", status_code=303)
