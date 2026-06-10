<p align="center">
  <img src="watcher-logo.svg" alt="Watcher" width="200">
</p>

<p align="center">
  <strong>A self-hosted tool to monitor any website or URL for changes — and tell you the moment something moves.</strong>
</p>

<p align="center">
  Pages are rendered in a <em>real browser</em> (Playwright / Camoufox), so JavaScript-heavy and
  anti-bot–protected sites work where naïve HTML-diff scrapers fail.
</p>

<p align="center">
  <a href="https://github.com/JameZUK/Watcher/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/JameZUK/Watcher/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white">
  <img alt="Playwright" src="https://img.shields.io/badge/Playwright-Chromium%20%7C%20Firefox%20%7C%20WebKit-2EAD33?logo=playwright&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-single%20image-2496ED?logo=docker&logoColor=white">
</p>

---

## ✨ Features

- **Smart detection (default)** — watches a page's **content (text) and appearance (visual) together**, and shows you both in a tabbed diff. Other modes: rendered **text**, **visual** (screenshot), single **element** (CSS/XPath), raw **HTML**, and **JSON**.
- **Real browser rendering** — Chromium / Firefox / WebKit via Playwright, plus **[Camoufox](https://github.com/daijro/camoufox)** (stealth Firefox) for anti-bot targets. Desktop **and** mobile previews are captured each check.
- **Sees changes in place** — dashboard cards show live screenshot thumbnails; the diff viewer overlays **changed regions in red** on the actual page, with an interactive **before/after slider**.
- **AI triage** *(optional)* — an OpenRouter-hosted model (or any OpenAI-compatible / **Ollama** endpoint) writes a one-line **headline**, classifies the change (price / stock / content / cosmetic …), and rates **importance** — so low-value churn is muted and only what matters interrupts you. It can also **auto-configure a monitor from a plain-English goal**, **suggest what to watch for**, and **summarise** a monitor's recent activity.
- **Value tracking & trends** — extract a numeric value (price, stock count, rating) each check, chart it over time, and fire **threshold alerts** ("tell me when it drops below £300").
- **Notifications** — in-app **inbox**, **Web Push**, **HMAC-signed webhooks**, **Email/SMTP**, **Telegram**, **Discord**, and **ntfy** — with **hourly digests** and **quiet hours** so non-urgent changes batch up instead of pinging you at 3am.
- **Reliability** — per-check render/detect timeouts, retries, **auto-pause** after repeated failures (with a recovery alert), and **adaptive intervals** that speed up on active pages and back off on quiet ones.
- **Anti-bot & authenticated sites** — Camoufox stealth rendering; blocked / challenge pages (DataDome, Cloudflare, PerimeterX, …) are detected and surfaced instead of silently failing; **paste Cookie Editor JSON** to reuse a logged-in session or clear a bot check; recorded **login flows** and **per-monitor proxies**. Credentials and sessions are **encrypted at rest**.
- **Noise control** — ignore selectors, ReDoS-safe regex ignore patterns, a per-monitor minimum-change threshold, **and a global visual noise floor** that absorbs anti-aliasing, lazy-loaded images, and carousels so trivial pixel churn never alerts you.
- **Organise & integrate** — **tags** + search, JSON **import/export**, a token-authed **REST API**, an **RSS** feed, and a **browser extension** to add the current tab in one click.
- **Futuristic, fully-responsive UI** — dark glassmorphic design that works on desktop and mobile (pinch-to-zoom snapshots), with an **Auto / Full / Lite** effects toggle for devices without GPU acceleration.
- **Single container** — SQLite + a content-addressed blob store. No Redis, no Postgres, no external services.

## 📸 Screenshots

| Dashboard | Monitor detail (diff + history) |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Monitor detail](docs/screenshots/detail.png) |

<p align="center"><img alt="Mobile" src="docs/screenshots/mobile.png" width="300"></p>

## 🚀 Quick start (Docker)

