# Changelog

All notable changes to Watcher are documented here. Dates are ISO-8601.

## 2026-06-13 — Title-aware headlines + a collapsible home summary

### Changed
- **Headlines read the page title, not just the URL.** Triage and the dashboard summary
  now use the page title to understand *what* a page is and to name the site by its
  human name (e.g. "The Register", "John Lewis", "Stock Analysis"), falling back to the
  diff/domain when a title is generic (a shop's homepage title on a product page). The
  fleet summary receives each change's page title alongside its domain.
- **The home summary is a TLDR by default.** The AI paragraph is collapsed to two lines
  with a "Show more" toggle to expand the full detail (links preserved), so it doesn't
  dominate the dashboard.
- Re-triaged the existing text-diff change headlines through the improved prompt so the
  history is site/title-aware too (value/visual-only changes were left as-is).

### Changed
- **Headlines name the real site, not just your saved label.** Change headlines (on
  the monitor pages) and the dashboard summary now situate each change on the actual
  site/retailer from its domain and page title — e.g. "price dropped on JBL UK",
  "on The Register", "Anthropic.com added…" — instead of referring to a page only by
  the name it's saved under in Watcher. Triage is given the page domain explicitly, and
  the fleet summary receives each change's domain.

## 2026-06-13 — Dashboard summary: grounded prose + no reload needed

### Fixed
- **Dashboard summary no longer embellishes.** It claimed a product was "now available"
  from a price-drop headline (the item was out of stock). The summary is now strictly
  grounded in the change headlines — a price/'cheapest' headline is treated as price
  only, and the words "available"/"in stock" are forbidden unless a headline contains
  them — and it can't invent prices/values/states. Also fixed truncated output (the
  per-run JSON was hitting the token cap and falling back to the plain list).
- **The summary paragraph appears without a reload.** It generates in the background;
  the page used to show the old headline list until you reloaded. The dashboard now
  polls `/dashboard/summary` and swaps the paragraph in as soon as it's ready.

A monitor watching Glassdoor "only for new reviews" kept alerting on *new analyst
reviews* that didn't exist — the page's rotating **jobs widget** churned every check
and the AI latched onto "Sr. Analyst" in a job title. Fixed generally, with zero
per-site code (the tool stays fully universal).

### Added
- **AI page profiler.** For a monitored page, Watcher now derives a short, plain-
  language understanding of its regions — *what's a real change* vs *what's incidental
  churn* (rotating jobs/ads/recommendations, counts, timestamps, pagination) — entirely
  from the page itself and the user's watch instruction. It's stored per-monitor and
  fed into every triage so the model judges *this* page's scope correctly. Generated
  automatically on the first good capture of an intent-watched page, and re-runnable
  on demand from a "Page understanding" card on the monitor. Editing "what to watch
  for" auto-refreshes the understanding (it's derived from the intent), so the two
  never drift out of sync.
- **Structural churn learning.** Watcher now tracks which *lines* of a page change on
  nearly every check (digit-masked, so incrementing counters / timestamps / view-counts
  count as the same churning line) and feeds that observed signal into triage and the
  page profiler — "these keep flipping, almost certainly incidental." It's an input the
  intent-aware AI weighs, never a silent veto, so a value the user *is* watching (a price
  that ticks each check) is still surfaced. No AI and no per-site rules to learn it.

### Changed
- **Triage prompt hardened.** It must ground every claim in the actual diff lines (a
  keyword in unrelated content — a job title, nav item, ad — is not that thing), treat
  the watch instruction as defining scope (an excluded/unrelated change is *noise* no
  matter how large), never manufacture a match, and never infer a specific content
  change (reviews/prices/stock) from a screenshot-only diff.
- **Out-of-scope churn is dropped.** When a watch instruction is set, a change the AI
  rates pure *noise* is dropped outright (not just muted), so unrelated page churn never
  reaches the timeline.

## 2026-06-13 — Whole-page section captures, smarter dashboard summary & capture fixes

