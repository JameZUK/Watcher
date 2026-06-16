"""Settings page and Web Push subscription management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...ai import triage_change
from ...app_settings import (
    get_app_settings,
    get_openrouter_key,
    get_pushover_token,
    get_telegram_token,
    get_user_ha_token,
    set_openrouter_key,
    set_pushover_token,
    set_smtp_password,
    set_telegram_token,
)
from ...auth.security import encrypt_secret
from ...auth.users import get_current_user, require_admin
from ...config import settings
from ...db import get_session
from ...models import PushSubscription, User
from ...netsec import validate_monitor_url, validate_notify_url, validate_public_url
from ...notify import discord, email, homeassistant, ntfy, push, pushover, telegram
from .. import templates

router = APIRouter()

_POLICIES = ("silent", "label", "drop")


@router.get("/settings")
async def settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    sub_count = len(
        (
            await session.execute(
                select(PushSubscription.id).where(PushSubscription.user_id == user.id)
            )
        ).scalars().all()
    )
    app = await get_app_settings(session)
    return templates.TemplateResponse(
        request, "settings.html",
        {
            "user": user,
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
            "push_enabled": push.enabled(),
            "vapid_public_key": settings.vapid_public_key or "",
            "sub_count": sub_count,
            "min_interval_min": settings.min_interval_seconds // 60,
            "retention_days": settings.retention_max_days,
            "retention_snapshots": settings.retention_max_snapshots,
            # AI config (the key itself is never sent to the client — only a flag).
            "is_admin": user.is_admin,
            "ai_enabled": app.ai_enabled,
            "ai_model": app.ai_model,
            "ai_key_set": bool(app.openrouter_key_enc),
            "ai_policy": app.ai_low_value_policy,
            "ai_base_url": app.ai_base_url or "",
            # The token is stored hashed; the cleartext is shown exactly once,
            # right after generation, via a one-time session value.
            "api_token_set": bool(user.api_token),
            "new_api_token": request.session.pop("new_api_token", None),
            # Per-user notification destinations + delivery prefs
            "nd": {
                "telegram_chat_id": user.telegram_chat_id or "",
                "discord_webhook": user.discord_webhook or "",
                "ntfy_topic": user.ntfy_topic or "",
                "pushover_key": user.pushover_key or "",
                "ha_url": user.ha_url or "",
                "ha_service": user.ha_service or "",
                "ha_token_set": bool(user.ha_token_enc),
                "digest_enabled": user.digest_enabled,
                "quiet_start": user.quiet_start,
                "quiet_end": user.quiet_end,
            },
            # Per-user dashboard AI-summary preferences
            "summary": {
                "enabled": user.summary_enabled,
                "days": user.summary_days or 7,
                "prompt": user.summary_prompt or "",
            },
            # Admin transport config (secrets exposed only as set/unset flags)
            "tx": {
                "smtp_host": app.smtp_host or "", "smtp_port": app.smtp_port,
                "smtp_user": app.smtp_user or "", "smtp_from": app.smtp_from or "",
                "smtp_tls": app.smtp_tls, "smtp_pass_set": bool(app.smtp_pass_enc),
                "telegram_token_set": bool(app.telegram_token_enc), "ntfy_server": app.ntfy_server,
                "pushover_token_set": bool(app.pushover_token_enc),
                "proxy_pool": app.proxy_pool or "",
            },
        },
    )


@router.post("/settings/api-token")
async def gen_api_token(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    from ...auth.security import hash_token, new_api_token
    raw = new_api_token()
    user.api_token = hash_token(raw)            # store only the hash
    await session.commit()
    request.session["new_api_token"] = raw      # shown once on the next render
    return RedirectResponse("/settings", status_code=303)


def _int_or_none(v):
    try:
        return int(v) if str(v).strip() != "" else None
    except (ValueError, TypeError):
        return None


@router.post("/settings/notifications")
async def save_notifications(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    # Collect rejected-URL errors instead of bailing out early (which silently dropped
    # the whole form — and left a half-saved HA token without its URL). Every other
    # field still saves; the rejected one keeps its old value and is reported.
    errors: list[str] = []
    user.telegram_chat_id = (form.get("telegram_chat_id") or "").strip() or None
    user.ntfy_topic = (form.get("ntfy_topic") or "").strip() or None
    user.pushover_key = (form.get("pushover_key") or "").strip() or None

    discord = (form.get("discord_webhook") or "").strip() or None
    if discord and validate_notify_url(discord):
        errors.append("discord_url")          # keep the existing value
    else:
        user.discord_webhook = discord

    # Home Assistant: SSRF-guarded like the other notifier destinations (a LAN URL needs
    # WATCHER_ALLOW_PRIVATE_TARGETS). Tolerate a scheme-less host:port. On rejection the
    # whole HA config is left untouched so a token is never saved without its URL.
    ha_url = (form.get("ha_url") or "").strip()
    if ha_url and "://" not in ha_url:
        ha_url = "http://" + ha_url
    if ha_url and validate_notify_url(ha_url):
        errors.append("ha_url")
    else:
        user.ha_url = ha_url or None
        user.ha_service = (form.get("ha_service") or "").strip() or None
        if _bool(form, "ha_token_clear"):
            user.ha_token_enc = None
        elif (form.get("ha_token") or "").strip():
            user.ha_token_enc = encrypt_secret(form["ha_token"].strip())

    user.digest_enabled = _bool(form, "digest_enabled")
    qs, qe = _int_or_none(form.get("quiet_start")), _int_or_none(form.get("quiet_end"))
    user.quiet_start = qs if qs is not None and 0 <= qs <= 23 else None
    user.quiet_end = qe if qe is not None and 0 <= qe <= 23 else None
    await session.commit()
    qstr = ("?error=" + ",".join(errors)) if errors else "?saved=1"
    return RedirectResponse("/settings" + qstr, status_code=303)


@router.post("/settings/notifications/test")
async def test_notifications(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Fire a sample alert to every notification destination this user has configured,
    and report per-channel success. Tests the SAVED config (save first)."""
    app = await get_app_settings(session)
    title = "Watcher test"
    body = "✅ Test notification from Watcher — this channel is working."
    click = "/settings"
    ext_link = (settings.public_base + click) if settings.public_base else None
    results: dict[str, bool] = {}

    if app.smtp_host and user.email:
        results["Email"] = await email.send(app, user.email, subject=title, body=f"{body}\n\n{ext_link}" if ext_link else body)
    if get_telegram_token(app) and user.telegram_chat_id:
        results["Telegram"] = await telegram.send(get_telegram_token(app), user.telegram_chat_id, title=title, body=body)
    if user.discord_webhook:
        results["Discord"] = await discord.send(user.discord_webhook, title=title, body=body, url=ext_link)
    if app.ntfy_server and user.ntfy_topic:
        results["ntfy"] = await ntfy.send(app.ntfy_server, user.ntfy_topic, title=title, body=body, url=ext_link)
    if get_pushover_token(app) and user.pushover_key:
        results["Pushover"] = await pushover.send(get_pushover_token(app), user.pushover_key, title=title, body=body, url=ext_link)
    if user.ha_url:
        results["Home Assistant"] = await homeassistant.send(user.ha_url, get_user_ha_token(user), user.ha_service, title=title, body=body, url=ext_link)
    subs = (await session.execute(
        select(PushSubscription).where(PushSubscription.user_id == user.id))).scalars().all()
    if subs:
        results["Web Push"] = await push.send_to_all(subs, title=title, body=body, url=click)

    if not results:
        return JSONResponse(
            {"ok": False, "error": "No notification destinations configured — add one and save first."},
            status_code=400)
    return JSONResponse({"ok": all(results.values()), "results": results})


