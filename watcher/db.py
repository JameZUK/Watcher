"""Async database engine, session factory, and init."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings

engine = create_async_engine(settings.database_url, echo=False, future=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session."""
    async with SessionLocal() as session:
        yield session


# Columns added after the initial release. SQLite's create_all won't ALTER an
# existing table, so we add any missing ones idempotently on startup.
_ADDED_COLUMNS = {
    "snapshots": {"title": "VARCHAR(512)", "screenshot_mobile_blob": "VARCHAR(64)"},
    "changes": {"visual_blob": "VARCHAR(64)"},
}


async def _add_missing_columns(conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        rows = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {r[1] for r in rows.fetchall()}
        for name, ddl in columns.items():
            if name not in existing:
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


async def init_db() -> None:
    """Create tables and enable SQLite WAL for better concurrency."""
    from . import models  # noqa: F401  (register mappers)
    from .models import Base

    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON;")
        await conn.run_sync(Base.metadata.create_all)
        await _add_missing_columns(conn)