### Added
- **Dashboard summary as an AI paragraph with embedded links.** The fleet summary at
  the top of the dashboard is now a natural-language paragraph that describes what
  changed across all your sites and highlights what's interesting, with the relevant
  phrases linking straight to each monitor — instead of a flat list. Generated in the
  background (never blocks the page), cached per user by a change fingerprint, and
  safe by construction: the model returns link-aware *segments* whose text is escaped
  and whose monitor ids are validated against your own changes. Falls back to the
  headline list when AI isn't configured or hasn't generated yet.
- **Customisable dashboard summary** (Settings → Dashboard summary): turn the AI
  paragraph on/off, set the look-back window (1–30 days), and give a free-text focus
  / tone hint (e.g. "lead with price drops; be terse") that steers the model without
  overriding its safety rules. Editing any of these regenerates the paragraph.
- **Whole-page captures as readable, full-width sections.** Very long pages (news
  homepages, infinite-ish feeds) are now captured in full and sliced top-to-bottom
  into sharp, full-width sections (`screenshot_section_height_px`, capped at
  `max_screenshot_sections`) rather than cropped to the top or squished illegibly into
  one image. The history viewer stacks the sections to reconstruct the whole page
  (framed as a phone for mobile). The snapshot image route gained `?section=N`; the
  first section stays the primary blob, so thumbnails/dashboard/RSS/diff are unchanged.

### Fixed
- **Mobile preview lost on SPA / shadow-DOM pages.** The mobile pass discarded a
  freshly-rendered page when its `innerText` read low — but modern e-commerce SPAs
  (e.g. JBL) render into shadow DOM and hydrate asynchronously, so a perfectly good
  page intermittently read as "empty" and the UI fell back to the desktop image. It
  now keeps the capture unless the page actually looks like a challenge interstitial,
  and waits on a populated DOM rather than `innerText`. Fixes both engines' mobile pass.
- **Tall captures failed to compress / truncated.** A `clip` without `full_page=True`
  was silently constrained to the viewport (truncating long mobile pages to one
  screen); and a true full-page capture of a very tall page tripped Pillow's
  decompression-bomb guard and WebP's 16383-px dimension limit, so it fell back to a
  raw multi-MB PNG. Captures are now sliced into per-section WebPs that always encode.
- **`detect()` timing out on tall pages.** The visual diff padded two captures with
  different aspect ratios onto an oversized common canvas, so the pure-Python
  pixelmatch blew the detect timeout and aborted change detection for the cycle (a
  long page like The Register stopped reporting changes). The padded canvas is now
  re-bounded to the diff megapixel budget.

## 2026-06-12 — Self-healing logins, proxy pool, status page & a hardening sweep

### Added
- **Automatic AI re-login on session expiry.** Opt-in per monitor: when a check
  fails because the stored login session expired, the AI agent re-logs in
  *headlessly* (no human), refreshes the session, and re-checks. Credential logins
  only — a new unattended agent mode gives up cleanly the moment it would need a
  captcha or one-time code (no captcha solving). Gated by the per-user AI budget +
  an in-flight guard + a persistent post-failure cooldown; runs out-of-band so it
  never holds a render slot. A successful self-heal drops an inbox note.
- **Auto-escalate the engine on a bot block.** When a non-stealth render is bot-
  walled (Cloudflare/403/…), it retries once on **Camoufox**; if that clears the
  wall the monitor is switched to Camoufox permanently (a failed escalation sets a
  short cooldown to avoid double-rendering every check).
- **Status page** (`/status`, in the nav) — per-monitor check count + success rate
  and avg/max render time, plus summary cards: monitors enabled/paused, how many
  are failing, AI calls used vs the per-window budget, and the on-disk snapshot
  blob-store size.
- **Shared proxy pool.** Admins paste a list of proxy URLs (Settings → transports);
  a job health-checks them every ~10 min and round-robins healthy ones to monitors
  that opt in (and have no explicit proxy) — useful against IP-reputation walls.

