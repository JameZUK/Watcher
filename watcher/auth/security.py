"""Password hashing (argon2) and credential encryption (Fernet)."""

from __future__ import annotations

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ..config import settings

_ph = PasswordHasher()


# --- API tokens ------------------------------------------------------------
# Tokens are high-entropy (192-bit) random strings, so a fast unsalted SHA-256
# is sufficient to store them at rest (no brute-force surface) while ensuring a
# DB leak doesn't hand out working tokens.

def new_api_token() -> str:
    return secrets.token_urlsafe(24)


def new_session_token() -> str:
    """Per-credential-version token; bumping it invalidates other live sessions."""
    return secrets.token_hex(16)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_hashed(value: str | None) -> bool:
    """True if ``value`` is already a SHA-256 hex digest (migration guard)."""
    return bool(value) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(password_hash)
    except Exception:
        return False


# --- Credential encryption (for login-flow secrets) ------------------------


def encrypt_secret(plaintext: str) -> str:
    return settings.fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    return settings.fernet().decrypt(token.encode("ascii")).decode("utf-8")
