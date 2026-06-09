"""Idempotent workaround for a Playwright Firefox driver crash.

Newer Playwright driver bundles (e.g. 1.60) crash the Node driver when a page
emits a `pageerror` with no `location` — `pageError.location.url` throws, and
the protocol validator also rejects a missing string. Some anti-bot challenge
scripts (DataDome, etc.) trigger exactly this, which kills Camoufox/Firefox
renders mid-page.

This applies the same defensive guard newer Playwright versions ship
(optional chaining + safe defaults). It is:
  * idempotent — running it twice is a no-op,
  * safe — never raises; returns a human-readable status string,
  * version-agnostic — only touches files containing the buggy pattern.

The pinned `playwright==1.49.1` used in the Docker image is unaffected; this
mainly helps local installs that pull a newer Playwright (e.g. on Python 3.14).
"""

from __future__ import annotations

import pathlib

# (buggy substring, safe replacement). The replacements contain "?." so the
# buggy substring no longer matches after patching -> naturally idempotent.
_REPLACEMENTS = [
    ("pageError.location.url", 'pageError.location?.url || ""'),
    ("pageError.location.lineNumber", "pageError.location?.lineNumber || 0"),
    ("pageError.location.columnNumber", "pageError.location?.columnNumber || 0"),
]
_MARKER = "pageError.location.url"  # presence => unpatched


def _driver_lib_dir() -> pathlib.Path | None:
    try:
        import playwright
    except Exception:
        return None
    lib = pathlib.Path(playwright.__file__).parent / "driver" / "package" / "lib"
    return lib if lib.is_dir() else None


def apply(verbose: bool = False) -> str:
    """Patch the Playwright driver bundle(s) in place. Returns a status string."""
    lib = _driver_lib_dir()
    if lib is None:
        msg = "Playwright driver not found — nothing to patch."
        if verbose:
            print(msg)
        return msg

    patched, skipped, failed = [], 0, []
    for js in lib.glob("**/*.js"):
        try:
            text = js.read_text(encoding="utf-8")
        except Exception:
            continue
        if _MARKER not in text:
            skipped += 1
            continue
        new = text
        for old, rep in _REPLACEMENTS:
            new = new.replace(old, rep)
        if new == text:
            continue
        try:
            js.write_text(new, encoding="utf-8")
            patched.append(js.name)
        except Exception as exc:  # e.g. read-only install
            failed.append(f"{js.name}: {exc}")

    if failed:
        msg = f"Playwright patch failed for {len(failed)} file(s): {'; '.join(failed)}"
    elif patched:
        msg = f"Patched Playwright driver ({', '.join(sorted(set(patched)))})."
    else:
        msg = "Playwright driver already patched / not affected."
    if verbose:
        print(msg)
    return msg


if __name__ == "__main__":
    apply(verbose=True)
