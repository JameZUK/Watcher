"""Settings page and Web Push subscription management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import get_current_user
from ...config import settings
from ...db import get_session
from ...models import PushSubscription, User
from ...notify import push
from .. import templates

router = APIRouter()


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
        },
    )


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
