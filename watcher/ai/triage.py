"""Triage a detected website change with an OpenRouter-hosted model.

Given the change (a unified text diff, or — for visual-only changes — a
screenshot) plus the monitor's URL/title and optional "what to watch for"
intent, the model returns a concise headline, a category, and an importance
rating used to write notifications and suppress noise.

All failures are swallowed and surfaced as ``None`` so triage can never break
the capture pipeline; callers fall back to the heuristic summary.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger("watcher.ai")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

CATEGORIES = ("price", "stock", "availability", "content", "layout", "cosmetic", "error", "other")
IMPORTANCES = ("high", "medium", "low", "noise")

_SCHEMA = {
    "name": "change_triage",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string", "description": "One short, factual sentence describing ONLY what actually changed. No preamble. Do not invent specific numbers/prices you cannot see in the provided diff or image."},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "importance": {"type": "string", "enum": list(IMPORTANCES)},
            "detail": {"type": "string", "description": "Optional one-line extra context."},
        },
        "required": ["headline", "category", "importance", "detail"],
    },
}

_SYSTEM = (
    "You triage changes detected on a monitored web page. You are given what "
    "changed (a text diff, or a screenshot of the page with changed regions). "
    "Respond ONLY with the requested JSON. Write a concise, human-actionable "
    "headline (no preamble). Classify the change and rate its importance:\n"
    "- high: meaningful, the user almost certainly wants to know (price/stock/"
    "availability change, key content update).\n"
    "- medium: a real content change worth seeing.\n"
    "- low: minor/peripheral content.\n"
    "- noise: cosmetic or churn the user does NOT care about (ad/banner "
    "rotation, carousels, timestamps, view counts, session tokens, reordering).\n"
    "If a 'what to watch for' instruction is given, rate importance RELATIVE to "
    "it: a change matching the instruction is high; an unrelated change is low "
    "or noise even if large.\n"
    "CRITICAL: report ONLY what the supplied diff or image actually shows changed. "
    "Never invent or guess specific prices, values, or stock states — if you are "
    "given a screenshot with no clear before/after and cannot tell what changed, "
    "say the appearance changed slightly and rate it 'noise' or 'cosmetic'. Do not "
    "manufacture a price/stock story from a page you only see once."
)


@dataclass
class Triage:
    headline: str
    category: str
    importance: str
    detail: str | None = None


def _compact_image(png: bytes, *, max_side: int = 1280, quality: int = 70) -> str | None:
    """Downscale a screenshot and return a JPEG data URL (cost control)."""
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = im.size
        scale = min(1.0, max_side / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


def _user_content(url, title, intent, diff_text, image_png):
    lines = [f"URL: {url}"]
    if title:
        lines.append(f"Page title: {title}")
    if intent:
        lines.append(f'What the user is watching for: "{intent}"')

    if diff_text and diff_text.strip():
        # Cap the diff so a huge change can't blow up cost.
        diff = diff_text.strip()
        if len(diff) > 6000:
            diff = diff[:6000] + "\n…(diff truncated)…"
        lines.append("\nUnified diff of the visible text (- old, + new):\n" + diff)
        return "\n".join(lines)

    # Visual-only change: attach a screenshot if we have one.
    data_url = _compact_image(image_png) if image_png else None
    if data_url:
        lines.append(
            "\nNo text diff is available — only a small fraction of pixels changed. "
            "The image is the CURRENT page (you do NOT have the previous version, so "
            "you cannot know exact before/after values). Describe only what is plainly "
            "different; if you cannot tell, treat it as cosmetic/noise. Do NOT state a "
            "specific price or stock change you cannot verify from a single image."
        )
        return [
            {"type": "text", "text": "\n".join(lines)},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]
    lines.append("\nThe page changed but no diff or screenshot is available.")
    return "\n".join(lines)


def _parse(content: str) -> Triage | None:
    if not content:
        return None
    txt = content.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt[txt.find("{"):]
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(txt[start:end + 1])
    except json.JSONDecodeError:
        return None
    headline = (data.get("headline") or "").strip()
    if not headline:
        return None
    category = (data.get("category") or "other").strip().lower()
    if category not in CATEGORIES:
        category = "other"
    importance = (data.get("importance") or "medium").strip().lower()
    if importance not in IMPORTANCES:
        importance = "medium"
    detail = (data.get("detail") or "").strip() or None
    return Triage(headline=headline, category=category, importance=importance, detail=detail)


async def triage_change(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    url: str,
    title: str | None,
    intent: str | None,
    diff_text: str | None,
    image_png: bytes | None = None,
    timeout: float = 30.0,
) -> Triage | None:
    """Call OpenRouter and return a Triage, or None on any failure."""
    if not api_key:
        return None
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user_content(url, title, intent, diff_text, image_png)},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": _SCHEMA},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Watcher",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter triage failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter triage error: %s", exc)
        return None
    return _parse(content)


_SUGGEST_SCHEMA = {
    "name": "watch_suggestions",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-6 short, self-contained phrases.",
            }
        },
        "required": ["suggestions"],
    },
}

_SUGGEST_SYSTEM = (
    "You help a user configure a website-change monitor. Given the page content, "
    "propose specific, concrete things worth being alerted about IF they change — "
    "prices, stock/availability, key values/metrics, dates, or new content. Each "
    "suggestion must be a short, self-contained phrase the user can drop into a "
    "'what to watch for' box, e.g. 'Price drops below the current price', 'Product "
    "comes back in stock', 'A new article is published', 'The version number "
    "changes'. Give 3-6, tailored to THIS page. Respond ONLY with the JSON."
)


async def suggest_watch_items(
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    url: str,
    title: str | None,
    page_text: str,
    timeout: float = 40.0,
) -> list[str] | None:
    """Suggest "what to watch for" items for a page. None on failure."""
    if not api_key or not (page_text or "").strip():
        return None
    text = page_text.strip()
    if len(text) > 8000:
        text = text[:8000] + "\n…(truncated)…"
    user = f"URL: {url}\n" + (f"Title: {title}\n" if title else "") + "\nPage content:\n" + text
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SUGGEST_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
        "response_format": {"type": "json_schema", "json_schema": _SUGGEST_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter suggest failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter suggest error: %s", exc)
        return None
    txt = (content or "").strip()
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(txt[start:end + 1])
    except json.JSONDecodeError:
        return None
    out, seen = [], set()
    for item in data.get("suggestions", []):
        s = (item or "").strip().lstrip("-•").strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out[:8] or None


_VALUE_SCHEMA = {
    "name": "tracked_value",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "found": {"type": "boolean"},
            "value": {"type": "number"},
            "label": {"type": "string", "description": "Tidy display, e.g. '£263.99' or '4.5'."},
        },
        "required": ["found", "value", "label"],
    },
}

_VALUE_SYSTEM = (
    "Extract the single most important tracked NUMBER from this page — usually a "
    "price, but it could be a stock count, rating, or key metric. Set found=false "
    "if there is no clear primary number. 'value' is the bare numeric value (no "
    "symbols, no thousands separators); 'label' is a tidy display like '£263.99'. "
    "Respond ONLY with the JSON."
)


async def extract_value(
    *, api_key: str, model: str, base_url: str | None = None, url: str, title: str | None,
    page_text: str, timeout: float = 30.0,
) -> tuple[float, str] | None:
    """Extract the page's primary tracked number via the model. None on failure."""
    if not api_key or not (page_text or "").strip():
        return None
    text = page_text.strip()[:6000]
    user = f"URL: {url}\n" + (f"Title: {title}\n" if title else "") + "\nPage content:\n" + text
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _VALUE_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
        "response_format": {"type": "json_schema", "json_schema": _VALUE_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter extract_value failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter extract_value error: %s", exc)
        return None
    txt = (content or "").strip()
    s, e = txt.find("{"), txt.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        data = json.loads(txt[s:e + 1])
    except json.JSONDecodeError:
        return None
    if not data.get("found"):
        return None
    try:
        val = float(data["value"])
    except (TypeError, ValueError, KeyError):
        return None
    label = (str(data.get("label") or "").strip() or str(val))[:64]
    return val, label


_CONFIG_SCHEMA = {
    "name": "monitor_config",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "detection_mode": {"type": "string", "enum": ["auto", "text", "visual", "element"]},
            "selector": {"type": "string", "description": "CSS selector for 'element' mode, else empty."},
            "interval_minutes": {"type": "integer"},
            "ai_watch_intent": {"type": "string"},
            "track_value": {"type": "boolean"},
            "value_threshold": {"type": "number", "description": "0 if none."},
            "value_threshold_dir": {"type": "string", "enum": ["below", "above", "none"]},
        },
        "required": ["name", "detection_mode", "selector", "interval_minutes",
                     "ai_watch_intent", "track_value", "value_threshold", "value_threshold_dir"],
    },
}

