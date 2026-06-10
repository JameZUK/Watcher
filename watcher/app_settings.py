"""Access to the singleton global AppSetting row (AI triage config, etc.)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .auth.security import decrypt_secret, encrypt_secret
from .models import AppSetting


async def get_app_settings(session: AsyncSession) -> AppSetting:
    """Return the singleton settings row, creating it on first access."""
    s = await session.get(AppSetting, 1)
    if s is None:
        s = AppSetting(id=1)
        session.add(s)
        await session.flush()
    return s


def get_openrouter_key(s: AppSetting) -> str | None:
    """Decrypt the stored OpenRouter key, or None if unset/undecryptable."""
    if not s.openrouter_key_enc:
        return None
    try:
        return decrypt_secret(s.openrouter_key_enc)
    except Exception:
        return None


def set_openrouter_key(s: AppSetting, plaintext: str | None) -> None:
    """Store (encrypt) a new key, or clear it when given an empty value."""
    s.openrouter_key_enc = encrypt_secret(plaintext) if plaintext else None