### Security
- **Self-hosted Tailwind / htmx / Alpine + same-origin CSP.** Dropped the Tailwind
  Play CDN (an unpinnable runtime) and the unpkg scripts; the CSS is now prebuilt
  from the templates and all JS is vendored. `script-src` is `'self'` only — a CDN
  compromise can no longer execute JS in the app.
- **TOTP codes are single-use.** A code accepted at login can't be replayed inside
  its ~90s window (a new `last_otp_step` rejects any code at or below the last used).
- **CSRF Origin check now validates the port.** A same-host origin on a *different*
  port (e.g. another app on localhost) is rejected; proxy deployments (port-less
  Host) are unaffected.
- **No login user-enumeration via timing.** Unknown/inactive logins now run a
  constant-time dummy password hash so they can't be distinguished by response time.

### Performance
- **Cache decrypted secrets** (OpenRouter key, SMTP/Telegram) by ciphertext — they
  were Fernet-decrypted up to ~3× per check.
- **Per-engine concurrency cap** — Camoufox (RAM-heavy) gets a tighter nested cap
  (`WATCHER_MAX_CAMOUFOX_CONCURRENCY`, default 2).
- **Retention pruning in SQL** — one windowed `DELETE` instead of loading every
  snapshot row per monitor into Python.
- **Screenshots stored as size-capped WebP.** Captures are downscaled to a
  megapixel budget (`WATCHER_MAX_SCREENSHOT_MEGAPIXELS`, default 12 — retina is
  overkill for storage, text stays readable) and re-encoded as **WebP** instead of
  PNG. A real page went ~24 MB PNG → ~0.5 MB WebP (~47×). Also caps the captured
  height (`WATCHER_MAX_SCREENSHOT_HEIGHT_PX`, default 8000 CSS px).

### Fixed
- **Checks failing with `Page.goto timeout … networkidle`.** `networkidle` never
  settles on ad/tracker-heavy sites, so a strict wait timed out even though the page
  loaded fine. Navigation now falls back to `domcontentloaded` and captures what
  loaded instead of failing the whole check.
- **Change detection silently timing out (`detect() aborted`) on long pages.** The
  pure-Python pixelmatch diff at 6 MP took ~18 s (tripping the detect ceiling) and
  produced spurious diffs from scale mismatches; `WATCHER_MAX_DIFF_MEGAPIXELS` is now
  2 (~5 s, accurate — layout/image changes; text is caught by the text diff).

## 2026-06-12 — Anti-bot warm-up, history pruning & a security/efficiency hardening pass

### Added
- **Automatic anti-bot warm-up.** Some sites (Cloudflare, and Glassdoor's review
  pagination) reject a cold navigation straight to a deep URL with an empty cookie
  jar — a real browser first banks a clearance cookie from the site root. If the
  first navigation looks blocked (HTTP 401/403/429 or a known challenge page), the
  engine now visits the origin to pick up that cookie, then retries the target with
  a same-site referer. Zero cost on normal sites (only fires on a detected block),
  works on both the Playwright and Camoufox engines.
- **Delete older history.** A subtle control in the history scrubber removes every
  snapshot + change record older than the render you're viewing (which becomes the
  new oldest entry), behind an inline confirmation. Reference-safe (a surviving
  change whose baseline was pruned is detached, not orphaned) and owner-scoped.
- **Optional verbose login tracing.** `WATCHER_AI_LOGIN_DEBUG` (off by default)
  gates per-event login traces; useful only when diagnosing a stuck login.

### Security
- **Session cookies encrypted at rest.** `LoginFlow.session_state` (live auth
  cookies for logged-in sites) is now Fernet-encrypted via a transparent
  `EncryptedJSON` column type — as sensitive as the credentials already encrypted
  beside it. Existing plaintext rows auto-migrate on the next write; an
  undecryptable session (changed key/corruption) is logged, not silently dropped.
