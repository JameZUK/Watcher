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

import json
from datetime import timedelta, timezone

from ..models import LoginFlow, utcnow
from .security import decrypt_secret, encrypt_secret

# Cookie Editor / browser sameSite strings → Playwright's accepted values.
_SAMESITE = {
    "no_restriction": "None", "none": "None",
    "lax": "Lax", "unspecified": "Lax", "": "Lax",
    "strict": "Strict",
}


def _domain_allowed(cookie_domain: str, host: str | None) -> bool:
    """A cookie is in-scope if its domain equals or is a parent of the host
    (standard cookie domain matching). Without a host, allow all."""
    if not host:
        return True
    d = cookie_domain.lstrip(".").lower()
    h = host.lower()
    return bool(d) and (h == d or h.endswith("." + d))


def _parse_json_blocks(raw: str) -> list:
    """Parse one OR MORE concatenated top-level JSON values from `raw`, so a user
    can paste several Cookie Editor exports (e.g. one per domain) in the same box.
    Blocks may be separated by whitespace, commas, or semicolons."""
    dec = json.JSONDecoder()
    s = raw.strip()
    out, i, n = [], 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n,;":   # skip separators between blocks
            i += 1
        if i >= n:
            break
        try:
            val, end = dec.raw_decode(s, i)
        except json.JSONDecodeError as exc:
            raise ValueError(f"That isn't valid JSON ({exc.msg} at line {exc.lineno}).") from exc
        out.append(val)
        i = end
    if not out:
        raise ValueError("That isn't valid JSON.")
    return out


def merge_storage_state(existing: dict | None, new: dict | None) -> dict | None:
    """Merge two storage_state dicts — cookies de-duped by (name, domain, path)
    with `new` winning, origins de-duped by URL. Either side may be None. So a
    fresh paste ADDS to / updates the stored set instead of replacing it."""
    def cks(s): return list((s or {}).get("cookies") or [])
    def ors(s): return list((s or {}).get("origins") or [])
    by_key = {}
    for c in [*cks(existing), *cks(new)]:          # new last → overwrites
        by_key[(c.get("name"), c.get("domain"), c.get("path", "/"))] = c
    by_origin = {}
    for o in [*ors(existing), *ors(new)]:
        by_origin[o.get("origin")] = o
    cookies, origins = list(by_key.values())[:200], list(by_origin.values())[:50]
    if not cookies and not origins:
        return None
    return {"cookies": cookies, "origins": origins}


def cookie_editor_to_storage_state(raw: str, allowed_host: str | None = None) -> dict | None:
    """Convert a Cookie Editor (cookie-editor.com) JSON export — a bare cookie
    array — or an existing Playwright storage_state into a Playwright
    storage_state dict ({"cookies": [...], "origins": [...]}). Accepts SEVERAL
    JSON blocks pasted together (e.g. cookies from two domains).

    Returns None if the input holds no usable cookies. Raises ValueError on
    malformed JSON or an unexpected shape, so callers can surface a clear error.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    items: list = []
    origins: list = []
    for data in _parse_json_blocks(raw):
        if isinstance(data, dict) and "cookies" in data:
            items += data.get("cookies") or []
            origins += data.get("origins") or []
        elif isinstance(data, list):
            items += data
        elif isinstance(data, dict) and data.get("name") and data.get("domain"):
            items.append(data)          # a single bare cookie object
        else:
            raise ValueError("Expected a cookie array (Cookie Editor export) or a storage_state object.")

    cookies: list[dict] = []
    for c in items:
        if not isinstance(c, dict) or not c.get("name") or not (c.get("domain") or "").strip():
            continue
        # Scope injected cookies to the monitor's domain (don't load cookies for
        # unrelated sites into the shared browser context).
        if not _domain_allowed(str(c["domain"]).strip(), allowed_host):
            continue
        exp = c.get("expires", c.get("expirationDate"))
        try:
            expires = float(exp) if exp not in (None, "") else -1.0
        except (TypeError, ValueError):
            expires = -1.0
        same_site = _SAMESITE.get(str(c.get("sameSite") or "").lower(), "Lax")
        secure = bool(c.get("secure", False))
        if same_site == "None" and not secure:
            same_site = "Lax"  # Playwright rejects SameSite=None without Secure.
        cookies.append({
            "name": str(c["name"]),
            "value": str(c.get("value", "")),
            "domain": str(c["domain"]).strip(),
            "path": c.get("path") or "/",
            "expires": expires,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": secure,
            "sameSite": same_site,
        })

    if not cookies:
        raise ValueError("No usable cookies found in the pasted JSON.")
    cookies = cookies[:200]   # bound stored state (defense-in-depth)

    # Scope pasted localStorage origins to the monitor host too (mirrors cookie
    # scoping), and bound the count.
    scoped_origins = []
    for o in (origins or [])[:50]:
        if not isinstance(o, dict):
            continue
        from urllib.parse import urlparse
        ohost = (urlparse(str(o.get("origin") or "")).hostname or "")
        if allowed_host is None or _domain_allowed(ohost, allowed_host):
            scoped_origins.append(o)
    return {"cookies": cookies, "origins": scoped_origins}


def session_cookie_count(flow: LoginFlow | None) -> int:
    """Number of cookies currently stored in a flow's session_state."""
    if not flow or not flow.session_state:
        return 0
    return len((flow.session_state or {}).get("cookies", []))


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
    valid_until = flow.session_valid_until
    # SQLite drops tzinfo on round-trip; treat a naive value as UTC so we don't
    # crash comparing it against the tz-aware utcnow().
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=timezone.utc)
    return valid_until > utcnow()


def mark_session(flow: LoginFlow, state: dict, ttl_hours: int = 12) -> None:
    flow.session_state = state
    flow.session_valid_until = utcnow() + timedelta(hours=ttl_hours)
