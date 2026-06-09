"""Content-addressed blob store on the filesystem.

Blobs are keyed by the sha256 of their content, so identical snapshots
(unchanged pages) are stored exactly once. Files are sharded by the first
two hex chars to avoid huge flat directories.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..config import settings


def _path_for(key: str) -> Path:
    return settings.blobs_dir / key[:2] / key


def put_bytes(data: bytes) -> str:
    """Store bytes, returning the content key (sha256 hex)."""
    key = hashlib.sha256(data).hexdigest()
    path = _path_for(key)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish write
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return key


def put_text(text: str) -> str:
    return put_bytes(text.encode("utf-8"))


def get_bytes(key: str) -> bytes | None:
    path = _path_for(key)
    return path.read_bytes() if path.exists() else None


def get_text(key: str) -> str | None:
    data = get_bytes(key)
    return data.decode("utf-8", errors="replace") if data is not None else None


def exists(key: str) -> bool:
    return _path_for(key).exists()


def delete(key: str) -> None:
    path = _path_for(key)
    if path.exists():
        path.unlink()