```bash
git clone https://github.com/JameZUK/Watcher.git
cd Watcher
cp .env.example .env          # then set WATCHER_SECRET_KEY (and ideally WATCHER_ENCRYPTION_KEY)
docker compose up --build
```

Open **http://localhost:8000**, create an account, and add your first monitor. The Docker image bundles the Playwright browsers **and** Camoufox, so it works out of the box.

## 🧑‍💻 Local development

> Python **3.11–3.13** is recommended — the pinned dependencies have prebuilt wheels there. On 3.14+, install the latest releases instead of the pins, or just use Docker.
>
> **Camoufox + Playwright:** Camoufox is validated against the pinned `playwright==1.49.1` (used in the Docker image). If you force a much newer Playwright (e.g. on Python 3.14), its bundled Firefox driver can crash on pages that emit a location-less `pageerror` (some anti-bot challenge scripts do). Watcher **auto-applies an idempotent workaround on startup**; you can also run it manually after installing/updating deps, or disable it with `WATCHER_PATCH_PLAYWRIGHT=false`:
>
> ```bash
> python scripts/patch_playwright.py
> ```

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # browsers for local rendering
python -m camoufox fetch             # optional, for the Camoufox engine

export WATCHER_SECRET_KEY=dev-secret
uvicorn watcher.main:app --reload
```

## 🏗 How it works

```
schedule (APScheduler) → render (Playwright / Camoufox) → snapshot
   → diff vs previous (detection + noise floor) → AI triage (optional)
   → store + notify (inbox / push / webhook / email / telegram / discord / ntfy)