_CONFIG_SYSTEM = (
    "You configure a website-change monitor from the user's goal and the page. "
    "Choose: a short descriptive name; detection_mode (auto = text+appearance, "
    "text, visual, or element for one CSS value); selector = a CSS selector ONLY "
    "for 'element' mode, else empty; interval_minutes (>=15, larger for slow-"
    "moving pages); ai_watch_intent restating what to alert on in one line; "
    "track_value true if the goal involves a number (price/stock/rating); "
    "value_threshold + value_threshold_dir if the user named a target (else 0 / "
    "'none'). Respond ONLY with the JSON."
)


async def configure_monitor(
    *, api_key: str, model: str, base_url: str | None = None, url: str, title: str | None,
    page_text: str, goal: str, timeout: float = 40.0,
) -> dict | None:
    """Produce a monitor config dict from a plain-English goal. None on failure."""
    if not api_key or not (page_text or "").strip():
        return None
    text = page_text.strip()[:7000]
    user = (f"URL: {url}\n" + (f"Title: {title}\n" if title else "")
            + f'User goal: "{goal}"\n\nPage content:\n{text}')
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CONFIG_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": _CONFIG_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter configure failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter configure error: %s", exc)
        return None
    txt = (content or "").strip()
    s, e = txt.find("{"), txt.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        return json.loads(txt[s:e + 1])
    except json.JSONDecodeError:
        return None


