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


_FLEET_SCHEMA = {
    "name": "fleet_summary",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "text": {"type": "string",
                                 "description": "A run of the paragraph (include its own spacing/punctuation)."},
                        "monitor_id": {"type": "integer",
                                       "description": "Monitor id this run links to, or 0 for plain text."},
                    },
                    "required": ["text", "monitor_id"],
                },
                "description": "The summary paragraph split into runs; a non-zero monitor_id makes that run a link.",
            }
        },
        "required": ["segments"],
    },
}

_FLEET_SYSTEM = (
    "You write a short, skimmable summary of what changed across a user's monitored "
    "websites recently. You get a list of changes — each with a monitor id, the site "
    "name, an importance, and a headline. Write a natural 2-4 sentence paragraph that "
    "leads with the most important/interesting changes, groups related ones, and gives "
    "the overall picture; mention minor/'noise' updates only briefly or in aggregate. "
    "Return it as 'segments': split the paragraph into text runs, and for a run that "
    "refers to a specific change set its monitor_id to that change's id (so it becomes "
    "a link); use 0 for ordinary connective text. Each run must carry its own spaces "
    "and punctuation so the runs read as one flowing paragraph. Never invent anything "
    "that isn't in the list. Respond ONLY with the JSON."
)


async def summarize_fleet(
    *, api_key: str, model: str, base_url: str | None = None,
    changes: list[dict], instruction: str | None = None, timeout: float = 40.0,
) -> list[dict] | None:
    """A flowing paragraph summarising changes across ALL the user's sites, returned
    as link-aware segments [{text, monitor_id}]. monitor_id is validated against the
    supplied changes so a link can't point elsewhere. None on failure.

    `instruction` is the user's own free-text preference for what the summary should
    focus on / its tone — applied as a style hint that can't override the safety
    rules (never invent, JSON-only)."""
    if not api_key or not changes:
        return None
    valid_ids = {c["monitor_id"] for c in changes}
    lines = [f"- [monitor {c['monitor_id']}] {c['monitor']} — {c.get('importance') or 'normal'} — {c['headline']}"
             for c in changes[:40]]
    messages = [
        {"role": "system", "content": _FLEET_SYSTEM},
        {"role": "user", "content": "Changes across the user's sites (newest first):\n" + "\n".join(lines)},
    ]
    if instruction and instruction.strip():
        messages.append({"role": "user", "content":
            "Tailor the summary to this preference (style and focus only — still never "
            "invent anything that isn't in the list, and still respond ONLY with the "
            "JSON): " + instruction.strip()[:500]})
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 600,
        "response_format": {"type": "json_schema", "json_schema": _FLEET_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            return None
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
        out: list[dict] = []
        for seg in (data.get("segments") or []):
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "")
            if not text:
                continue
            mid = seg.get("monitor_id")
            out.append({"text": text, "monitor_id": mid if (isinstance(mid, int) and mid in valid_ids) else 0})
        return out or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter fleet summary error: %s", exc)
        return None


_LOGIN_ACTION_SCHEMA = {
    "name": "login_action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string",
                       "enum": ["type", "type_text", "click", "await_code", "done", "fail"]},
            "index": {"type": "integer", "description": "Target element index, or -1 if N/A."},
            "secret": {"type": "string", "enum": ["username", "password", ""],
                       "description": "Which stored credential to type (for 'type')."},
            "text": {"type": "string", "description": "Literal text to type (for 'type_text'); else empty."},
            "reason": {"type": "string", "description": "One short line on why."},
        },
        "required": ["action", "index", "secret", "text", "reason"],
    },
}

_LOGIN_SYSTEM = (
    "You are logging into a website on the user's behalf, one step at a time. You "
    "are given a screenshot and a numbered list of the page's interactive elements "
    "(inputs/buttons/links). Decide the SINGLE next action to advance the login.\n"
    "Actions:\n"
    "- type: put a stored credential into element `index` — set `secret` to "
    "'username' or 'password'. You never see the value; it's filled for you. ALWAYS "
    "use this for the email/username field and the password field — the email IS the "
    "stored 'username'. NEVER invent or guess an email address.\n"
    "- type_text: type a literal `text` into element `index` — ONLY for a non-secret "
    "value that is explicitly shown on the page (rare). Never for an email, username, "
    "or password, and never make up a value.\n"
    "- click: click element `index` (a Continue/Next/Sign in/Log in button, or the "
    "'continue with email' option on social-login screens).\n"
    "- await_code: element `index` is a one-time passcode / OTP / 2FA / verification "
    "code field that needs a code only the user has (e.g. emailed/SMS code). Use "
    "this instead of guessing — the user will be asked for it.\n"
    "- done: login has clearly succeeded (the login form is gone / a signed-in page "
    "shows).\n"
    "- fail: cannot proceed (a captcha/anti-bot challenge blocks it, an error is "
    "shown, or there is no way forward). Put the reason in `reason`.\n"
    "Typical order: fill the email/username, click Continue/Next if present, fill "
    "the password, submit, handle a code if asked. Pick the email/username field "
    "before the password. The user logs in with EMAIL + PASSWORD: NEVER click "
    "'Continue with Google', 'Continue with Apple', 'Continue with Facebook' or any "
    "other third-party / SSO sign-in button — always use the email field (or an "
    "'…or email' option) instead. Also avoid 'create account' and 'forgot "
    "password'. Exactly ONE action. Set unused fields to -1 / empty string. "
    "Respond ONLY with the JSON."
)


