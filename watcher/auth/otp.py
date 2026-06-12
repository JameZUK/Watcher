"""TOTP (authenticator-app) two-factor helpers.

Secrets are stored Fernet-encrypted on the user row; this module handles
generation, the provisioning URI / QR, and code verification.
"""

from __future__ import annotations

import pyotp

from .security import decrypt_secret

ISSUER = "Watcher"


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify(secret: str | None, code: str | None) -> bool:
    """True if ``code`` is a valid TOTP for ``secret`` (±1 step of clock skew)."""
    return verify_step(secret, code) is not None


def verify_step(secret: str | None, code: str | None) -> int | None:
    """The absolute TOTP step ``code`` matches for ``secret`` (within ±1 of clock
    skew), or None. The step lets the caller enforce single-use (reject a code at
    or below the last consumed step) so a shoulder-surfed code can't be replayed
    inside its ~90s validity window."""
    import hmac
    import time as _time

    if not secret or not code:
        return None
    code = "".join(ch for ch in str(code) if ch.isdigit())
    if len(code) != 6:
        return None
    try:
        totp = pyotp.TOTP(secret)
        step = int(_time.time()) // totp.interval
        for s in (step - 1, step, step + 1):                 # ±1 window
            if hmac.compare_digest(totp.at(s * totp.interval), code):
                return s
        return None
    except Exception:  # noqa: BLE001
        return None


def user_secret(user) -> str | None:
    """Decrypt and return the user's stored TOTP secret, or None."""
    if not getattr(user, "otp_secret_enc", None):
        return None
    try:
        return decrypt_secret(user.otp_secret_enc)
    except Exception:  # noqa: BLE001
        return None


def qr_svg(uri: str) -> str:
    """Inline SVG QR code for an otpauth:// URI (dark modules on white)."""
    import segno

    return segno.make(uri, error="m").svg_inline(scale=4, border=2,
                                                 dark="#0b0813", light="#ffffff")
