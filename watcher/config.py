"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATCHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    data_dir: Path = Field(default=Path("data"))

    # --- Security ---
    # Used to sign session cookies. MUST be set in production.
    secret_key: str = Field(default="dev-insecure-change-me")
    # Fernet key (base64, 32 bytes) for encrypting stored credentials. If unset,
    # one is derived from secret_key (fine for dev, set explicitly in prod).
    encryption_key: str | None = Field(default=None)

    # --- Server ---
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    session_cookie: str = Field(default="watcher_session")
    session_max_age: int = Field(default=60 * 60 * 24 * 14)  # 14 days

    # --- Scheduling ---
    min_interval_seconds: int = Field(default=15 * 60)  # 15 min floor
    default_interval_seconds: int = Field(default=60 * 60)  # 1 hr default
    schedule_jitter_seconds: int = Field(default=60)
    max_render_concurrency: int = Field(default=3)
    render_timeout_seconds: int = Field(default=45)

    # --- Retention ---
    retention_max_snapshots: int = Field(default=50)
    retention_max_days: int = Field(default=90)

    # --- Web Push (VAPID) ---
    vapid_public_key: str | None = Field(default=None)
    vapid_private_key: str | None = Field(default=None)
    vapid_subject: str = Field(default="mailto:admin@example.com")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "watcher.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    def fernet(self) -> Fernet:
        """Return a Fernet cipher for credential encryption."""
        if self.encryption_key:
            return Fernet(self.encryption_key.encode())
        # Derive a stable 32-byte key from secret_key (dev convenience).
        raw = self.secret_key.encode("utf-8").ljust(32, b"0")[:32]
        return Fernet(base64.urlsafe_b64encode(raw))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
