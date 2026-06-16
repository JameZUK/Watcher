"""URL safety checks to limit SSRF.

Levels:
- ``validate_monitor_url`` — for URLs a real browser will navigate to: blocks
  non-http(s) schemes (file:, data:, chrome:, view-source:, …).
- ``validate_public_url`` — stricter: additionally rejects hosts that resolve to
  any private/loopback/link-local/reserved/metadata address. Used for server-side
  ``httpx`` fetches AND (unless ``allow_private_targets`` is set) for the browser
  render path and every user-supplied outbound destination.
- ``validate_proxy`` — same IP policy applied to a per-monitor proxy host.

Residual risk: a real browser re-resolves DNS itself, so DNS-rebinding / a
redirect to an internal host between validation and the actual navigation is not
fully closed in-process. For untrusted public exposure, also place the renderer
behind an egress firewall/proxy that blocks RFC1918 + link-local + ULA + metadata
ranges. ``allow_private_targets`` disables the IP checks for trusted/internal
deployments that intentionally monitor private hosts.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

_ALLOWED_SCHEMES = {"http", "https"}
_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h", "socks4"}

# Explicit deny networks layered on top of the ``ipaddress`` property checks.
_DENY_NETS = (
    ipaddress.ip_network("0.0.0.0/8"),       # "this network"
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. cloud metadata
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT
)


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


def _unwrap(addr):
    """Unwrap IPv4-mapped / 6to4 / NAT64 IPv6 forms to the embedded IPv4 so an
    address like ``::ffff:127.0.0.1`` or ``64:ff9b::7f00:1`` is judged on its
    real (internal) IPv4 target rather than slipping through as "public IPv6"."""
    if addr.version != 6:
        return addr
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    sixtofour = getattr(addr, "sixtofour", None)
    if sixtofour is not None:
        return sixtofour
    if addr in ipaddress.ip_network("64:ff9b::/96"):  # NAT64 well-known prefix
        return ipaddress.ip_address(int(addr) & 0xFFFFFFFF)
    return addr


def _ip_is_public(ip: str) -> bool:
    try:
        addr = _unwrap(ipaddress.ip_address(ip))
    except ValueError:
        return False
    if any(addr in net for net in _DENY_NETS if addr.version == net.version):
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _resolve(host: str) -> set[str] | None:
    """Resolve ``host`` to a set of IP strings, or None on resolution failure."""
    try:
        return {info[4][0] for info in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        return None


def _host_is_public(host: str) -> str | None:
    """Resolve ``host`` and return an error string if it has no addresses or ANY
    resolved address is non-public; else None. Unresolvable → error (save-time)."""
    if not host:
        return "URL is missing a host."
    ips = _resolve(host)
    if ips is None:
        return "Could not resolve the host."
    if not ips or any(not _ip_is_public(ip) for ip in ips):
        return "Refusing to connect to a private/internal address."
    return None


def _host_resolves_internal(host: str) -> bool:
    """True only if ``host`` resolves and ANY address is internal. Unresolvable
    or public → False — so a transient DNS failure isn't treated as a block."""
    ips = _resolve(host)
    return bool(ips) and any(not _ip_is_public(ip) for ip in ips)


def host_resolves_internal(host: str) -> bool:
    """Public wrapper: True if ``host`` resolves to any private/internal address.
    Used by the engine-level render-time SSRF route guard to abort browser
    requests (incl. redirects / JS navigations) to internal targets — the
    in-process complement to an egress firewall. Blocking (resolves DNS); call
    off the event loop."""
    return _host_resolves_internal((host or "").strip().lower())


def render_block_reason(url: str) -> str | None:
    """Render-time SSRF gate: block a bad scheme or a host that RESOLVES to an
    internal address. Returns None on resolution failure (transient — let the
    normal render attempt handle it rather than hard-failing/auto-pausing)."""
    err = validate_monitor_url(url)
    if err:
        return err
    if _host_resolves_internal(urlparse(url.strip()).hostname or ""):
        return "Refusing to connect to a private/internal address."
    return None


def proxy_block_reason(proxy: str | None) -> str | None:
    """Render-time proxy gate (transient-tolerant counterpart to validate_proxy)."""
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        return "Proxy must be scheme://host:port."
    if proxy.split("://", 1)[0].lower() not in _PROXY_SCHEMES:
        return "Unsupported proxy scheme."
    host = urlparse(proxy).hostname
    if not host:
        return "Proxy is missing a host."
    if _host_resolves_internal(host):
        return "Proxy resolves to a private/internal address."
    return None


def validate_public_url(url: str) -> str | None:
    """Stricter check: scheme allowlist AND every resolved IP must be public."""
    err = validate_monitor_url(url)
    if err:
        return err
    return _host_is_public(urlparse(url.strip()).hostname or "")


def validate_notify_url(url: str) -> str | None:
    """Validate a USER notification destination (Home Assistant / ntfy / Discord /
    webhook). Always scheme/format-checked; the private-IP restriction is applied ONLY
    when both ``allow_private_targets`` and ``allow_private_notify_targets`` are off — so
    by default a user can notify their own LAN service while monitor render targets stay
    locked to public addresses."""
    from .config import settings
    err = validate_monitor_url(url)
    if err:
        return err
    if settings.allow_private_targets or settings.allow_private_notify_targets:
        return None
    return _host_is_public(urlparse(url.strip()).hostname or "")


def validate_proxy(proxy: str | None) -> str | None:
    """Validate a per-monitor proxy string ``scheme://host[:port]``: known scheme
    and a host that resolves only to public addresses. None/empty is allowed."""
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if "://" not in proxy:
        return "Proxy must be scheme://host:port."
    scheme = proxy.split("://", 1)[0].lower()
    if scheme not in _PROXY_SCHEMES:
        return "Unsupported proxy scheme."
    host = urlparse(proxy).hostname
    if not host:
        return "Proxy is missing a host."
    return _host_is_public(host)
