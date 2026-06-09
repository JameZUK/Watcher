# Watcher — Website / URL Change Monitor

A self-hosted, web-based tool to monitor any website or URL, detect and visualize
changes in an easy-to-understand way, and notify the user when something changes.

---

## 1. Overview

Watcher renders target pages with a real browser engine (Playwright / Camoufox),
captures snapshots, diffs each snapshot against the previous one, and surfaces
changes through a clean web UI and notification channels.

**Core loop:**

```
schedule → render (chosen engine) → capture snapshot → diff vs last snapshot
        → if changed: store change + notify
```

```
┌─────────────┐   schedules    ┌──────────────┐   renders   ┌─────────────┐
│  Web UI /   │──── monitors ──▶│  Scheduler/   │────────────▶│  Renderer    │
│  API        │                 │  Worker       │             │ (Playwright/ │
│ (Jinja2 +   │◀── history/diff │               │◀── snapshot │  Camoufox)   │
│  HTMX)      │                 └──────┬───────┘             └─────────────┘
│             │                        │ stores snapshot + computes diff
       ▲                        ┌──────▼───────┐
       │  views diffs           │  SQLite +     │   on change ┌─────────────┐
       └────────────────────────│  blob store   │────────────▶│ Notifiers   │
                                 │  (snapshots)  │             │ push/webhook│
                                 └──────────────┘             └─────────────┘
```

---

## 2. Decisions (locked)

| Dimension | Decision |
|---|---|
| **Detection modes** | Rendered text, visual/screenshot, CSS/XPath element, raw HTML/JSON |
| **Notifications** | In-app inbox + Web Push, Webhook (HMAC-signed) |
| **Scale / hosting** | Self-hosted, single box; **full user accounts** |
| **Stack** | FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind |
| **Rendering** | Playwright (Chromium default; Firefox/WebKit) + Camoufox, common interface |
| **Min check interval** | 15 min floor, 1 hr default, with jitter |
| **Authenticated sites** | MVP / day-one (encrypted creds + login-step replay) |
| **App auth** | Full user accounts (hashed passwords, sessions) |
| **Camoufox** | Bundled in the Docker image |

---

## 3. Tech stack

- **Python 3.12+**, **FastAPI** (async throughout)
- **Jinja2** server-rendered templates + **HTMX** (live updates, no JS build) +
  light **Alpine.js**; **Tailwind CSS** for a professional look (dark/light)
- **Playwright** (Chromium default; Firefox/WebKit) + **Camoufox** for anti-bot
  targets, behind one `Renderer` interface
- **SQLite** via SQLAlchemy/SQLModel; **APScheduler** (async) for scheduling
- **httpx** (webhooks), **pywebpush** + VAPID (Web Push)
- **argon2/bcrypt** (password hashing), **cryptography** (Fernet) for credential
  encryption at rest

---

## 4. Data model (SQLite)

- **User** — email, password_hash, created_at, is_active
- **Monitor** — user_id, name, url, engine, schedule (interval/cron), detection
  mode + config, selectors, ignore rules, viewport, wait conditions, login_flow_id,
  notification channel selection, enabled, created_at
- **Snapshot** — monitor_id, taken_at, http_status, render status, `rendered_text`,
  `html_path`, `screenshot_path`, `content_hash`, `dom_hash`, `extracted_value`,
  render metadata
- **Change** — monitor_id, from_snapshot_id, to_snapshot_id, detected_at,
  change_type, diff_summary, diff_blob/path, magnitude (% or char count), acknowledged
- **LoginFlow** — monitor_id, steps (ordered navigate/fill/click/wait),
  encrypted credentials, persisted session/cookies
- **Notification** — change_id, channel, status, sent_at
- **PushSubscription** — user_id, endpoint, keys
- **Settings** — per-user defaults, webhook endpoints, retention, proxies

---

## 5. Rendering pipeline (engine abstraction)

One contract — `Renderer.render(monitor) -> RenderResult` — implemented by
`PlaywrightChromium`, `PlaywrightFirefox`, `PlaywrightWebKit`, and `Camoufox`.