- **SSRF gate on the live login agent.** `ai-login/start` drives a real browser at
  the target and streams it back, but only checked the URL scheme — it now also
  enforces the public-IP policy every other outbound path uses (no
  `169.254.169.254` / localhost / RFC1918 unless `WATCHER_ALLOW_PRIVATE_TARGETS`).
- **Debug trace no longer logs typed text.** The manual `/input` trace redacted to
  `<N chars>` so enabling login debug can't dump a password / OTP / email.

### Performance
- **Heavy work moved off the event loop.** Snapshot/change blob hashing + writes
  (sha256 over multi-MB screenshots) and the create/update DNS validation are now
  offloaded via `asyncio.to_thread`, so a slow target can't stall request serving.
- **Group pages de-N+1'd.** The dashboard's group summaries and the group detail
  view replaced per-member queries with batched window-function queries (latest
  value, latest change, price series).
- **Camoufox skips the second (mobile) browser launch when the desktop pass was a
  block (401/403/429)** — no point re-running the anti-bot gauntlet to screenshot a
  wall.

### Fixed
- **Glassdoor "login loop" diagnosed.** Review pagination (page 2+) is
  login-gated while page 1 is public; pointing a monitor at page 1 + the warm-up
  above renders it with no login. The earlier loop was a paginated URL hitting the
  sign-in wall on every check, not a stealth failure.
- **Snappier manual login control.** One screenshot per input batch instead of one
  per click (a burst no longer backs the queue up to seconds of lag), a higher
  click-vs-drag threshold so a click on a small target stays a click, and a hard
  cap on the input backlog.
- **Dashboard no longer crashes on a price tie with a missing label.** Group
  "cheapest" now compares on the numeric value only (comparing the whole
  `(value, label)` tuple could hit `None < str` and TypeError the render).
- **Email notifications survive a newline in the subject.** A model/page-influenced
  headline with a newline previously made the message silently fail to send; the
  subject is now flattened.

### Docs
- README gained a full **Docker deployment (production)** guide (key generation,
  data/backups, TLS reverse proxy + loopback rebind, SSRF egress hardening,
  updating, resources), a **privacy / "what leaves the box"** note, and corrected
  the network-binding and Subresource-Integrity descriptions.

## 2026-06-11 — AI-assisted login (with captcha solving)

Turn "This page requires login" into an automated, watch-it-happen flow: you
provide the credentials, the model drives a real browser through the login,
pauses for a one-time code if one is needed, and the captured session is saved
so scheduled checks ride it.

### Fixed
- **Manual login now shows a screenshot (it didn't).** The initial `page.goto`
  used a strict load wait, so on a site that never finishes loading (Glassdoor's
  trackers, or its Cloudflare wall on chromium) it blocked for 45s and then
  errored — with a blank modal the whole time. Now navigation uses
  `domcontentloaded` and never hard-fails, and the screenshotter captures the
  *current* rendered state even while the page keeps loading (it halts pending
  network with `window.stop()` when Playwright would otherwise wait forever for
  fonts/'load'). A frame now appears in ~2s. Verified on chromium, firefox and
  camoufox; webkit shows it for normal pages but can't capture a never-loading
  page (a Playwright/webkit limitation — its screenshot waits for the `load`
  event that never fires).
- **Manual-control clicks land where you aim on Camoufox.** Camoufox's live
  screenshot is a 1280px CROP of a wider page, but clicks were scaled by
  `innerWidth`, pushing every click far to the right (so the reCAPTCHA checkbox
  never got hit). Clicks are now mapped to the SCREENSHOT's CSS size (what you
  actually see). The view also refreshes continuously (a frame is streamed at the
  start of every tick, ~2×/sec) with a manual ⟳ refresh button, so a click's
  result isn't "stuck" — though a stealth (Camoufox) click still takes ~1–2s to
  show because it's humanised. Caught by a rewritten screenshot-driven precision
  harness (reads the real screenshot, clicks a fraction of it, asserts the click
  landed at the matching CSS pixel) — 4/4 engines.
