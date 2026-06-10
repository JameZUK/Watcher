"""SQLAlchemy 2.0 ORM models."""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)
from sqlalchemy.types import JSON


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --- Enums -----------------------------------------------------------------


class Engine(str, enum.Enum):
    chromium = "chromium"
    firefox = "firefox"
    webkit = "webkit"
    camoufox = "camoufox"


class DetectionMode(str, enum.Enum):
    auto = "auto"          # smart: watch text + visual together, highlight both
    text = "text"          # rendered visible text
    visual = "visual"      # screenshot pixel diff
    element = "element"    # CSS/XPath selector value
    html = "html"          # raw HTML diff
    json = "json"          # parsed JSON diff


class SnapshotStatus(str, enum.Enum):
    ok = "ok"
    error = "error"


# --- Models ----------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), default=None)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Two-factor (TOTP). Secret is Fernet-encrypted at rest.
    otp_secret_enc: Mapped[str | None] = mapped_column(Text, default=None)
    otp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Personal notification destinations + delivery preferences
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), default=None)
    discord_webhook: Mapped[str | None] = mapped_column(String(512), default=None)
    ntfy_topic: Mapped[str | None] = mapped_column(String(128), default=None)
    digest_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # batch low-importance
    quiet_start: Mapped[int | None] = mapped_column(Integer, default=None)  # quiet-hours start (0-23)
    quiet_end: Mapped[int | None] = mapped_column(Integer, default=None)
    api_token: Mapped[str | None] = mapped_column(String(64), unique=True, default=None)  # REST API + RSS

    monitors: Mapped[list["Monitor"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    push_subscriptions: Mapped[list["PushSubscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Monitor(Base):
    __tablename__ = "monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048))
    engine: Mapped[Engine] = mapped_column(Enum(Engine), default=Engine.chromium)
    detection_mode: Mapped[DetectionMode] = mapped_column(
        Enum(DetectionMode), default=DetectionMode.auto
    )

    # Detection configuration
    selector: Mapped[str | None] = mapped_column(String(1024), default=None)
    selector_attr: Mapped[str | None] = mapped_column(String(128), default=None)
    ignore_selectors: Mapped[list] = mapped_column(JSON, default=list)
    ignore_patterns: Mapped[list] = mapped_column(JSON, default=list)
    min_change_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    normalize_whitespace: Mapped[bool] = mapped_column(Boolean, default=True)
    normalize_numbers: Mapped[bool] = mapped_column(Boolean, default=False)

    # Render configuration
    wait_until: Mapped[str] = mapped_column(String(32), default="networkidle")
    wait_selector: Mapped[str | None] = mapped_column(String(1024), default=None)
    wait_timeout_ms: Mapped[int] = mapped_column(Integer, default=15000)
    viewport_width: Mapped[int] = mapped_column(Integer, default=1280)
    viewport_height: Mapped[int] = mapped_column(Integer, default=800)
    actions: Mapped[list] = mapped_column(JSON, default=list)  # scroll/click/dismiss
    proxy: Mapped[str | None] = mapped_column(String(512), default=None)

    # Scheduling
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Notification channels (list of "inbox" | "push" | "webhook")
    notify_channels: Mapped[list] = mapped_column(JSON, default=lambda: ["inbox"])
    webhook_url: Mapped[str | None] = mapped_column(String(2048), default=None)

    # AI triage (honoured only when global AI triage is enabled + a key is set)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_watch_intent: Mapped[str | None] = mapped_column(Text, default=None)   # "what to watch for"
    ai_policy: Mapped[str | None] = mapped_column(String(16), default=None)   # silent|label|drop; null = inherit global

    # Value tracking (price/number trends + threshold alerts)
    track_value: Mapped[bool] = mapped_column(Boolean, default=False)
    value_threshold: Mapped[float | None] = mapped_column(Float, default=None)
    value_threshold_dir: Mapped[str | None] = mapped_column(String(8), default=None)  # below|above

    # Reliability
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    # Organization
    tags: Mapped[list] = mapped_column(JSON, default=list)
    adaptive_interval: Mapped[bool] = mapped_column(Boolean, default=False)  # auto-tune cadence
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), default=None, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped["User"] = relationship(back_populates="monitors")
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan",
        order_by="Snapshot.taken_at.desc()",
    )
    changes: Mapped[list["Change"]] = relationship(
        back_populates="monitor", cascade="all, delete-orphan",
        order_by="Change.detected_at.desc()",
    )
    login_flow: Mapped["LoginFlow | None"] = relationship(
        back_populates="monitor", cascade="all, delete-orphan", uselist=False
    )
    group: Mapped["Group | None"] = relationship(back_populates="monitors")


class Group(Base):
    """A collection of monitors watched together — e.g. the same product across
    several retailers for price comparison + a single group-level alert."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="price")  # price|stock|change|custom
    # Shared "what to watch for" — combined (non-destructively) with each
    # member's own intent at triage time. Used by stock/change/custom groups.
    watch_intent: Mapped[str | None] = mapped_column(Text, default=None)
    # Price groups: alert when the BEST value across members crosses it
    # ("below" → cheapest drops below target; "above" → highest rises above).
    target_value: Mapped[float | None] = mapped_column(Float, default=None)
    target_dir: Mapped[str | None] = mapped_column(String(8), default=None)  # below|above
    alert_active: Mapped[bool] = mapped_column(Boolean, default=False)       # dedup: armed/fired
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship()
    monitors: Mapped[list["Monitor"]] = relationship(
        back_populates="group", order_by="Monitor.name",
    )


class LoginFlow(Base):
    __tablename__ = "login_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), unique=True
    )
    # Ordered steps: [{"action": "fill", "selector": "#user", "secret": "username"}, ...]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    # Encrypted credential map {name: token}. Decrypted at replay time only.
    encrypted_secrets: Mapped[dict] = mapped_column(JSON, default=dict)
    # Persisted Playwright storage_state (cookies + localStorage), JSON.
    session_state: Mapped[dict | None] = mapped_column(JSON, default=None)
    session_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    monitor: Mapped["Monitor"] = relationship(back_populates="login_flow")


class Snapshot(Base):
    __tablename__ = "snapshots"
    # Hot query: latest snapshot(s) per monitor (optionally filtered by status).
    __table_args__ = (Index("ix_snap_monitor_taken", "monitor_id", "taken_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    status: Mapped[SnapshotStatus] = mapped_column(Enum(SnapshotStatus), default=SnapshotStatus.ok)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)

    title: Mapped[str | None] = mapped_column(String(512), default=None)
    rendered_text: Mapped[str | None] = mapped_column(Text, default=None)
    extracted_value: Mapped[str | None] = mapped_column(Text, default=None)
    numeric_value: Mapped[float | None] = mapped_column(Float, default=None, index=True)  # tracked value
    value_label: Mapped[str | None] = mapped_column(String(64), default=None)              # display, e.g. "£263.99"
    content_hash: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    dom_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    # Content-addressed blob keys (sha256) resolved via storage.blobs
    html_blob: Mapped[str | None] = mapped_column(String(64), default=None)
    screenshot_blob: Mapped[str | None] = mapped_column(String(64), default=None)         # desktop
    screenshot_mobile_blob: Mapped[str | None] = mapped_column(String(64), default=None)  # mobile

    render_ms: Mapped[int | None] = mapped_column(Integer, default=None)

    monitor: Mapped["Monitor"] = relationship(back_populates="snapshots")


class Change(Base):
    __tablename__ = "changes"
    __table_args__ = (
        Index("ix_change_monitor_detected", "monitor_id", "detected_at"),
        Index("ix_change_monitor_acked", "monitor_id", "acknowledged"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    monitor_id: Mapped[int] = mapped_column(
        ForeignKey("monitors.id", ondelete="CASCADE"), index=True
    )
    from_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), default=None
    )
    to_snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshots.id", ondelete="CASCADE"))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    change_type: Mapped[DetectionMode] = mapped_column(Enum(DetectionMode))
    summary: Mapped[str] = mapped_column(Text, default="")
    magnitude: Mapped[float] = mapped_column(Float, default=0.0)
    diff_blob: Mapped[str | None] = mapped_column(String(64), default=None)    # unified text diff
    visual_blob: Mapped[str | None] = mapped_column(String(64), default=None)  # screenshot overlay (desktop)
    visual_mobile_blob: Mapped[str | None] = mapped_column(String(64), default=None)  # screenshot overlay (mobile)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # AI triage results (null when triage is off or failed)
    ai_headline: Mapped[str | None] = mapped_column(Text, default=None)
    ai_category: Mapped[str | None] = mapped_column(String(32), default=None)   # price|stock|content|availability|cosmetic|other
    ai_importance: Mapped[str | None] = mapped_column(String(16), default=None)  # high|medium|low|noise
    notified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)    # delivered (immediate or digest)

    monitor: Mapped["Monitor"] = relationship(back_populates="changes")


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "endpoint", name="uq_push_user_endpoint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(2048))
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="push_subscriptions")


class AppSetting(Base):
    """Global, app-wide settings — a single row with id=1."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    # Access control (admin-managed)
    registration_open: Mapped[bool] = mapped_column(Boolean, default=False)  # self-service signup
    force_otp: Mapped[bool] = mapped_column(Boolean, default=False)          # require 2FA for everyone
    # AI triage (OpenRouter). Key is Fernet-encrypted at rest.
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    openrouter_key_enc: Mapped[str | None] = mapped_column(Text, default=None)
    ai_model: Mapped[str] = mapped_column(String(128), default="google/gemini-2.5-flash-lite")
    ai_low_value_policy: Mapped[str] = mapped_column(String(16), default="silent")  # silent|label|drop
    ai_base_url: Mapped[str | None] = mapped_column(String(255), default=None)  # OpenAI-compatible endpoint (Ollama, etc.)

    # Notification transports (admin-configured, shared). Secrets are encrypted.
    smtp_host: Mapped[str | None] = mapped_column(String(255), default=None)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_user: Mapped[str | None] = mapped_column(String(255), default=None)
    smtp_pass_enc: Mapped[str | None] = mapped_column(Text, default=None)
    smtp_from: Mapped[str | None] = mapped_column(String(255), default=None)
    smtp_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    telegram_token_enc: Mapped[str | None] = mapped_column(Text, default=None)
    ntfy_server: Mapped[str] = mapped_column(String(255), default="https://ntfy.sh")