@router.post("/settings/summary")
async def save_summary(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Per-user dashboard AI-summary preferences: on/off, lookback window, and a
    free-text focus/tone hint fed to the model."""
    form = await request.form()
    user.summary_enabled = _bool(form, "summary_enabled")
    days = _int_or_none(form.get("summary_days")) or 7
    user.summary_days = max(1, min(30, days))
    user.summary_prompt = (form.get("summary_prompt") or "").strip()[:500] or None
    await session.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/transports")
async def save_transports(
    request: Request,
    user: User = Depends(require_admin),   # admin-only
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    app = await get_app_settings(session)
    app.smtp_host = (form.get("smtp_host") or "").strip() or None
    app.smtp_port = _int_or_none(form.get("smtp_port")) or 587
    app.smtp_user = (form.get("smtp_user") or "").strip() or None
    app.smtp_from = (form.get("smtp_from") or "").strip() or None
    app.smtp_tls = _bool(form, "smtp_tls")
    app.proxy_pool = (form.get("proxy_pool") or "").strip() or None
    if (form.get("smtp_pass") or "").strip():
        set_smtp_password(app, form["smtp_pass"].strip())
    if (form.get("telegram_token") or "").strip():
        set_telegram_token(app, form["telegram_token"].strip())
    if (form.get("pushover_token") or "").strip():
        set_pushover_token(app, form["pushover_token"].strip())
    # ntfy host last: a rejected (private) host keeps the old value + reports it rather
    # than discarding the SMTP/token changes above.
    errors: list[str] = []
    ntfy_server = (form.get("ntfy_server") or "").strip() or "https://ntfy.sh"
    if validate_notify_url(ntfy_server):
        errors.append("ntfy_url")
    else:
        app.ntfy_server = ntfy_server
    await session.commit()
    qstr = ("?error=" + ",".join(errors)) if errors else "?saved=1"
    return RedirectResponse("/settings" + qstr, status_code=303)


def _bool(form, key: str) -> bool:
    return form.get(key) in ("on", "true", "1", "yes")


@router.post("/settings/ai")
async def save_ai_settings(
    request: Request,
    user: User = Depends(require_admin),               # admin-only
    session: AsyncSession = Depends(get_session),
):
    form = await request.form()
    app = await get_app_settings(session)
    app.ai_enabled = _bool(form, "ai_enabled")
    model = (form.get("ai_model") or "").strip()
    if model:
        app.ai_model = model
    policy = (form.get("ai_low_value_policy") or "silent").strip()
    app.ai_low_value_policy = policy if policy in _POLICIES else "silent"
    # Key: a new value replaces; "clear" wipes; blank leaves the existing key.
    if _bool(form, "openrouter_key_clear"):
        set_openrouter_key(app, None)
    else:
        new_key = (form.get("openrouter_key") or "").strip()
        if new_key:
            set_openrouter_key(app, new_key)
    # Base URL last: the shared OpenRouter key is sent as a Bearer header to this URL —
    # only accept http(s) so it can't be redirected to an exfiltration endpoint via a
    # malformed scheme (localhost is allowed: self-hosted Ollama). A bad URL keeps the
    # old value + reports it rather than discarding the model/policy/key changes above.
    errors: list[str] = []
    base_url = (form.get("ai_base_url") or "").strip() or None
    if base_url and validate_monitor_url(base_url):
        errors.append("ai_base_url")
    else:
        app.ai_base_url = base_url
    await session.commit()
    qstr = ("?error=" + ",".join(errors)) if errors else "?saved=1"
    return RedirectResponse("/settings" + qstr, status_code=303)


@router.post("/settings/ai/test")
async def test_ai_settings(
    user: User = Depends(require_admin),               # admin-only
    session: AsyncSession = Depends(get_session),
):
    """Run a tiny live triage to validate the key/model. Returns JSON."""
    app = await get_app_settings(session)
    key = get_openrouter_key(app)
    if not key:
        return JSONResponse({"ok": False, "error": "No API key set."}, status_code=400)
    result = await triage_change(
        api_key=key, model=app.ai_model, base_url=app.ai_base_url, url="https://example.com",
        title="Example", intent=None,
        diff_text="- Price: £299.00\n+ Price: £263.99", timeout=20.0,
    )
    if result is None:
        return JSONResponse({"ok": False, "error": "Call failed — check the key, model id, and credit."}, status_code=502)
    return JSONResponse({"ok": True, "headline": result.headline, "category": result.category, "importance": result.importance})


@router.get("/push/vapid-key")
async def vapid_key():
    return JSONResponse({"publicKey": settings.vapid_public_key or ""})


@router.post("/push/subscribe")
async def push_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    data = await request.json()
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys", {}) if isinstance(data.get("keys"), dict) else {}
    # Push endpoints are always https URLs; bound the length (column is 2048).
    if (not endpoint.startswith("https://") or len(endpoint) > 2048
            or not keys.get("p256dh") or not keys.get("auth")
            or len(str(keys.get("p256dh"))) > 255 or len(str(keys.get("auth"))) > 255):
        return JSONResponse({"ok": False, "error": "invalid subscription"}, status_code=400)
    # SSRF: the server POSTs to this endpoint on every change — block internal
    # targets like the other notifier destinations (push services are public).
    if not settings.allow_private_targets and validate_public_url(endpoint):
        return JSONResponse({"ok": False, "error": "endpoint not allowed"}, status_code=400)

    existing = (
        await session.execute(
            select(PushSubscription).where(
                PushSubscription.user_id == user.id,
                PushSubscription.endpoint == endpoint,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            PushSubscription(
                user_id=user.id, endpoint=endpoint,
                p256dh=keys["p256dh"], auth=keys["auth"],
            )
        )
        await session.commit()
    return JSONResponse({"ok": True})


@router.post("/push/unsubscribe")
async def push_unsubscribe(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    data = await request.json()
    endpoint = data.get("endpoint")
    if endpoint:
        await session.execute(
            delete(PushSubscription).where(
                PushSubscription.user_id == user.id,
                PushSubscription.endpoint == endpoint,
            )
        )
        await session.commit()
    return JSONResponse({"ok": True})