_GROUP_SCHEMA = {
    "name": "group_config",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "description": "Short, human group name."},
            "kind": {"type": "string", "enum": ["price", "stock", "change", "custom"]},
            "watch_intent": {"type": "string", "description": "One line describing what to watch for across the group (empty for plain price)."},
            "target_value": {"type": "number", "description": "Price target to alert on, or 0 if none."},
            "target_dir": {"type": "string", "enum": ["below", "above", "none"]},
        },
        "required": ["name", "kind", "watch_intent", "target_value", "target_dir"],
    },
}

_GROUP_SYSTEM = (
    "You set up a group of web pages watched together, from the user's goal. Choose "
    "kind: 'price' (compare prices + alert on a target), 'stock' (alert when any "
    "comes back in stock), 'change' (alert on any notable change), or 'custom' (a "
    "specific thing). Give a short group name. Set watch_intent to a one-line "
    "description of what to watch for (for stock/change/custom; empty for plain "
    "price). For a price target set target_value + target_dir ('below 250' → "
    "250/below); otherwise 0 / 'none'. Respond ONLY with the JSON."
)


async def configure_group(
    *, api_key: str, model: str, base_url: str | None = None, goal: str,
    urls: list[str], timeout: float = 30.0,
) -> dict | None:
    """Derive a group name + price target from a plain-English goal. None on failure."""
    if not api_key or not (goal or "").strip():
        return None
    user = f'User goal: "{goal.strip()}"\n\nPages in the group:\n' + "\n".join(urls[:20])
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _GROUP_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 200,
        "response_format": {"type": "json_schema", "json_schema": _GROUP_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter group config failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter group config error: %s", exc)
        return None
    txt = (content or "").strip()
    s, e = txt.find("{"), txt.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        return json.loads(txt[s:e + 1])
    except json.JSONDecodeError:
        return None


_CONSENT_SCHEMA = {
    "name": "consent_selectors",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "selectors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "1-4 CSS selectors, clicked in order (most-likely first), that ACCEPT or CLOSE the banner.",
            }
        },
        "required": ["selectors"],
    },
}

_CONSENT_SYSTEM = (
    "You are given the HTML of an overlay obscuring a web page that a headless "
    "browser FAILED to dismiss automatically — a cookie/consent/privacy banner, "
    "or a promotional / sign-in / newsletter / app-install interstitial. Return "
    "1-4 CSS selectors that, clicked IN ORDER, accept or close it so the "
    "underlying page is usable. For consent: prefer an 'accept all' / 'agree' / "
    "'OK' control. For a promo/sign-in nag: prefer its close / dismiss / 'X' / "
    "'no thanks' / 'continue without' control. NEVER choose 'reject', 'decline', "
    "'manage', 'settings', or 'sign in' / 'register' (those open more dialogs or "
    "navigate away). If the HTML is an image/slider CAPTCHA or anti-bot challenge "
    "you cannot dismiss by a single click, return an EMPTY selectors array. Use "
    "the most stable selector (id, data-* attribute, aria-label, or unique class) "
    "and make it valid CSS that querySelector accepts. Respond ONLY with the JSON."
)


async def suggest_consent_selectors(
    *, api_key: str, model: str, base_url: str | None = None, url: str,
    html_snippets: list[str], timeout: float = 30.0,
) -> list[str] | None:
    """Given the HTML of an undismissable consent banner, suggest CSS selectors to
    click to accept/close it. Returns an ordered list, or None on failure."""
    if not api_key or not html_snippets:
        return None
    blob = "\n\n---\n\n".join(s for s in html_snippets if (s or "").strip())[:9000]
    if not blob.strip():
        return None
    user = f"URL: {url}\n\nBanner/modal HTML:\n{blob}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CONSENT_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 200,
        "response_format": {"type": "json_schema", "json_schema": _CONSENT_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter consent failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter consent error: %s", exc)
        return None
    txt = (content or "").strip()
    s, e = txt.find("{"), txt.rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        data = json.loads(txt[s:e + 1])
    except json.JSONDecodeError:
        return None
    out, seen = [], set()
    for item in data.get("selectors", []):
        sel = (item or "").strip()
        # keep it a sane, single CSS selector — no newlines, no script-y junk.
        if not sel or len(sel) > 200 or "\n" in sel or "<" in sel:
            continue
        if sel.lower() not in seen:
            seen.add(sel.lower())
            out.append(sel)
    return out[:4] or None


async def summarize_history(
    *, api_key: str, model: str, base_url: str | None = None, name: str, lines: list[str],
    timeout: float = 40.0,
) -> str | None:
    """One-paragraph narrative of a monitor's recent changes/values. None on failure."""
    if not api_key or not lines:
        return None
    user = (f"Monitor: {name}\nRecent changes (newest first):\n"
            + "\n".join(f"- {ln}" for ln in lines[:60]))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Summarise this website monitor's recent activity in 2-4 plain sentences a person can skim — trends, notable changes, and current state. No preamble."},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 300,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            return None
        return (resp.json()["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter summarize error: %s", exc)
        return None
