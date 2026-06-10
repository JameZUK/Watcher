"""Web layer: templates, static files, routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

_HERE = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _timeago(value: datetime | None) -> str:
    if not value:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - value
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _pct(value: float | None) -> str:
    return f"{(value or 0) * 100:.1f}%"


def _interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


def _url_host(url: str | None) -> str:
    """The bare hostname (no scheme, no www.) — the most pertinent bit of a URL."""
    from urllib.parse import urlparse
    try:
        host = (urlparse((url or "").strip()).hostname or "").removeprefix("www.")
    except ValueError:
        host = ""
    return host or (url or "")


def _url_path_short(url: str | None, max_len: int = 40) -> str:
    """A compact path/query tail to pair with the host, e.g. '/JBLBB3WIFI.html?…'.
    Empty for a bare domain. Long paths are middle-ellipsised."""
    from urllib.parse import urlparse
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return ""
    path = p.path or ""
    if path in ("", "/"):
        return "?…" if p.query else ""
    if len(path) > max_len:
        head, tail = path[: max_len // 2], path[-(max_len // 2 - 1):]
        path = f"{head}…{tail}"
    return path + ("?…" if p.query else "")


templates.env.filters["timeago"] = _timeago
templates.env.filters["pct"] = _pct
templates.env.filters["interval"] = _interval
templates.env.filters["url_host"] = _url_host
templates.env.filters["url_path_short"] = _url_path_short
