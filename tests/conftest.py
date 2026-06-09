"""Test bootstrap: configure env before any watcher module is imported."""

import os
import tempfile

os.environ.setdefault("WATCHER_SECRET_KEY", "test-ci-secret")
os.environ.setdefault("WATCHER_DATA_DIR", tempfile.mkdtemp(prefix="watcher-test-"))