async def ai_login_action(
    *, api_key: str, model: str, base_url: str | None = None,
    elements: list[dict], screenshot_png: bytes | None,
    available: list[str], history: list[str], code_pending: bool = False,
    timeout: float = 40.0,
) -> dict | None:
    """Decide the next login step. Returns the action dict, or None on failure."""
    if not api_key:
        return None
    lines = [
        "Goal: log into this page.",
        f"Stored credentials available to type: {', '.join(available) or 'none'}.",
        ("A one-time code has just been supplied — fill it into the code field now."
         if code_pending else ""),
        "Recent actions: " + (" → ".join(history[-6:]) or "(none yet)"),
        "Interactive elements (index: tag/type | name/id | placeholder/aria/label):",
    ]
    for e in elements[:60]:
        desc = (f"{e.get('idx')}: {e.get('tag')}/{e.get('type','')} | "
                f"{e.get('name','') or e.get('id','')} | "
                f"{e.get('placeholder','') or e.get('aria','') or e.get('label','')}"
                f"{' [autocomplete:'+e['autocomplete']+']' if e.get('autocomplete') else ''}")
        lines.append(desc[:160])
    text = "\n".join(l for l in lines if l)

    content: list | str = text
    data_url = _compact_image(screenshot_png) if screenshot_png else None
    if data_url:
        content = [{"type": "text", "text": text},
                   {"type": "image_url", "image_url": {"url": data_url}}]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": _LOGIN_SYSTEM},
                     {"role": "user", "content": content}],
        "temperature": 0.0,
        "max_tokens": 200,
        "response_format": {"type": "json_schema", "json_schema": _LOGIN_ACTION_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter login action failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        raw = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter login action error: %s", exc)
        return None
    s, e = (raw or "").find("{"), (raw or "").rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        d = json.loads(raw[s:e + 1])
    except json.JSONDecodeError:
        return None
    if d.get("action") not in ("type", "type_text", "click", "await_code", "done", "fail"):
        return None
    return d


_CAPTCHA_SCHEMA = {
    "name": "captcha_grid",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "cells": {"type": "array", "items": {"type": "integer"},
                      "description": "1-based cell numbers (left→right, top→bottom) to click."},
        },
        "required": ["cells"],
    },
}


async def solve_captcha_grid(
    *, api_key: str, model: str, base_url: str | None = None,
    target: str, rows: int, cols: int, image_png: bytes, timeout: float = 40.0,
) -> list[int] | None:
    """Look at a reCAPTCHA image grid and return which cells contain `target`.

    Best-effort: the model reads the grid (numbered left→right, top→bottom) and
    returns the matching cell numbers. reCAPTCHA also scores browser behaviour, so
    a correct answer is necessary but not always sufficient.
    """
    if not api_key or not image_png:
        return None
    data_url = _compact_image(image_png, max_side=768, quality=88)
    if not data_url:
        return None
    n = rows * cols
    system = (
        "You solve image-grid captchas. You are given an image that is a single "
        f"{rows}x{cols} grid (so {n} equal cells), numbered 1..{n} left-to-right "
        "then top-to-bottom (cell 1 is top-left). Return EVERY cell that contains "
        f"any visible part of: {target}. Even a small sliver counts. If unsure about "
        "a cell, include it. Respond ONLY with the JSON {\"cells\":[...]}.")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": f"Which cells contain {target}? Grid is {rows}x{cols}."},
                {"type": "image_url", "image_url": {"url": data_url}}]},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
        "response_format": {"type": "json_schema", "json_schema": _CAPTCHA_SCHEMA},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "Watcher"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(base_url or OPENROUTER_URL, json=body, headers=headers)
        if resp.status_code != 200:
            logger.warning("OpenRouter captcha solve failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return None
        raw = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter captcha solve error: %s", exc)
        return None
    s, e = (raw or "").find("{"), (raw or "").rfind("}")
    if s == -1 or e == -1:
        return None
    try:
        cells = json.loads(raw[s:e + 1]).get("cells") or []
    except json.JSONDecodeError:
        return None
    return [c for c in cells if isinstance(c, int) and 1 <= c <= n]
