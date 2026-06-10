"""URL safety checks to limit SSRF.

Two levels:
- ``validate_monitor_url`` — for URLs a real browser will navigate to: blocks
  non-http(s) schemes (file:, data:, chrome:, view-source:, …).
- ``validate_public_url`` — stricter, for server-side ``httpx`` fetches that
  return the body to the user: additionally rejects hosts that resolve to any
  private/loopback/link-local/reserved/metadata address.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}


def validate_monitor_url(url: str) -> str | None:
    """Return an error string if the URL is unsafe to render, else None."""
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return "Invalid URL."
    if p.scheme.lower() not in _ALLOWED_SCHEMES:
        return "URL must start with http:// or https://."
    if not p.hostname:
        return "URL is missing a host."
    return None


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def validate_public_url(url: str) -> str | None:
    """Stricter check for server-side fetches: also blocks internal targets.

    Resolves the host and rejects if ANY resolved address is non-public
    (defends against internal hosts and most metadata endpoints; DNS-rebinding
    between this check and the fetch is a residual risk).
    """
    err = validate_monitor_url(url)
    if err:
        return err
    host = urlparse(url.strip()).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return "Could not resolve the host."
    ips = {info[4][0] for info in infos}
    if not ips or any(not _ip_is_public(ip) for ip in ips):
        return "Refusing to fetch a private/internal address."
    return None
