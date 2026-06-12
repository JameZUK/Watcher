"""Access to the singleton global AppSetting row (AI triage config, etc.)."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession

from .auth.security import decrypt_secret, encrypt_secret
from .models import AppSetting


@lru_cache(maxsize=64)
def _decrypt_cached(token: str) -> str | None:
    """Decrypt a Fernet token, cached by ciphertext. The shared AI key (and SMTP /
    Telegram secrets) were re-decrypted up to ~3x per check; the ciphertext is a
    perfect cache key — it changes whenever the stored value does, so no explicit
    invalidation is needed (a key rotation requires a restart anyway)."""
    try:
        return decrypt_secret(token)
    except Exception:
        return None


async def get_app_settings(session: AsyncSession) -> AppSetting:
    """Return the singleton settings row, creating it on first access.

    Tolerates a concurrent create (two sessions racing on first run)."""
    from sqlalchemy.exc import IntegrityError

    s = await session.get(AppSetting, 1)
    if s is None:
        s = AppSetting(id=1)
        session.add(s)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            s = await session.get(AppSetting, 1)
    return s


def get_openrouter_key(s: AppSetting) -> str | None:
    """Decrypt the stored OpenRouter key, or None if unset/undecryptable."""
    return _decrypt_cached(s.openrouter_key_enc) if s.openrouter_key_enc else None


def set_openrouter_key(s: AppSetting, plaintext: str | None) -> None:
    """Store (encrypt) a new key, or clear it when given an empty value."""
    s.openrouter_key_enc = encrypt_secret(plaintext) if plaintext else None


def _get_enc(token: str | None) -> str | None:
    return _decrypt_cached(token) if token else None


def get_smtp_password(s: AppSetting) -> str | None:
    return _get_enc(s.smtp_pass_enc)


def set_smtp_password(s: AppSetting, plaintext: str | None) -> None:
    s.smtp_pass_enc = encrypt_secret(plaintext) if plaintext else None


def get_telegram_token(s: AppSetting) -> str | None:
    return _get_enc(s.telegram_token_enc)


def set_telegram_token(s: AppSetting, plaintext: str | None) -> None:
    s.telegram_token_enc = encrypt_secret(plaintext) if plaintext else None