- **Manual remote control now works reliably on every engine — including
  Camoufox.** Clicks were landing in the wrong place (or not at all) because
  Camoufox reports `viewport_size` decoupled from the real CSS viewport. Mouse
  input is human-like
  (cursor glides to the target with real motion, a short press, and per-key
  typing delays) — but engine-aware: Camoufox humanises input *itself* and does
  so per `mouse.move`, so passing Playwright's `steps` made a single move take
  20s+; on Camoufox we now issue one (already-humanised) move. Pressing *Capture
  & finish* also drains any last queued click/keystroke first. Verified with a
  precision harness (a 5×5 grid — every click asserted to hit the exact cell, plus
  a typing check) across chromium, firefox, webkit and camoufox.

### Added
- **"Open site & do it manually" button.** Launches the live-control modal
  straight into manual mode on the monitored page — no AI, no credentials needed.
  Drive the site yourself (solve a captcha, log in), then *Capture session &
  finish*. Reuses the same session machinery and endpoints as the AI login;
  verified on chromium, firefox and camoufox (webkit uses the identical path).
- **Stored-cookie viewer & editor.** The Session cookies section now lists the
  cookies actually stored for a monitor — name, domain, and scope (session vs an
  expiry date) — with a *Reveal & edit values* toggle (values are fetched only on
  request, never embedded in the page). Delete individual cookies with ✕, edit a
  value inline, and Save (replaces the stored set; pasting still merges). New
  `GET/POST /monitors/{id}/cookies` endpoints + `cookie_rows` / `cookie_summary` /
  `apply_cookie_edits` helpers.
- **Capture summary after AI login.** When a login finishes, the modal shows a
  per-domain breakdown of exactly which cookies were captured (count + names per
  domain), and stays open so you can read it (Done closes + refreshes). The
  status endpoint returns a `captured` summary.
- **Manual remote control + AI handoff.** When the agent can't finish (captcha,
  bot wall, OTP not entered, rejected login) it no longer hard-fails — it hands
  control to you: the modal's live view becomes interactive (click and type
  relayed straight to the headless browser, plus Enter/Tab/scroll), and a
  *"Capture session & finish"* button verifies + saves whatever session you've
  reached. You can also hit *"Take over manually"* at any time and *"Let AI
  continue"* to hand it back. Endpoints: `/ai-login/{mode,input,finish}`.
