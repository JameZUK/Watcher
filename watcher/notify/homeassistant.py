"""Home Assistant delivery: call a notify service via the HA REST API.

Each user configures their own HA base URL, a long-lived access token, and the
service to invoke — e.g. ``notify.mobile_app_phone`` (pushes to their devices) or
``persistent_notification.create`` (shows in the HA UI; works with no extra setup).
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("watcher.notify.homeassistant")

DEFAULT_SERVICE = "persistent_notification.create"


def _split_service(service: str | None) -> tuple[str, str]:
    """Normalise a service string into (domain, service). A bare name (no dot) is
    treated as a notify service: 'mobile_app_x' → ('notify', 'mobile_app_x')."""
    s = (service or DEFAULT_SERVICE).strip()
    if "." in s:
        domain, svc = s.split(".", 1)
        return domain.strip() or "notify", svc.strip()
    return "notify", s


def _build_payload(domain: str, title: str, body: str, url: str | None) -> dict:
    """Service-call data. For a notify.* service the link goes in data.clickAction (so
    tapping the mobile notification opens it); for persistent_notification.create it's a
    markdown link in the message (no data field — that service rejects unknown keys)."""
    if domain == "notify":
        message = f"{body}\n{url}" if url else body
        payload: dict = {"title": title[:255], "message": message[:2000]}
        if url:
            payload["data"] = {"url": url, "clickAction": url}
        return payload
    message = f"{body}\n\n[Open in Watcher]({url})" if url else body
    return {"title": title[:255], "message": message[:2000]}


async def send(base_url: str | None, token: str | None, service: str | None, *,
               title: str, body: str, url: str | None = None) -> bool:
    """POST a notification to ``{base_url}/api/services/{domain}/{service}`` with a Bearer
    token. ``url`` is the click target (the Watcher change page). Returns False on missing
    config or a non-2xx response."""
    if not (base_url and token):
        return False
    domain, svc = _split_service(service)
    payload = _build_payload(domain, title, body, url)
    endpoint = f"{base_url.rstrip('/')}/api/services/{domain}/{svc}"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(endpoint, json=payload, headers={"Authorization": f"Bearer {token}"})
        return r.status_code < 400
    except Exception as exc:  # noqa: BLE001
        logger.warning("Home Assistant send failed: %s", exc)
        return False
