# Changelog

All notable changes to Watcher are documented here. Dates are ISO-8601.

## 2026-06-11 — Automatic cookie / consent / ad & overlay handling

Render pages the way a person sees them after dismissing the noise, so
screenshots and visual diffs aren't polluted by banners, ads, and pop-ups.
All of it is generic and site-agnostic — no per-site rules.

### Added
- **Ad & tracker blocking** during render — network-level abort of ~45
  ad/analytics/tracker domains (uBlock-style; works headless on both engines).
- **Generic cookie/consent banner handling** — detects consent overlays by
  wording *and* by the universal "accept-all + manage/reject" button signature,
  then accepts (or dismisses/hides). Covers positioned overlays, static
  edge-anchored bars (e.g. GOV.UK), dialogs that fill a dedicated CMP
  `<iframe srcdoc>` (e.g. Le Figaro), and cross-origin iframe CMPs (Sourcepoint).
- **Late-mounting CMP handling** — a short-lived MutationObserver keeps
  dismissing banners as they appear, so CMPs that load seconds after the page
  are still cleared before the screenshot.
- **Multilingual** accept/reject/dismiss/consent terms (EN + FR/DE/ES/IT/PT/NL),
  so non-English banners are accepted, not merely hidden.
- **Promo / interstitial dismissal** — closes sign-in / newsletter / app-install
  nags and toasts via their explicit close control (e.g. BBC's "Close sign in
  banner"); close-matching is restricted to UI phrasing so it can never click a
  destructive button like "Close account".
- **Per-monitor manual override** — a "Consent / dismiss clicks" field: CSS
  selectors clicked in order after load, for stubborn cookie walls, modals, or a
  captcha checkbox the auto-handler misses.
- **AI self-healing fallback** — when the heuristic can only hide a blocking
  consent wall, or a large positioned overlay/paywall survives every pass, its
  HTML is sent to the model to learn a dismiss selector, cached per-monitor.
  Bounded to a single AI call per monitor (`consent_ai_tried`) so a persistent
  unsolvable overlay can't re-spend tokens every render.
- **Actionable "needs your help" alert** — when a render is blocked by a
  captcha / anti-bot wall or login gate (DataDome, Cloudflare, …), the failure
  notification now tells the user to add this monitor's Session cookies instead
  of surfacing a dead-end error.
- **`scripts/test_consent.py`** — a recursive multi-site validation harness
  (renders through Camoufox so Cloudflare-protected sites actually load; reports
  surviving banners, "walls", and late-appearing banners).
- **Block / challenge pages are now captured for review.** Previously a blocked
  render stored only the error text and threw the page away; now the failure
  path persists the interstitial's screenshot + HTML on the error snapshot
  (content-addressed, so a repeated identical block dedupes to one image). The
  "Last check failed" banner gains a *"View the captured block page"* link, and
  the History scrubber includes block pages tagged *"blocked / challenge page"* —
  so you can actually see what a site (e.g. DataDome on JBL) served, and improve
  the handling from real evidence.
- **Groups can hide their pages from the dashboard.** A per-group *"Hide these
  pages from the dashboard"* toggle collapses the group's member monitors off
  the main grid (the group card stays, as a folder); the pages are then viewable
  inside the group as full dashboard-style cards (live preview, status, "new"
  badges). The monitor card grid is now a shared partial used by both the
  dashboard and the group page so they render identically; the inbox total still
  counts hidden monitors' changes. New `Group.hide_members` column.

### Changed
- **Dashboard cards cleaned up across large / compact / list views.** The
  detection-mode pill ("Smart"/"Text"/…) moved off every thumbnail into quiet
  footer text, so previews show just the page; only meaningful status badges
  ("N new", "last check failed") remain on the image. The list view shows status
  as inline chips in the row instead of overlaying the narrow thumbnail. Mobile
  fixes: list rows truncate instead of overflowing (and keep a slim mode + when
  footer plus the status chips), and the dashboard now defaults to the compact
  view below 640px (an explicit view choice — including large — is remembered
  and overrides the default). Card markup is shared between the dashboard and
  group pages via one partial.
- New `Monitor` columns `block_annoyances` (default on), `consent_clicks`, and
  `consent_ai_tried`, added via idempotent startup migrations.
- The capture pipeline now runs `click_consent` → auto-dismiss observer →
  `hide_banners` (twice) → a final pre-screenshot sweep, on the main frame and
  every child frame.
- **"Check now" is now an async, progress-aware flow.** It used to fire a
  background job and immediately redirect, so the page reloaded showing the
  *previous* capture with no sign anything was happening. Now the button shows a
  spinner + "Checking…" with a status line, polls the new
  `GET /monitors/{id}/check-status` until a snapshot newer than the baseline
  lands (covering success *and* failure), and only then reloads — so you always
  see the latest render, never the stale one. `POST /monitors/{id}/check`
  returns JSON (baseline id, or a cooldown countdown); the scheduler tracks
  in-flight checks (`is_checking`) so a reload mid-check resumes the live state;
  a 90s timeout guards against a hung render.
- **Session cookies — multi-domain, multi-block, merge & manage.** The cookie
  box now accepts **several JSON exports pasted together** (one per domain) and,
  via a new *"Keep cookies for all domains"* toggle, keeps cookies from **every**
  domain in the paste — needed for federated/SSO logins (e.g. Glassdoor signs in
  through `indeed.com`, so its auth cookies live on a different domain and were
  previously rejected). A paste now **merges into** the stored set by default
  (de-duped by name+domain+path, newest wins) so you can add/update without
  re-pasting everything; *"Replace stored cookies"* and *"Clear all stored
  cookies"* override that. A per-domain summary shows what's currently stored.

### Fixed
- **Full-page screenshots no longer come out tall but mostly blank** on sites
  that lazy-load content and leave a scroll-lock engaged (e.g. BBC News, where
  ~90% of the image was empty below the first fold). `reveal_full_content()` now
  runs before each full-page screenshot (desktop + mobile, both engines): it
  forces lazy `<img>`/`<iframe>` to load and un-clamps top-level layout wrappers
  pinned to ~one viewport. BBC News went from ~10% to ~99% rendered; Guardian,
  Amazon, Wikipedia, GOV.UK and eBay were unchanged (no regression). Pre-existing
  issue, unrelated to the banner handling.
- **Mobile previews of UA-sensitive sites (e.g. Amazon) render correctly.** The
  Chromium/Playwright engine captured "mobile" by resizing the viewport to 390px
  while keeping the desktop user-agent, so Amazon served desktop markup at phone
  width — the page overflowed (~1000px-wide document) and the preview came out
  far too wide with a large blank area. The mobile preview is now a dedicated
  phone-emulated pass (mobile UA + touch where supported; UA+viewport fallback on
  Firefox/WebKit) that re-navigates so the site serves its real mobile layout and
  re-runs consent handling there. Amazon mobile went from 2000px (broken) to
  780px with a correct layout; desktop unchanged. (Camoufox already did this.)

### Notes / limitations
- An LLM **cannot** solve real image/slider captchas (reCAPTCHA grids, hCaptcha,
  DataDome sliders). Those escalate to the "needs your help" alert; the reliable
  fix is session-cookie injection or a residential proxy. Engine choice
  (Firefox vs Camoufox) does not affect IP-reputation-based DataDome blocking.
- The app runs as a long-lived uvicorn without `--reload`; code changes require
  a restart to take effect, and existing snapshots keep their previous
  screenshots — only checks after a restart render clean.
