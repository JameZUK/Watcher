"""Application configuration, loaded from environment / .env."""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, MultiFernet
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WATCHER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    data_dir: Path = Field(default=Path("data"))

    # --- Security ---
    # Used to sign session cookies. MUST be set in production.
    secret_key: str = Field(default="dev-insecure-change-me")
    # Fernet key (base64, 32 bytes) for encrypting stored credentials. If unset,
    # one is derived from secret_key (fine for dev, set explicitly in prod).
    encryption_key: str | None = Field(default=None)
    # Escape hatch: allow the app to start with a weak SECRET_KEY (dev only).
    allow_insecure: bool = Field(default=False)
    # Open self-service registration. When False, registration is allowed only
    # while no users exist yet (bootstrap the first/admin account), then closed.
    registration_open: bool = Field(default=False)
    # Serve the interactive API docs (/docs, /redoc, /openapi.json).
    enable_docs: bool = Field(default=False)
    # Extra hostnames permitted in Host/Origin (besides the request host) — e.g.
    # the public hostname when behind a reverse proxy. Comma-separated via env.
    trusted_hosts: str = Field(default="")

    # --- Server ---
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000)
    session_cookie: str = Field(default="watcher_session")
    session_max_age: int = Field(default=60 * 60 * 24 * 14)  # 14 days
    secure_cookies: bool = Field(default=False)  # set true behind HTTPS

    def secret_is_weak(self) -> bool:
        return self.secret_key in (
            "dev-insecure-change-me", "change-me-to-a-long-random-string", "",
        )

    # --- Scheduling ---
    min_interval_seconds: int = Field(default=15 * 60)  # 15 min floor
    default_interval_seconds: int = Field(default=60 * 60)  # 1 hr default
    schedule_jitter_seconds: int = Field(default=60)
    max_render_concurrency: int = Field(default=3)
    max_camoufox_concurrency: int = Field(default=2)    # tighter nested cap (Camoufox is RAM-heavy)
    render_timeout_seconds: int = Field(default=45)

    # --- Reliability ---
    render_retries: int = Field(default=1)              # extra render attempts on transient failure
    retry_backoff_seconds: float = Field(default=3.0)
    auto_pause_after_failures: int = Field(default=6)   # 0 disables auto-pause
    # After a failed automatic AI re-login, wait this long before trying again (the
    # per-user AI rate-limit is the hard budget ceiling; this just avoids hammering).
    auto_relogin_cooldown_seconds: int = Field(default=6 * 3600)
    detect_timeout_seconds: int = Field(default=20)     # ceiling on diffing (regex-DoS guard)

    # --- Abuse / resource limits (public-exposure hardening) ---
    allow_private_targets: bool = Field(default=False)   # let monitors hit private IPs
    max_monitors_per_user: int = Field(default=100)      # 0 = unlimited
    max_groups_per_user: int = Field(default=50)         # 0 = unlimited
    ai_max_calls: int = Field(default=30)                # per user per window (shared AI key)
    ai_window_seconds: int = Field(default=300)
    manual_check_cooldown_seconds: int = Field(default=20)
    max_request_bytes: int = Field(default=2_000_000)    # body-size ceiling (~2 MB)
    max_changes_per_monitor: int = Field(default=500)    # prune oldest beyond this
    # Downscale screenshots to at most this many megapixels BEFORE the (pure-Python)
    # pixelmatch diff. pixelmatch is ~slow per pixel, so on a long page even 6 MP
    # took ~18s (tripping the detect timeout) AND produced spurious diffs from minor
    # scale mismatches. 2 MP runs in ~5s and is ample for spotting layout/image
    # changes — text changes are caught exactly by the separate text diff.
    max_diff_megapixels: float = Field(default=2.0)
    login_max_attempts: int = Field(default=10)          # per IP per window
    login_window_seconds: int = Field(default=300)

    # Verbose per-event tracing of the AI / manual login agent (every click,
    # page navigation, and relayed input event). Off by default — it's noisy
    # (one line per click) and only useful when diagnosing a stuck login.
    # Enable with WATCHER_AI_LOGIN_DEBUG=true.
    ai_login_debug: bool = Field(default=False)

    # --- Detection sensitivity ---
    # Minimum fraction of pixels (0..1) that must differ before a *visual* change
    # counts — a noise floor that absorbs anti-aliasing, lazy-loaded images,
    # carousels, and sub-pixel render jitter. Acts as a lower bound on each
    # monitor's own min_change_threshold (so a monitor left at 0 still gets it).
    min_visual_change: float = Field(default=0.005)     # 0.5% of the screenshot

    # Screenshot quality / device emulation.
    screenshot_scale: int = Field(default=2)        # device pixel ratio (retina-crisp)
    mobile_viewport_width: int = Field(default=390)   # iPhone-class logical width
    mobile_viewport_height: int = Field(default=844)
    # The whole page is captured, then sliced into readable, full-width SECTIONS
    # stacked top-to-bottom (instead of one image cropped to the top, or one squished
    # to fit). Each section is this many CSS px tall (~10 screens) — small enough to
    # stay sharp at full width and within WebP's 16383px dimension limit.
    screenshot_section_height_px: int = Field(default=8000)
    # Cap on sections per capture, so a near-infinite page can't balloon storage.
    # section_height × max_sections is the deepest the capture reaches (logged if hit).
    max_screenshot_sections: int = Field(default=8)
    # A SAFETY clip on the FULL capture height (top N CSS px) before slicing — only
    # guards against a pathological infinite-scroll page spiking memory during decode.
    max_screenshot_height_px: int = Field(default=80000)
    # Each section is downscaled to fit this many megapixels and saved as WebP (far
    # smaller than PNG for document-like pages; text stays readable). 0 = native PNG.
    max_screenshot_megapixels: float = Field(default=20.0)
    screenshot_webp_quality: int = Field(default=85)   # high enough to keep text crisp

    # Auto-apply the Playwright Firefox driver workaround on startup (idempotent).
    patch_playwright: bool = Field(default=True)

    # --- Retention ---
    retention_max_snapshots: int = Field(default=50)
    retention_max_days: int = Field(default=90)

    # --- Web Push (VAPID) ---
    vapid_public_key: str | None = Field(default=None)
    vapid_private_key: str | None = Field(default=None)
    vapid_subject: str = Field(default="mailto:admin@example.com")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "watcher.db"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def blobs_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def trusted_host_set(self) -> set[str]:
        return {h.strip().lower() for h in self.trusted_hosts.split(",") if h.strip()}

    def _derived_fernet_key(self) -> bytes:
        """HKDF-derive a 32-byte Fernet key from secret_key (domain-separated)."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                    info=b"watcher-credential-encryption-v1")
        return base64.urlsafe_b64encode(hkdf.derive(self.secret_key.encode("utf-8")))

    def _legacy_fernet_key(self) -> bytes:
        """The pre-v1 zero-padded derivation — kept only so secrets encrypted by
        an older build still decrypt (MultiFernet rotation)."""
        raw = self.secret_key.encode("utf-8").ljust(32, b"0")[:32]
        return base64.urlsafe_b64encode(raw)

    def fernet(self) -> Fernet | MultiFernet:
        """Return a cipher for credential encryption.

        New data is encrypted with the primary key; decryption also accepts the
        legacy zero-padded key so existing stored secrets keep working after the
        upgrade. An explicit WATCHER_ENCRYPTION_KEY (recommended in prod) takes
        precedence and skips the secret_key derivation entirely.
        """
        if self.encryption_key:
            return Fernet(self.encryption_key.encode())
        return MultiFernet([Fernet(self._derived_fernet_key()),
                            Fernet(self._legacy_fernet_key())])

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
