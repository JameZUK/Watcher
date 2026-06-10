"""Token-authenticated REST API + RSS feed of changes."""

from __future__ import annotations

from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db import get_session
from ...models import Change, Monitor, User

router = APIRouter()


async def require_token(
    request: Request,
    token: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> User:
    tok = token
    auth = request.headers.get("authorization", "")
    if not tok and auth.lower().startswith("bearer "):
        tok = auth[7:].strip()
    user = None
    if tok:
        from ...auth.security import hash_token
        user = (await session.execute(
            select(User).where(User.api_token == hash_token(tok), User.is_active)
        )).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")
    return user


@router.get("/robots.txt", include_in_schema=False)
async def robots() -> Response:
    """Keep the (auth-gated) app out of search indexes."""
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


def _monitor_json(m: Monitor) -> dict:
    return {
        "id": m.id, "name": m.name, "url": m.url, "engine": m.engine.value,
        "detection_mode": m.detection_mode.value, "enabled": m.enabled,
        "interval_seconds": m.interval_seconds, "tags": m.tags or [],
        "last_checked_at": m.last_checked_at.isoformat() if m.last_checked_at else None,
        "last_change_at": m.last_change_at.isoformat() if m.last_change_at else None,
        "consecutive_failures": m.consecutive_failures,
        "track_value": m.track_value,
    }


def _change_json(c: Change) -> dict:
    return {
        "id": c.id, "monitor_id": c.monitor_id, "detected_at": c.detected_at.isoformat(),
        "summary": c.summary, "headline": c.ai_headline, "category": c.ai_category,
        "importance": c.ai_importance, "magnitude": round(c.magnitude, 4),
        "acknowledged": c.acknowledged,
    }


@router.get("/api/monitors")
async def api_monitors(user: User = Depends(require_token), session: AsyncSession = Depends(get_session)):
    mons = (await session.execute(
        select(Monitor).where(Monitor.user_id == user.id).order_by(Monitor.id)
    )).scalars().all()
    return JSONResponse({"monitors": [_monitor_json(m) for m in mons]})


@router.get("/api/changes")
async def api_changes(
    limit: int = Query(50, le=200),
    user: User = Depends(require_token),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(Change, Monitor).join(Monitor, Monitor.id == Change.monitor_id)
        .where(Monitor.user_id == user.id)
        .order_by(Change.detected_at.desc()).limit(limit)
    )).all()
    return JSONResponse({"changes": [{**_change_json(c), "monitor_name": m.name} for c, m in rows]})


@router.get("/feed.xml")
async def rss_feed(
    user: User = Depends(require_token),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(Change, Monitor).join(Monitor, Monitor.id == Change.monitor_id)
        .where(Monitor.user_id == user.id)
        .order_by(Change.detected_at.desc()).limit(50)
    )).all()
    items = []
    for c, m in rows:
        title = escape(c.ai_headline or f"{m.name}: {c.summary}")
        desc = escape(c.summary or "")
        link = escape(m.url)
        items.append(
            f"<item><title>{title}</title><link>{link}</link>"
            f"<guid isPermaLink=\"false\">change-{c.id}</guid>"
            f"<pubDate>{c.detected_at.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
            f"<description>{desc}</description></item>"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Watcher — changes</title>"
        "<description>Website changes detected by Watcher</description>"
        "<link>/</link>" + "".join(items) + "</channel></rss>"
    )
    return Response(content=xml, media_type="application/rss+xml")