```

- **Engines** implement one `Renderer` contract, so detection is engine-agnostic.
- Every render captures HTML, visible text, desktop + mobile screenshots, and (for element/value modes) a selector or numeric value.
- Snapshots are **content-addressed** on disk — identical (unchanged) pages are stored once.
- AI triage and value extraction are **skipped on byte-identical pages** to save calls; non-urgent changes are deferred to the hourly **digest**.
- A daily job prunes old snapshots but always preserves change-point snapshots, so diff history stays intact.

See **[SPEC.md](SPEC.md)** for the full design.

## ⚙️ Configuration

All settings are environment variables prefixed `WATCHER_` (see [`.env.example`](.env.example)). The essentials:

| Variable | Default | Purpose |
|---|---|---|
| `WATCHER_SECRET_KEY` | *(dev placeholder)* | Signs session cookies **and** webhook payloads. **Set this in production.** |
| `WATCHER_ENCRYPTION_KEY` | *(derived)* | Fernet key for encrypting login credentials. Set explicitly in production. |
| `WATCHER_DATA_DIR` | `data` | Where the SQLite DB and snapshot blobs live. |
| `WATCHER_MIN_INTERVAL_SECONDS` | `900` | Floor on how often a monitor can check (15 min). |
| `WATCHER_MIN_VISUAL_CHANGE` | `0.005` | Global **visual noise floor** (fraction of pixels). See below. |
| `WATCHER_AUTO_PAUSE_AFTER_FAILURES` | `6` | Pause a monitor after this many consecutive failures (`0` disables). |
| `WATCHER_RENDER_TIMEOUT_SECONDS` / `_DETECT_TIMEOUT_SECONDS` | `45` / `20` | Hard ceilings per render / per diff. |
| `WATCHER_VAPID_PUBLIC_KEY` / `_PRIVATE_KEY` | — | Enable Web Push. Generate with `pip install py-vapid && vapid --gen`. |

> **AI triage** and the **Email/SMTP, Telegram, and ntfy** credentials are configured in the **web UI** (Settings — global settings are admin-only) and stored **encrypted at rest**, not via environment variables.

### Tuning detection sensitivity (avoiding phantom changes)

Visual diffing on real sites is noisy — anti-aliasing, lazy-loaded images, ad/carousel rotation, and sub-pixel font rendering all shift a few pixels between otherwise-identical renders. Watcher suppresses that noise on two levels:

- **`WATCHER_MIN_VISUAL_CHANGE`** (default `0.5%`) is a global floor: a visual change must move at least this fraction of the screenshot's pixels to count. Anti-aliased pixels are ignored entirely.
- Each monitor's own **minimum-change threshold** (in its settings) raises that floor further for a specific, busy page.

The *effective* visual threshold is `max(monitor threshold, WATCHER_MIN_VISUAL_CHANGE)`. Content (text) and tracked-value changes are exact and are **not** subject to this floor.

### Webhook verification

Payloads are signed with HMAC-SHA256 in the `X-Watcher-Signature: sha256=<hex>` header:

```python
import hmac, hashlib
expected = "sha256=" + hmac.new(SECRET.encode(), request.body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(request.headers["X-Watcher-Signature"], expected)
```

## 📁 Project layout

```
watcher/
  config.py, db.py, models.py, runner.py   # core (settings, ORM, the check pipeline)
  netsec.py     SSRF guards (scheme + resolved-IP validation, per-redirect-hop)
  engines/      Playwright + Camoufox behind one interface
  detection/    text / visual / element / structured diffing + noise control
  ai/           OpenRouter/Ollama triage, value extraction, auto-config, summaries
  auth/         users, password hashing (argon2), credential crypto, login flows
  scheduler/    APScheduler jobs (checks, prune, hourly digest, adaptive retune)
  notify/       inbox, push, webhook, email, telegram, discord, ntfy + digests
  storage/      content-addressed blobs + retention
  web/          FastAPI routes (incl. REST API + RSS), Jinja2 templates, assets
extension/      one-click "add this tab" browser extension
```

## 🔒 Security notes

Watcher is built to be exposed to the public internet behind a TLS-terminating reverse proxy. Key controls:

- **Secrets**: a strong `WATCHER_SECRET_KEY` is **required** — the app refuses to start with the dev placeholder (override in local dev only with `WATCHER_ALLOW_INSECURE=1`). Credentials, session state, and global API keys (OpenRouter/SMTP/Telegram) are encrypted at rest with Fernet (key HKDF-derived from the secret, or set `WATCHER_ENCRYPTION_KEY` explicitly in prod). API tokens are stored **hashed** (shown once on generation).
- **Accounts**: self-registration is closed by default (`WATCHER_REGISTRATION_OPEN=false`) — only the first/bootstrap admin account can self-register; create further users as admin or open registration deliberately. Login/registration are **rate-limited** per IP. Global settings are gated behind an **admin** role.
- **SSRF**: every render target, proxy, login URL, webhook, and ntfy/Discord destination is scheme-checked and its resolved IPs validated — private / loopback / link-local / CGNAT / metadata ranges (incl. IPv4-mapped & NAT64 forms) are blocked, re-checked just before each render. Set `WATCHER_ALLOW_PRIVATE_TARGETS=true` only on trusted networks that intentionally monitor internal hosts. A real browser re-resolves DNS, so for untrusted exposure **also place the renderer behind an egress firewall** that blocks internal ranges.
- **Web hardening**: a fail-closed Origin/Referer **CSRF** guard on cookie-authed writes; **security headers** (X-Frame-Options, nosniff, Referrer-Policy, Permissions-Policy, HSTS when `WATCHER_SECURE_COOKIES=true`); request **body-size limit**; CDN scripts pinned with **SRI**; API docs (`/docs`) off by default (`WATCHER_ENABLE_DOCS`).
- **Abuse / DoS**: per-user monitor cap, manual-check cooldown, ReDoS-safe regex (timeout), bounded screenshot diffing, capped change retention, and outbound timeouts on every notifier.
- **Deployment**: the container runs as a **non-root** user; `docker-compose.yml` binds to loopback (front it with Caddy/nginx/Traefik for TLS). Set `WATCHER_SECURE_COOKIES=true` and `WATCHER_TRUSTED_HOSTS=<public-host>` behind the proxy.

## 📄 License

See [LICENSE](LICENSE).
