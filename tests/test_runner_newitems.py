"""New-item detection: the persisted seen-set gate (runner._new_items_gate).

Run via asyncio.run() (no pytest-asyncio), mirroring tests/test_routes.py.
"""

import asyncio
import types
import uuid

from sqlalchemy import func, select

from watcher import runner as R
from watcher.db import SessionLocal, engine, init_db
from watcher.models import Monitor, MonitorSeenItem, User


def _run(coro_factory):
    async def _wrap():
        await engine.dispose()
        await init_db()
        return await coro_factory()
    return asyncio.run(_wrap())


def _blk(k, x=0, y=0, w=300, h=80, s="a review of substantial length goes here"):
    return {"k": k, "g": "reviews>div.card", "x": x, "y": y, "w": w, "h": h, "s": s}


def _snap(keys):
    blocks = [_blk(k, y=500 + i * 100, s=f"review {k} body text that is long enough")
              for i, k in enumerate(keys)]
    return types.SimpleNamespace(element_map={"pw": 1200, "ph": 4000, "blocks": blocks})


async def _mk_monitor(s, **kw):
    u = User(email=f"u-{uuid.uuid4().hex[:8]}@t.test", password_hash="x")
    s.add(u)
    await s.flush()
    m = Monitor(user_id=u.id, name="feed", url="https://e.test", new_items_only=True, **kw)
    s.add(m)
    await s.flush()
    return m


async def _seen_count(s, mid):
    return (await s.execute(select(func.count()).select_from(MonitorSeenItem)
                            .where(MonitorSeenItem.monitor_id == mid))).scalar_one()


def test_first_capture_seeds_silently():
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s)
            out = await R._new_items_gate(s, m, _snap(["a", "b", "c"]))
            assert out == []                       # seeded, nothing announced
            assert await _seen_count(s, m.id) == 3
    _run(_t)


def test_only_new_records_are_returned():
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s)
            await R._new_items_gate(s, m, _snap(["a", "b", "c"]))         # seed
            out = await R._new_items_gate(s, m, _snap(["d", "a", "b", "c"]))  # +d, reordered
            assert len(out) == 1 and "d" in out[0]
            assert await _seen_count(s, m.id) == 4
    _run(_t)


def test_reorder_only_is_silent():
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s)
            await R._new_items_gate(s, m, _snap(["a", "b", "c"]))
            out = await R._new_items_gate(s, m, _snap(["c", "a", "b"]))
            assert out == []
            assert await _seen_count(s, m.id) == 3   # no growth
    _run(_t)


def test_no_list_region_returns_none():
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s)
            # Fewer than 3 records → not a usable list.
            assert await R._new_items_gate(s, m, _snap(["a", "b"])) is None
            # Empty / missing map → None.
            assert await R._new_items_gate(s, m, types.SimpleNamespace(element_map=None)) is None
    _run(_t)


def _patch_pick(monkeypatch, idx):
    async def _fake(*a, **k):
        return idx
    monkeypatch.setattr(R, "pick_list_region", _fake)


def test_run_pick_region_pins_rid_and_drops_heuristic_seed(monkeypatch):
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s)            # ai_list_rid is None (first pin)
            s.add(MonitorSeenItem(monitor_id=m.id, key="seeded-from-main-region"))
            await s.commit()
        _patch_pick(monkeypatch, 1)
        await R._run_pick_region(m.id, "model", None, "key", "u", "t", "intent",
                                 regions=[], candidates=[("ridA", "sa"), ("ridB", "sb")])
        async with SessionLocal() as s:
            m2 = await s.get(Monitor, m.id)
            assert m2.ai_list_rid == "ridB" and m2.ai_list_sample == "sb"
            # The heuristic-region seed is dropped so the chosen list re-seeds cleanly.
            assert await _seen_count(s, m.id) == 0
    _run(_t)


def test_run_pick_region_clears_seen_when_region_changes(monkeypatch):
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s, ai_list_rid="ridOLD")
            s.add(MonitorSeenItem(monitor_id=m.id, key="k1"))
            s.add(MonitorSeenItem(monitor_id=m.id, key="k2"))
            await s.commit()
        _patch_pick(monkeypatch, 0)
        await R._run_pick_region(m.id, "model", None, "key", "u", "t", "intent",
                                 regions=[], candidates=[("ridNEW", "s")])
        async with SessionLocal() as s:
            m2 = await s.get(Monitor, m.id)
            assert m2.ai_list_rid == "ridNEW"
            assert await _seen_count(s, m.id) == 0       # stale keys dropped
    _run(_t)


def test_run_pick_region_same_rid_keeps_seen(monkeypatch):
    async def _t():
        async with SessionLocal() as s:
            m = await _mk_monitor(s, ai_list_rid="ridA")
            s.add(MonitorSeenItem(monitor_id=m.id, key="k1"))
            await s.commit()
        _patch_pick(monkeypatch, 0)
        await R._run_pick_region(m.id, "model", None, "key", "u", "t", "intent",
                                 regions=[], candidates=[("ridA", "s")])
        async with SessionLocal() as s:
            assert await _seen_count(s, m.id) == 1       # unchanged → kept
    _run(_t)


def test_seen_set_is_bounded_by_cap(monkeypatch):
    async def _t():
        monkeypatch.setattr(R, "_SEEN_CAP", 5)
        async with SessionLocal() as s:
            m = await _mk_monitor(s)
            await R._new_items_gate(s, m, _snap(["a", "b", "c"]))         # 3
            await R._new_items_gate(s, m, _snap([f"k{i}" for i in range(6)]))  # +6 -> evict
            assert await _seen_count(s, m.id) <= 5
    _run(_t)
