#!/bin/sh
# Persist Camoufox's ~700MB browser in the mounted data volume (XDG_CACHE_HOME →
# /data/.cache/camoufox) so it survives image rebuilds instead of re-downloading on
# every container recreate. Fetch once, in the background, when absent — the web
# server starts immediately and Chromium/Firefox renders work while it downloads.
set -e
CACHE="${XDG_CACHE_HOME:-/data/.cache}"
mkdir -p "$CACHE"
if [ ! -d "$CACHE/camoufox" ]; then
  echo "entrypoint: Camoufox not in $CACHE — fetching once in the background…"
  (python -m camoufox fetch || echo "entrypoint: Camoufox fetch failed (will retry on first use)") &
fi
exec "$@"
