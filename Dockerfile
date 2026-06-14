# Watcher — single-image deployment with Playwright browsers + Camoufox bundled.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATCHER_DATA_DIR=/data \
    # Camoufox (and platformdirs) resolve their cache from XDG_CACHE_HOME. Point it
    # at the mounted /data volume so the ~700MB browser is downloaded ONCE and reused
    # across image rebuilds / container recreates, instead of re-fetched every time.
    XDG_CACHE_HOME=/data/.cache

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Camoufox is NOT baked into the image: it lives in the /data volume (see
# XDG_CACHE_HOME above) and is fetched once at first runtime start by entrypoint.sh,
# so it persists across rebuilds. (Chromium/Firefox/WebKit ship in the base image.)
COPY watcher ./watcher
COPY entrypoint.sh /app/entrypoint.sh

# Drop privileges: run as the non-root user shipped in the Playwright base image
# (so an app/browser-renderer compromise doesn't get root in the container).
RUN chmod +x /app/entrypoint.sh && mkdir -p /data/.cache \
    && chown -R pwuser:pwuser /app /data
USER pwuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/login').status<500 else 1)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]

# --proxy-headers makes request.client.host the real client IP from a TRUSTED
# proxy (so per-IP rate limiting works). Set FORWARDED_ALLOW_IPS to the proxy's
# address (defaults to 127.0.0.1; use the proxy container's IP/range otherwise).
CMD ["uvicorn", "watcher.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
