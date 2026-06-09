"""Helpers to build and apply authenticated-site login flows.

A login flow is an ordered list of steps replayed against a freshly opened
page before the snapshot is captured. Supported step actions:

    {"action": "goto",  "url": "https://site/login"}
    {"action": "fill",  "selector": "#user", "secret": "username"}
    {"action": "fill",  "selector": "#pass", "secret": "password"}
    {"action": "click", "selector": "button[type=submit]"}
    {"action": "wait",  "selector": ".dashboard"}       # or "ms": 2000

Secrets are referenced by name; their plaintext lives only in the encrypted
secret map and is decrypted at replay time. The resulting browser
storage_state (cookies + localStorage) is persisted so subsequent checks can
skip the login until the session expires.
"""

from __future__ import annotations

from datetime import timedelta

from ..models import LoginFlow, utcnow
from .security import decrypt_secret, encrypt_secret


def build_secret_map(plain: dict[str, str]) -> dict[str, str]:
    """Encrypt a {name: plaintext} map for storage."""
    return {name: encrypt_secret(value) for name, value in plain.items() if value}


def resolve_secrets(flow: LoginFlow) -> dict[str, str]:
    """Decrypt the stored secret map for use during replay."""
    return {name: decrypt_secret(token) for name, token in (flow.encrypted_secrets or {}).items()}


def session_is_valid(flow: LoginFlow | None) -> bool:
    if not flow or not flow.session_state:
        return False
    if flow.session_valid_until is None:
        return True
    return flow.session_valid_until > utcnow()


def mark_session(flow: LoginFlow, state: dict, ttl_hours: int = 12) -> None:
    flow.session_state = state
    flow.session_valid_until = utcnow() + timedelta(hours=ttl_hours)