Steps per render:
1. (Optional) replay **LoginFlow**, reusing persisted session/cookies when valid
2. Navigate
3. Wait — `load` / `networkidle` / selector / timeout
4. Optional actions — scroll, dismiss cookie banner, click
5. Capture — **HTML + visible text + full-page screenshot** (+ element subtree for
   selector watches, + parsed JSON when `application/json`)

Controls: concurrency cap, per-render timeout, browser context reuse, optional proxy.

---

## 6. Detection / diffing

- **Text** — normalized visible-text diff (difflib unified/side-by-side); optional
  whitespace + number/date normalization
- **Visual** — full-page screenshot pixel-diff (pixelmatch-style) → % changed +
  highlighted-region overlay
- **Element** — extract selector's text/attribute → exact compare; numeric values
  rendered as a trend chart
- **HTML/JSON** — normalized DOM diff / DeepDiff for JSON

**Noise control (key to usability):** per-monitor ignore selectors (strip
ads/timestamps before diffing), regex ignore patterns, and a minimum-change
threshold (% or char count) so trivial churn doesn't trigger alerts.

---

## 7. Scheduling

- **APScheduler** async, in-process; each monitor → interval or cron trigger
- **15 min floor, 1 hr default**, with jitter to avoid thundering herd
- Manual "Check now"; retry with backoff on render errors

---

## 8. UI / UX (server-rendered + HTMX)

- **Auth** — register / login / logout, session-protected
- **Dashboard** — monitor cards: status, last checked, last change,
  change-frequency sparkline, quick enable/disable + "Check now"
- **Monitor detail** — snapshot/change timeline + diff viewer:
  side-by-side/unified text, visual before/after slider + highlight overlay,
  selector-value chart
- **Add/edit wizard** — URL → live preview render → **visual element picker**
  (click the page to generate a selector) → detection mode → schedule →
  (optional) login flow → notifications
- **Change inbox** — unacknowledged changes, ack/dismiss
- **Settings** — webhook endpoints, push subscriptions, defaults, retention, proxies

---

## 9. Notifications

- **In-app inbox** — change feed, ack/dismiss
- **Web Push** — service worker + VAPID, real browser notifications
- **Webhook** — HMAC-signed JSON payload with diff summary + links
- Per-monitor throttling / quiet hours / digest to prevent alert fatigue

---

## 10. Storage & retention

- SQLite for metadata; **content-addressed** blob store on disk (dedupes identical
  snapshots)
- Retention: keep last N snapshots or M days; **always preserve change-point
  snapshots**; periodic prune job

---

## 11. Security

- Full user accounts; argon2/bcrypt password hashing; secure session cookies; CSRF
  on forms
- **Encrypted credential storage** (Fernet) for login flows — requires an app
  secret key (env)
- SSRF awareness: fetching arbitrary URLs is the product's purpose, but guard
  against internal-network targets when exposed; run browsers sandboxed

---

## 12. Deployment

- Single **Docker** image (app + Playwright browsers + **Camoufox bundled**)
- `docker-compose` with one data volume; env-based config; healthcheck

---

## 13. Suggested project layout

```
watcher/
  app/         main.py, config, deps
  models/      SQLAlchemy models + Pydantic schemas
  engines/     base.py, playwright_chromium.py, playwright_firefox.py, camoufox.py
  detection/   text.py, visual.py, element.py, structured.py, noise.py
  auth/        users.py, sessions.py, login_flows.py (record/replay + crypto)
  scheduler/   jobs.py (APScheduler)
  notify/      push.py, webhook.py, inbox.py
  web/         routes/, templates/ (Jinja2), static/ (css, htmx, sw.js)
  storage/     blobs.py, retention.py
  data/        watcher.db, blobs/   (volume-mounted)
```

---

## 14. Roadmap

- **MVP** — user accounts; Chromium + Camoufox; text/element/visual/HTML-JSON diff;
  authenticated-site monitoring (login record/replay); dashboard + detail + diff
  viewer + element picker; interval scheduling; in-app + Web Push + webhook;
  SQLite/blob storage; Docker
- **Phase 2** — noise-rule UI polish, retention tuning, digests / quiet hours,
  proxy management, JSON/API monitoring UX
- **Phase 3** — multi-user sharing/teams, advanced anti-bot tuning, export/reporting
