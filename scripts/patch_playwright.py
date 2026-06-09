#!/usr/bin/env python3
"""Apply the Playwright Firefox driver workaround (see watcher._playwright_patch).

Run after installing/updating dependencies if you use the Camoufox engine on a
newer Playwright than the pinned 1.49.1:

    python scripts/patch_playwright.py

Idempotent and safe to run repeatedly.
"""

import pathlib
import sys

# Make the repo root importable when run directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from watcher._playwright_patch import apply  # noqa: E402

if __name__ == "__main__":
    apply(verbose=True)
