"""Settings page and Web Push subscription management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...ai import triage_change
from ...app_settings import (
    get_app_settings,
    get_openrouter_key,
    set_openrouter_key,
    set_smtp_password,
    set_telegram_token,
)
from ...auth.users import get_current_user, require_admin
from ...config import settings
from ...db import get_session
from ...models import PushSubscription, User
from ...notify import push
from .. import templates

router = APIRouter()

_POLICIES = ("silent", "label", "drop")


@router.get("/settings")
async def settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    sub_count = len(
        (
            await session.execute(
                select(PushSubscription.id).where(PushSubscription.user_id == user.id)
            )
        ).scalars().all()
    )
    app = await get_app_settings(session)
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "user": user,
            "push_enabled": push.enabled(),
            "vapid_public_key": settings.vapid_public_key or "",
            "sub_count": sub_count,
            "min_interval_min": settings.min_interval_seconds // 60,
            "retention_days": settings.retention_max_days,
            "retention_snapshots": settings.retention_max_snapshots,
            # AI config (the key itself is never sent to the client — only a flag).
            "is_admin": user.is_admin,
            "ai_enabled": app.ai_enabled,
            "ai_model": app.ai_model,
            "ai_key_set": bool(app.openrouter_key_enc),
            "ai_policy": app.ai_low_value_policy,
            "api_token": user.api_token or "",
            # Per-user notification destinations + delivery prefs
            "nd": {
                "telegram_chat_id": user.telegram_chat_id or "",
                "discord_webhook": user.discord_webhook or "",
                "ntfy_topic": user.ntfy_topic or "",
                "digest_enabled": user.digest_enabled,
                "quiet_start": user.quiet_start,
                "quiet_end": user.quiet_end,
            },
            # Admin transport config (secrets exposed only as set/unset flags)
            "tx": {
                "smtp_host": app.smtp_host or "", "smtp_port": app.smtp_port,
                "smtp_user": app.smtp_user or "", "smtp_from": app.smtp_from or "",
                "smtp_tls": app.smtp_tls, "smtp_pass_set": bool(app.smtp_pass_enc),
                "telegram_token_set": bool(app.telegram_token_enc), "ntfy_server": app.ntfy_server,
            },
        },
    )


@router.post("/settings/api-token")
async def gen_api_token(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    import secrets
    user.api_token = secrets.token_urlsafe(24)
    await session.commit()
    return RedirectResponse("/settings", status_code=303)


def _int_or_none(v):
    try:
        return int(v) if str(v).strip() != "" else None
    except (ValueError, TypeError):
        return None


@router.post("/settings/notifications")
async def save_notifications(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    user.telegram_chat_id = (form.get("telegram_chat_id") or "").strip() or None
    user.discord_webhook = (form.get("discord_webhook") or "").strip() or None
    user.ntfy_topic = (form.get("ntfy_topic") or "").strip() or None
    user.digest_enabled = _bool(form, "digest_enabled")
    qs, qe = _int_or_none(form.get("quiet_start")), _int_or_none(form.get("quiet_end"))
    user.quiet_start = qs if qs is not None and 0 <= qs <= 23 else None
    user.quiet_end = qe if qe is not None and 0 <= qe <= 23 else None
    await session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/transports")
async def save_transports(
    request: Request,
    user: User = Depends(require_admin),   # admin-only
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    app = await get_app_settings(session)
    app.smtp_host = (form.get("smtp_host") or "").strip() or None
    app.smtp_port = _int_or_none(form.get("smtp_port")) or 587
    app.smtp_user = (form.get("smtp_user") or "").strip() or None
    app.smtp_from = (form.get("smtp_from") or "").strip() or None
    app.smtp_tls = _bool(form, "smtp_tls")
    app.ntfy_server = (form.get("ntfy_server") or "").strip() or "https://ntfy.sh"
    if (form.get("smtp_pass") or "").strip():
        set_smtp_password(app, form["smtp_pass"].strip())
    if (form.get("telegram_token") or "").strip():
        set_telegram_token(app, form["telegram_token"].strip())
    await session.commit()
    return RedirectResponse("/settings", status_code=303)


def _bool(form, key: str) -> bool:
    return form.get(key) in ("on", "true", "1", "yes")


@router.post("/settings/ai")
async def save_ai_settings(
    request: Request,
    user: User = Depends(require_admin),               # admin-only
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    app = await get_app_settings(session)
    app.ai_enabled = _bool(form, "ai_enabled")
    model = (form.get("ai_model") or "").strip()
    if model:
        app.ai_model = model
    policy = (form.get("ai_low_value_policy") or "silent").strip()
    app.ai_low_value_policy = policy if policy in _POLICIES else "silent"
    # Key: a new value replaces; "clear" wipes; blank leaves the existing key.
    if _bool(form, "openrouter_key_clear"):
        set_openrouter_key(app, None)
    else:
        new_key = (form.get("openrouter_key") or "").strip()
        if new_key:
            set_openrouter_key(app, new_key)
    await session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/ai/test")
async def test_ai_settings(
    user: User = Depends(require_admin),               # admin-only
    session: AsyncSession = Depends(get_session),
):
    """Run a tiny live triage to validate the key/model. Returns JSON."""
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return JSONResponse({"ok": False, "error": "No API key set."}, status_code=400)
    result = await triage_change(
        api_key=key, model=app.ai_model, url="https://example.com",
        title="Example", intent=None,
        diff_text="- Price: £299.00\n+ Price: £263.99", timeout=20.0,
    )
    if result is None:
        return JSONResponse({"ok": False, "error": "Call failed — check the key, model id, and credit."}, status_code=502)
    return JSONResponse({"ok": True, "headline": result.headline, "category": result.category, "importance": result.importance})


@router.get("/push/vapid-key")
async def vapid_key():
    return JSONResponse({"publicKey": settings.vapid_public_key or ""})


@router.post("/push/subscribe")
async def push_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    data = await request.json()
    endpoint = data.get("endpoint")
    keys = data.get("keys", {})
    if not endpoint or "p256dh" not in keys or "auth" not in keys:
        return JSONResponse({"ok": False, "error": "invalid subscription"}, status_code=400)

    existing = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == user.id,
                PushSubscription.endpoint == endpoint,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            PushSubscription(
                user_id=user.id, endpoint=endpoint,
                p256dh=keys["p256dh"], auth=keys["auth"],
            )
        )
        await session.commit()
    return JSONResponse({"ok": True})


@router.post("/push/unsubscribe")
async def push_unsubscribe(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    data = await request.json()
    endpoint = data.get("endpoint")
    if endpoint:
        await session.execute(
            delete(PushSubscription).where(
                PushSubscription.user_id == user.id,
                PushSubscription.endpoint == endpoint,
            )
        )
        await session.commit()
    return JSONResponse({"ok": True})
