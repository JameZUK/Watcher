"""Seed impressive demo data (product price groups + an AI-news group) onto the
demo@watcher.local account. Re-runnable: clears that account's groups/monitors
first. Run: .venv/bin/python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from watcher.db import SessionLocal, init_db
from watcher.models import (
    Change, DetectionMode, Engine, Group, Monitor, Snapshot, SnapshotStatus, User,
)
from watcher.storage import blobs

random.seed(7)
NOW = datetime.now(timezone.utc)
THUMBS: dict[str, str] = {}


_THUMB_DIR = os.path.join(os.path.dirname(__file__), "demo_thumbs")


def _thumb(path: str) -> str:
    key = THUMBS.get(path)
    if key is None:
        full = path if os.path.isabs(path) else os.path.join(_THUMB_DIR, path)
        with open(full, "rb") as f:
            key = blobs.put_bytes(f.read())
        THUMBS[path] = key
    return key


def _series(final: float, start: float, n: int = 13) -> list[float]:
    """A gentle trend from start→final with small noise, oldest→newest."""
    out = []
    for i in range(n):
        t = i / (n - 1)
        base = start + (final - start) * t
        noise = base * random.uniform(-0.006, 0.006)
        out.append(round((final if i == n - 1 else base + noise), 2))
    return out


def _money(cur: str, v: float) -> str:
    return f"{cur}{v:,.2f}"


# --- demo definitions ------------------------------------------------------

PRICE_GROUPS = [
    {
        "name": "NVIDIA RTX 5090", "cur": "£", "target_value": 1900.0, "target_dir": "below",
        "thumb": "thumb-gpu.png", "engine": Engine.chromium,
        "members": [
            ("RTX 5090 — Overclockers UK", "https://www.overclockers.co.uk/", _series(1879.00, 1949.00)),
            ("RTX 5090 FE — Scan", "https://www.scan.co.uk/", _series(1949.99, 1999.00)),
            ("RTX 5090 — Currys", "https://www.currys.co.uk/", _series(1999.00, 2059.00)),
        ],
    },
    {
        "name": "MacBook Pro 16″ M4 Max", "cur": "£", "target_value": 3500.0, "target_dir": "below",
        "thumb": "thumb-macbook.png", "engine": Engine.chromium,
        "members": [
            ("MacBook Pro 16 — John Lewis", "https://www.johnlewis.com/", _series(3499.00, 3599.00)),
            ("MacBook Pro 16 — Amazon", "https://www.amazon.co.uk/", _series(3549.00, 3629.00)),
            ("MacBook Pro 16 — Apple", "https://www.apple.com/uk/macbook-pro/", _series(3699.00, 3699.00)),
        ],
    },
    {
        "name": "KEF LS50 Meta speakers", "cur": "£", "target_value": 1000.0, "target_dir": "below",
        "thumb": "thumb-hifi.png", "engine": Engine.firefox,
        "members": [
            ("LS50 Meta — Richer Sounds", "https://www.richersounds.com/", _series(999.00, 1099.00)),
            ("LS50 Meta — Sevenoaks", "https://www.sevenoakssoundandvision.co.uk/", _series(1099.00, 1099.00)),
            ("LS50 Meta — KEF Direct", "https://uk.kef.com/products/ls50-meta", _series(1099.00, 1149.00)),
        ],
    },
    {
        "name": "AI stocks watch", "cur": "$", "target_value": 160.0, "target_dir": "above",
        "thumb": "thumb-stocks.png", "engine": Engine.chromium,
        "members": [
            ("AMD (AMD)", "https://stockanalysis.com/stocks/AMD/", _series(167.40, 150.00)),
            ("Palantir (PLTR)", "https://stockanalysis.com/stocks/PLTR/", _series(155.80, 138.00)),
            ("NVIDIA (NVDA)", "https://stockanalysis.com/stocks/NVDA/", _series(131.20, 118.00)),
        ],
    },
]

NEWS_GROUP = {
    "name": "AI & finance news", "kind": "change",
    "watch_intent": "a major AI model launch, funding round, or AI-market move",
    "thumb": "thumb-stocks.png", "engine": Engine.chromium,
    "members": [
        ("OpenAI — News", "https://openai.com/news/", "OpenAI previews GPT-5.5 with agentic tool use", "content"),
        ("Anthropic — News", "https://www.anthropic.com/news", "Anthropic raises $4B at a $60B valuation", "content"),
        ("NVIDIA Newsroom", "https://nvidianews.nvidia.com/", "NVIDIA unveils next-gen Rubin data-centre GPUs", "content"),
        ("The Verge — AI", "https://www.theverge.com/ai-artificial-intelligence", None, None),
    ],
}


async def _make_monitor(s, name, url, engine, group_id, *, track, thumb_key,
                        series=None, cur="£", headline=None, category=None):
    m = Monitor(
        user_id=1, url=url, name=name, engine=engine, detection_mode=DetectionMode.auto,
        interval_seconds=3600, enabled=True, notify_channels=["inbox"],
        track_value=track, ai_enabled=True, group_id=group_id,
        last_checked_at=NOW - timedelta(minutes=random.randint(3, 55)),
        created_at=NOW - timedelta(days=46),
    )
    s.add(m)
    await s.flush()

    latest = None
    pts = series if series is not None else [None]
    span = len(pts)
    for i, v in enumerate(pts):
        age = timedelta(days=(span - 1 - i) * 3.2, hours=random.randint(0, 12))
        snap = Snapshot(
            monitor_id=m.id, status=SnapshotStatus.ok, taken_at=NOW - age,
            title=name, content_hash=f"demo{m.id}-{i}",
            numeric_value=v, value_label=(_money(cur, v) if v is not None else None),
        )
        if i == span - 1:
            snap.screenshot_blob = thumb_key
        s.add(snap)
        await s.flush()
        latest = snap

    if headline:
        m.last_change_at = NOW - timedelta(hours=random.randint(1, 20))
        s.add(Change(
            monitor_id=m.id, to_snapshot_id=latest.id, change_type=DetectionMode.auto,
            summary=headline, ai_headline=headline, ai_category=category or "price",
            ai_importance="high", magnitude=0.02, detected_at=m.last_change_at,
            acknowledged=False, notified=True,
        ))
    return m, latest


async def main():
    await init_db()
    async with SessionLocal() as s:
        demo = (await s.execute(select(User).where(User.email == "demo@watcher.local"))).scalar_one()
        # Clean any prior demo data on this account.
        gids = (await s.execute(select(Group.id).where(Group.user_id == demo.id))).scalars().all()
        await s.execute(delete(Monitor).where(Monitor.user_id == demo.id))
        for gid in gids:
            await s.execute(delete(Group).where(Group.id == gid))
        await s.commit()

        # --- price groups ---
        for gd in PRICE_GROUPS:
            g = Group(user_id=demo.id, name=gd["name"], kind="price",
                      target_value=gd["target_value"], target_dir=gd["target_dir"])
            s.add(g)
            await s.flush()
            thumb = _thumb(gd["thumb"])
            best = None
            for idx, (name, url, series) in enumerate(gd["members"]):
                # The cheapest member (index 0) gets a "price dropped" change.
                drop = None
                if idx == 0:
                    prev, last = series[-3], series[-1]
                    verb = "rose" if gd["target_dir"] == "above" else "dropped"
                    drop = f"Price {verb} {_money(gd['cur'], prev)} → {_money(gd['cur'], last)}"
                _, latest = await _make_monitor(
                    s, name, url, gd["engine"], g.id, track=True, thumb_key=thumb,
                    series=series, cur=gd["cur"], headline=drop, category="price")
                v = series[-1]
                if best is None or (gd["target_dir"] == "below" and v < best) or \
                        (gd["target_dir"] == "above" and v > best):
                    best = v
            below = gd["target_dir"] == "below"
            g.alert_active = (below and best < gd["target_value"]) or (not below and best > gd["target_value"])
            await s.commit()

        # --- AI news (change) group ---
        ng = Group(user_id=demo.id, name=NEWS_GROUP["name"], kind="change",
                   watch_intent=NEWS_GROUP["watch_intent"])
        s.add(ng)
        await s.flush()
        thumb = _thumb(NEWS_GROUP["thumb"])
        for name, url, headline, _cat in NEWS_GROUP["members"]:
            await _make_monitor(s, name, url, NEWS_GROUP["engine"], ng.id, track=False,
                                 thumb_key=thumb, series=None, headline=headline, category="content")
        await s.commit()

        n_m = (await s.execute(select(Monitor).where(Monitor.user_id == demo.id))).scalars().all()
        n_g = (await s.execute(select(Group).where(Group.user_id == demo.id))).scalars().all()
        print(f"Seeded {len(n_g)} groups and {len(n_m)} monitors on demo@watcher.local.")


if __name__ == "__main__":
    asyncio.run(main())
