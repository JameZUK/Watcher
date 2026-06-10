# Watcher — single-image deployment with Playwright browsers + Camoufox bundled.
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATCHER_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browsers ship in the base image; fetch Camoufox so it works offline.
RUN python -m camoufox fetch || true

COPY watcher ./watcher

# Drop privileges: run as the non-root user shipped in the Playwright base image
# (so an app/browser-renderer compromise doesn't get root in the container).
RUN mkdir -p /data && chown -R pwuser:pwuser /app /data
USER pwuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/login').status<500 else 1)" || exit 1

# --proxy-headers makes request.client.host the real client IP from a TRUSTED
# proxy (so per-IP rate limiting works). Set FORWARDED_ALLOW_IPS to the proxy's
# address (defaults to 127.0.0.1; use the proxy container's IP/range otherwise).
CMD ["uvicorn", "watcher.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
