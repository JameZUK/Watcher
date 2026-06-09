"""Change inbox: review and acknowledge detected changes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...auth.users import get_current_user
from ...db import get_session
from ...models import Change, Monitor, User
from .. import templates

router = APIRouter()


@router.get("/inbox")
async def inbox(
    request: Request,
    show: str = "unacked",
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Change, Monitor.name)
        .join(Monitor, Monitor.id == Change.monitor_id)
        .where(Monitor.user_id == user.id)
        .order_by(Change.detected_at.desc())
        .limit(200)
    )
    if show == "unacked":
        stmt = stmt.where(Change.acknowledged.is_(False))
    rows = (await session.execute(stmt)).all()
    items = [{"change": c, "monitor_name": name} for c, name in rows]
    return templates.TemplateResponse(
        request, "inbox.html", {"user": user, "items": items, "show": show}
    )


@router.post("/changes/{change_id}/ack")
async def ack_change(
    change_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    change = await session.get(Change, change_id)
    if not change:
        raise HTTPException(404)
    monitor = await session.get(Monitor, change.monitor_id)
    if not monitor or monitor.user_id != user.id:
        raise HTTPException(404)
    change.acknowledged = True
    await session.commit()
    return RedirectResponse("/inbox", status_code=303)


@router.post("/changes/ack-all")
async def ack_all(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    monitor_ids = (
        await session.execute(select(Monitor.id).where(Monitor.user_id == user.id))
    ).scalars().all()
    if monitor_ids:
        await session.execute(
            update(Change)
            .where(Change.monitor_id.in_(monitor_ids), Change.acknowledged.is_(False))
            .values(acknowledged=True)
        )
        await session.commit()
    return RedirectResponse("/inbox", status_code=303)
