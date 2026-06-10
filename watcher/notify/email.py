"""Email delivery via SMTP (admin-configured transport)."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger("watcher.notify.email")


async def send(app, to_addr: str, *, subject: str, body: str) -> bool:
    """Send an email; return True only if it was actually delivered."""
    if not (app.smtp_host and app.smtp_from and to_addr):
        return False
    from ..app_settings import get_smtp_password
    pw = get_smtp_password(app)

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = app.smtp_from, to_addr, subject
    msg.set_content(body)

    def _send() -> None:
        with smtplib.SMTP(app.smtp_host, app.smtp_port or 587, timeout=20) as srv:
            if app.smtp_tls:
                srv.starttls(context=ssl.create_default_context())
            if app.smtp_user:
                srv.login(app.smtp_user, pw or "")
            srv.send_message(msg)

    try:
        await asyncio.to_thread(_send)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("SMTP send failed: %s", exc)
        return False
