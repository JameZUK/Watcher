"""HMAC-signed webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from ..config import settings


# Derive a dedicated webhook-signing key so the cookie-signing secret is never
# exposed to (user-controlled) webhook endpoints.
def _webhook_key() -> bytes:
    return hmac.new(settings.secret_key.encode(), b"watcher-webhook-signing", hashlib.sha256).digest()


def _sign(body: bytes) -> str:
    return hmac.new(_webhook_key(), body, hashlib.sha256).hexdigest()


async def send(url: str, payload: dict) -> bool:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Watcher/0.1",
        "X-Watcher-Signature": f"sha256={_sign(body)}",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, content=body, headers=headers)
            return resp.status_code < 400
    except Exception:
        return False
