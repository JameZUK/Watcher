"""User creation / lookup and the current-user FastAPI dependency."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import User
from .security import hash_password, verify_password


async def user_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count(User.id)))).scalar_one()


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    return (
        await session.execute(select(User).where(User.email == email.lower().strip()))
    ).scalar_one_or_none()


async def create_user(session: AsyncSession, email: str, password: str) -> User:
    user = User(email=email.lower().strip(), password_hash=hash_password(password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    user = await get_by_email(session, email)
    if user and user.is_active and verify_password(password, user.password_hash):
        return user
    return None


async def get_current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    """Dependency: resolve the logged-in user or 401 (redirect handled in web)."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    user = await session.get(User, user_id)
    if not user or not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return user