- **Interactive AI login agent.** A new per-monitor *"Log in automatically with
  AI"* panel: enter the login URL + username + password and the agent opens a
  real browser and decides each step from a screenshot + the page's interactive
  elements (observe → decide → act). It fills credentials (the model never sees
  the values — they're injected), clicks through multi-step and social-login
  screens, and on success persists the session cookies to the monitor. A live
  screenshot + step log stream into the modal as it works. New
  `watcher/auth/ai_login.py`, `ai.ai_login_action`, and `/monitors/{id}/ai-login/*`
  endpoints; credentials are stored encrypted for one-click re-login.
- **Follows federated-login popups.** Logins that open the credential form in a
  new window (e.g. Glassdoor → Indeed "Continue with email", Google/Apple) are
  tracked — the agent switches to the freshest open page each step instead of
  staring at the now-frozen opener, and switches back once the popup closes.
- **One-time code (OTP/2FA) live prompt.** When the site asks for an emailed/SMS
  code, the agent pauses, the modal shows a code box, and it resumes with what
  you enter — handling the early-submit race so a code typed before it asks
  isn't lost.
- **reCAPTCHA image-challenge solving (best effort).** When a reCAPTCHA grid
  gates the login, the agent screenshots the grid, asks the vision model which
  squares match ("select all squares with buses"), clicks them and verifies,
  looping a few rounds until a token is minted — then continues the login. The
  challenge is shown live in the modal. New `ai.solve_captcha_grid` +
  `ai_login.solve_recaptcha`. Honest limits below.

### Fixed
- Credentials are **never free-typed or invented** — a field that is clearly a
  username/email or password is always filled from the stored secret, even if
  the model tries to type a literal (it once hallucinated `testuser@example.com`).
- A successful login whose OAuth popup closed on completion is now **captured**
  instead of crashing on the cleanup wait (`TargetClosedError`); once a code has
  been entered, returning to the signed-in site with no credential fields left
  is treated as success and the session is saved immediately.
- The agent recognises a genuine anti-bot wall / captcha it can't pass and stops
  with a clear, actionable message (use Session cookies) rather than a vague
  "couldn't work out the next step".
- **One-time codes now actually register.** The code is typed with real
  keystrokes (so multi-box, auto-advancing OTP inputs fill correctly — page.fill
  silently broke them) and submitted with Enter. We no longer flag a code as
  "pending" to the model after entering it (that made it hunt for a code field
  that was already gone and give up with "No input fields … to enter the code").
- **Post-code handling is robust:** a brief "verifying…" / transitional frame no
  longer aborts the login; landing back at the social-login wall is detected as a
  rejection (clear message) instead of re-clicking it.
- **Login success is judged by the popup closing**, not by what the opener shows.
  For federated logins, the credential popup closing IS the success — so a
  successful login is now captured even when the original page doesn't refresh to
  a signed-in state. A staying-open popup back at the wall is still a rejection.
- **Saving the monitor no longer wipes a captured session.** Re-saving a monitor
  whose login section was filled in cleared `session_state` whenever login steps
  existed — so a session the AI login (or a cookie paste) had just stored was
  destroyed on the next form save, leaving checks with no cookies. The session is
  now only invalidated when the login steps actually **change**.
- **The saved session is now verified before success is claimed.** After the flow
  finishes, the page is reloaded with the captured session and checked that it no
  longer shows a sign-in page. This catches a login the site *silently rejected*
  (anti-bot scoring): previously the popup would close, we'd report "logged in",
  and the very next render still showed the login page. Now that case reports an
  honest "the session was rejected — paste a cookie instead" instead of a false
  success, and a rejected session is never saved.
- **Always uses the email login, never Google/Apple SSO.** A weak model would
  sometimes click "Continue with Google" and wander into Google's sign-in; the
  agent now refuses third-party SSO buttons and steers back to the email field /
  '…or email' option (the credentials are email+password). The prompt says so too.
- **No duplicate popups.** After an action opens a federated-login popup we wait
  for it to register and navigate before the next step, so we follow it instead
  of briefly falling back to the opener and spawning a second window.
- Covered by a six-scenario browser harness (`scripts/test_ai_login.py`):
  Enter-submit, explicit-Continue, multi-box OTP, transitional-then-success,
  rejected-bounce, and a weak model that keeps grabbing SSO buttons.

### Notes / limitations
- Captcha solving is **best effort and stochastic.** reCAPTCHA Enterprise also
  scores the browser's behaviour, not just the answer, so a correct pick can
  still be re-challenged; the agent retries a few times then falls back to the
  session-cookie message. A stronger vision model raises the hit rate on the
  harder 4×4 single-image grids. Each attempt costs one vision call on the
  configured model.
- Some sites (notably ones behind aggressive bot-detection) gate the login
  behind a captcha on **every** automated attempt; for those, session-cookie
  injection (log in once in your own browser, paste the cookies) remains the
  reliable path.

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
- During a normal *render* (not the AI login agent) an image/slider captcha or
  anti-bot wall still escalates to the "needs your help" alert; the reliable fix
  is session-cookie injection or a residential proxy. (The AI login agent *can*
  now attempt reCAPTCHA image grids — see the AI-assisted login entry above —
  but slider/DataDome challenges remain unsolved.) Engine choice (Firefox vs
  Camoufox) does not
  affect IP-reputation-based DataDome blocking.
- The app runs as a long-lived uvicorn without `--reload`; code changes require
  a restart to take effect, and existing snapshots keep their previous
  screenshots — only checks after a restart render clean.
