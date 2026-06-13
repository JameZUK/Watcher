"""Async database engine, session factory, and init."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import settings

engine = create_async_engine(settings.database_url, echo=False, future=True)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    """Per-connection PRAGMAs (they don't persist across connections like WAL does).

    busy_timeout is the important one: without it a writer that meets SQLite's
    single-writer lock fails *immediately* with "database is locked" — so a form
    save colliding with a background check 500s. With it, the writer waits.
    """
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=15000")   # wait up to 15s for the write lock
    cur.execute("PRAGMA synchronous=NORMAL")   # safe + faster under WAL
    cur.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async session."""
    async with SessionLocal() as session:
        yield session


# Columns added after the initial release. SQLite's create_all won't ALTER an
# existing table, so we add any missing ones idempotently on startup.
_ADDED_COLUMNS = {
    "users": {
        "is_admin": "BOOLEAN DEFAULT 0",
        "telegram_chat_id": "VARCHAR(64)", "discord_webhook": "VARCHAR(512)",
        "ntfy_topic": "VARCHAR(128)", "digest_enabled": "BOOLEAN DEFAULT 0",
        "quiet_start": "INTEGER", "quiet_end": "INTEGER", "api_token": "VARCHAR(64)",
        "display_name": "VARCHAR(120)", "otp_secret_enc": "TEXT", "otp_enabled": "BOOLEAN DEFAULT 0",
        "session_token": "VARCHAR(64)", "last_otp_step": "INTEGER DEFAULT 0",
        "summary_enabled": "BOOLEAN DEFAULT 1", "summary_days": "INTEGER DEFAULT 7",
        "summary_prompt": "VARCHAR(500)",
    },
    "snapshots": {
        "title": "VARCHAR(512)", "screenshot_mobile_blob": "VARCHAR(64)",
        "numeric_value": "FLOAT", "value_label": "VARCHAR(64)",
        "screenshot_sections": "JSON", "screenshot_mobile_sections": "JSON",
        "element_map": "JSON", "element_map_mobile": "JSON",
    },
    "changes": {
        "visual_blob": "VARCHAR(64)", "visual_mobile_blob": "VARCHAR(64)",
        "ai_headline": "TEXT", "ai_category": "VARCHAR(32)", "ai_importance": "VARCHAR(16)",
        "notified": "BOOLEAN DEFAULT 0",
    },
    "monitors": {
        "ai_enabled": "BOOLEAN DEFAULT 1", "ai_watch_intent": "TEXT", "ai_policy": "VARCHAR(16)",
        "track_value": "BOOLEAN DEFAULT 0", "value_threshold": "FLOAT", "value_threshold_dir": "VARCHAR(8)",
        "consecutive_failures": "INTEGER DEFAULT 0", "auto_paused_at": "DATETIME",
        "tags": "JSON", "adaptive_interval": "BOOLEAN DEFAULT 0",
        "group_id": "INTEGER", "block_annoyances": "BOOLEAN DEFAULT 1",
        "consent_clicks": "JSON", "consent_ai_tried": "BOOLEAN DEFAULT 0",
        "auto_relogin_enabled": "BOOLEAN DEFAULT 0", "use_proxy_pool": "BOOLEAN DEFAULT 0",
        "ai_page_profile": "TEXT", "churn_lines": "JSON",
    },
    "login_flows": {
        "relogin_cooldown_until": "TIMESTAMP",
    },
    "groups": {
        "watch_intent": "TEXT", "kind": "VARCHAR(16) DEFAULT 'price'",
        "hide_members": "BOOLEAN DEFAULT 0",
    },
    "app_settings": {
        "registration_open": "BOOLEAN DEFAULT 0", "force_otp": "BOOLEAN DEFAULT 0",
        "smtp_host": "VARCHAR(255)", "smtp_port": "INTEGER DEFAULT 587", "smtp_user": "VARCHAR(255)",
        "smtp_pass_enc": "TEXT", "smtp_from": "VARCHAR(255)", "smtp_tls": "BOOLEAN DEFAULT 1",
        "telegram_token_enc": "TEXT", "ntfy_server": "VARCHAR(255) DEFAULT 'https://ntfy.sh'",
        "ai_base_url": "VARCHAR(255)", "proxy_pool": "TEXT",
    },
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
        # Ensure an admin exists: if none, promote the earliest account so the
        # global settings aren't locked out (covers DBs created pre-admin).
        admins = (await conn.exec_driver_sql("SELECT COUNT(*) FROM users WHERE is_admin")).scalar()
        if not admins:
            await conn.exec_driver_sql(
                "UPDATE users SET is_admin=1 WHERE id=(SELECT MIN(id) FROM users)"
            )
        # Composite indexes for the hot query shapes (idempotent; covers DBs
        # created before these indexes existed).
        for ddl in (
            "CREATE INDEX IF NOT EXISTS ix_snap_monitor_taken ON snapshots(monitor_id, taken_at)",
            "CREATE INDEX IF NOT EXISTS ix_change_monitor_detected ON changes(monitor_id, detected_at)",
            "CREATE INDEX IF NOT EXISTS ix_change_monitor_acked ON changes(monitor_id, acknowledged)",
        ):
            await conn.exec_driver_sql(ddl)
        # One-time migration: hash any API tokens still stored in cleartext.
        # The previously-issued token keeps working (lookups hash the presented
        # value), but a DB leak no longer exposes usable tokens.
        from .auth.security import hash_token, looks_hashed
        rows = (await conn.exec_driver_sql(
            "SELECT id, api_token FROM users WHERE api_token IS NOT NULL")).fetchall()
        for uid, tok in rows:
            if tok and not looks_hashed(tok):
                await conn.exec_driver_sql(
                    "UPDATE users SET api_token=? WHERE id=?", (hash_token(tok), uid))
