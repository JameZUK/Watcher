# Changelog

All notable changes to Watcher are documented here. Dates are ISO-8601.

## 2026-06-11 — AI-assisted login (with captcha solving)

Turn "This page requires login" into an automated, watch-it-happen flow: you
provide the credentials, the model drives a real browser through the login,
pauses for a one-time code if one is needed, and the captured session is saved
so scheduled checks ride it.

### Added
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
