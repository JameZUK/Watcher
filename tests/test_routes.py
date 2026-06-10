"""Integration tests: web routes, REST API/RSS, notification routing, auto-pause.

Run via asyncio.run() (no pytest-asyncio dependency). The shared async engine is
disposed at the start of each test so it reconnects on the current event loop.
"""

import asyncio
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from watcher.db import SessionLocal, engine, init_db


def _run(coro_factory):
    async def _wrap():
        await engine.dispose()
        await init_db()
        return await coro_factory()
    return asyncio.run(_wrap())


def _email():
    return f"test-{uuid.uuid4().hex[:10]}@example.test"


# --- web + API smoke -------------------------------------------------------

def test_web_and_api_smoke():
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import User
        settings.registration_open = True   # allow the HTTP register in this flow
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        email = _email()
        # Browser-like same-origin header so the (fail-closed) CSRF guard admits
        # the POSTs in this flow.
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            r = await c.post("/register", data={"email": email, "password": "password123"})
            assert r.status_code == 200
            r = await c.post("/login", data={"email": email, "password": "password123"})
            assert r.status_code == 200

            # core pages render (no 500s)
            for path in ("/", "/settings", "/monitors/new", "/inbox"):
                rr = await c.get(path)
                assert rr.status_code == 200, (path, rr.status_code)

            # create a monitor with tags
            r = await c.post("/monitors", data={
                "url": "https://example.com", "engine": "chromium", "detection_mode": "text",
                "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox",
                "name": "Smoke Monitor", "tags": "alpha, beta",
            })
            assert r.status_code == 200 and "Smoke Monitor" in r.text

            # tag filter + search
            r = await c.get("/?tag=alpha")
            assert r.status_code == 200 and "Smoke Monitor" in r.text
            r = await c.get("/?q=smoke")
            assert "Smoke Monitor" in r.text

            # export
            r = await c.get("/monitors/export")
            assert r.status_code == 200 and "Smoke Monitor" in r.text

            # import a second monitor
            r = await c.post("/monitors/import", data={
                "data": '{"monitors":[{"url":"https://example.org","name":"Imported"}]}'
            })
            assert r.status_code == 200 and "Imported" in r.text

            # generate an API token — the cleartext is shown exactly once
            import re
            r = await c.post("/settings/api-token")
            m = re.search(r'break-all">([A-Za-z0-9_-]+)<', r.text)
            assert m, "one-time API token not surfaced"
            token = m.group(1)

        async with SessionLocal() as s:
            u = (await s.execute(select(User).where(User.email == email))).scalar_one()
            assert u.api_token and len(u.api_token) == 64   # stored as a SHA-256 hash

        # token-authed API + RSS (and 401 on a bad token)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/api/monitors?token={token}")
            assert r.status_code == 200 and len(r.json()["monitors"]) >= 2
            r = await c.get(f"/api/changes?token={token}")
            assert r.status_code == 200
            r = await c.get(f"/feed.xml?token={token}")
            assert r.status_code == 200 and "<rss" in r.text
            r = await c.get("/api/monitors?token=WRONG")
            assert r.status_code == 401
        return True

    assert _run(_t)


# --- notification routing (digest defers non-high) -------------------------

def test_dispatch_digest_routing():
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.models import Change, DetectionMode, Monitor, Snapshot, User
        from watcher.notify import dispatch
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x"), digest_enabled=True)
            s.add(u); await s.flush()
            m = Monitor(user_id=u.id, url="https://x.test", name="M", notify_channels=["inbox"])
            s.add(m); await s.flush()
            snap = Snapshot(monitor_id=m.id); s.add(snap); await s.flush()

            med = Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                         summary="s", ai_importance="medium")
            s.add(med); await s.flush()
            await dispatch(s, m, med)
            assert med.notified is False     # deferred to digest

            high = Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                          summary="s", ai_importance="high")
            s.add(high); await s.flush()
            await dispatch(s, m, high)
            assert high.notified is True      # high interrupts immediately
            await s.rollback()
        return True

    assert _run(_t)


# --- groups ----------------------------------------------------------------

def test_group_membership_and_page():
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Group, Monitor, User
        settings.registration_open = True
        email = _email()
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            r = await c.post("/monitors", data={
                "url": "https://example.com", "engine": "chromium", "detection_mode": "auto",
                "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox", "name": "M1"})
            assert r.status_code == 200
            r = await c.post("/groups", data={"name": "Topic group", "kind": "change",
                                              "watch_intent": "a fresh update"})
            assert r.status_code == 200 and "Topic group" in r.text

            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                gid = (await s.execute(select(Group.id).where(Group.user_id == u.id))).scalar_one()
                mid = (await s.execute(select(Monitor.id).where(Monitor.user_id == u.id))).scalars().first()

            r = await c.post(f"/groups/{gid}/add", data={"monitor_id": str(mid)})
            assert r.status_code == 200
            r = await c.get(f"/groups/{gid}")
            assert r.status_code == 200 and "example.com" in r.text and "a fresh update" in r.text
            r = await c.post(f"/groups/{gid}/remove", data={"monitor_id": str(mid)})
            assert r.status_code == 200

        async with SessionLocal() as s:
            assert (await s.get(Monitor, mid)).group_id is None
        return True

    assert _run(_t)


def test_group_price_alert_fires_once():
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.models import (Change, Group, Monitor, Snapshot, SnapshotStatus, User)
        from watcher.runner import _maybe_group_alert
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
            g = Group(user_id=u.id, name="JBL", kind="price", target_value=300.0, target_dir="below")
            s.add(g); await s.flush()
            m1 = Monitor(user_id=u.id, url="https://a.test", name="A", group_id=g.id,
                         track_value=True, notify_channels=["inbox"])
            m2 = Monitor(user_id=u.id, url="https://b.test", name="B", group_id=g.id,
                         track_value=True, notify_channels=["inbox"])
            s.add_all([m1, m2]); await s.flush()
            s.add(Snapshot(monitor_id=m1.id, status=SnapshotStatus.ok, numeric_value=320.0, value_label="£320"))
            s.add(Snapshot(monitor_id=m2.id, status=SnapshotStatus.ok, numeric_value=280.0, value_label="£280"))
            await s.flush()

            await _maybe_group_alert(s, m1)            # cheapest 280 < 300 → fire on m2
            await s.flush()
            ch = (await s.execute(select(Change).where(Change.monitor_id == m2.id))).scalars().all()
            assert len(ch) == 1 and ch[0].ai_importance == "high"
            assert (await s.get(Group, g.id)).alert_active is True

            await _maybe_group_alert(s, m1)            # already crossed → no duplicate
            ch = (await s.execute(select(Change).where(Change.monitor_id == m2.id))).scalars().all()
            assert len(ch) == 1
            await s.rollback()
        return True

    assert _run(_t)


# --- account / OTP / admin -------------------------------------------------

def test_otp_login_flow():
    async def _t():
        import pyotp
        from watcher.auth import otp
        from watcher.auth.security import encrypt_secret, hash_password
        from watcher.main import create_app
        from watcher.models import User
        secret = otp.new_secret()
        email = _email()
        async with SessionLocal() as s:
            s.add(User(email=email, password_hash=hash_password("password123"),
                       otp_secret_enc=encrypt_secret(secret), otp_enabled=True))
            await s.commit()
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}) as c:
            r = await c.post("/login", data={"email": email, "password": "password123"})
            assert r.status_code == 200 and "Two-factor" in r.text       # password ok → 2FA step
            r = await c.post("/login/otp", data={"code": "000000"})
            assert r.status_code == 401                                  # wrong code
            r = await c.post("/login/otp", data={"code": pyotp.TOTP(secret).now()}, follow_redirects=False)
            assert r.status_code == 303                                  # correct → logged in
        return True

    assert _run(_t)


def test_admin_gate_and_create_user():
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import User
        settings.registration_open = True
        admin_email, plain_email, new_email = _email(), _email(), _email()
        async with SessionLocal() as s:
            s.add(User(email=admin_email, password_hash=hash_password("password123"), is_admin=True))
            s.add(User(email=plain_email, password_hash=hash_password("password123"), is_admin=False))
            await s.commit()
        transport = httpx.ASGITransport(app=create_app())
        # non-admin is blocked
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=False) as c:
            await c.post("/login", data={"email": plain_email, "password": "password123"})
            r = await c.get("/admin/users")
            assert r.status_code in (401, 403)
        # admin can view + create
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/login", data={"email": admin_email, "password": "password123"})
            r = await c.get("/admin/users")
            assert r.status_code == 200 and admin_email in r.text
            r = await c.post("/admin/users", data={"email": new_email, "password": "password123"})
            assert r.status_code == 200
        async with SessionLocal() as s:
            assert (await s.execute(select(User).where(User.email == new_email))).scalar_one_or_none() is not None
        return True

    assert _run(_t)


# --- registration gating ---------------------------------------------------

def test_registration_closed_blocks_signup():
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import User
        # Ensure at least one user exists, then close registration.
        async with SessionLocal() as s:
            s.add(User(email=_email(), password_hash=hash_password("x")))
            await s.commit()
        settings.registration_open = False
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}) as c:
            r = await c.post("/register", data={"email": _email(), "password": "password123"})
            assert r.status_code == 403   # closed → rejected
        return True

    assert _run(_t)


# --- CSRF origin guard -----------------------------------------------------

def test_csrf_origin_guard():
    async def _t():
        from watcher.main import create_app
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # cross-origin unsafe request is blocked before reaching the handler
            r = await c.post("/changes/ack-all", headers={"Origin": "http://evil.test"})
            assert r.status_code == 403
            # same-origin passes the guard (then 401 for being unauthenticated)
            r = await c.post("/changes/ack-all", headers={"Origin": "http://t"})
            assert r.status_code != 403
            # Referer-only same-origin also passes
            r = await c.post("/changes/ack-all", headers={"Referer": "http://t/inbox"})
            assert r.status_code != 403
            # fail CLOSED: a cookie-authed unsafe request with neither Origin nor
            # Referer is rejected (a browser always sends one cross-origin)
            r = await c.post("/changes/ack-all")
            assert r.status_code == 403
        return True

    assert _run(_t)


# --- delivery: notified only when actually delivered -----------------------

def test_dispatch_notified_only_when_delivered():
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.models import Change, DetectionMode, Monitor, Snapshot, User
        from watcher.notify import dispatch
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x"))  # digest off
            s.add(u); await s.flush()
            # external-only channel with no destination configured → can't deliver
            m_ext = Monitor(user_id=u.id, url="https://x.test", name="E", notify_channels=["telegram"])
            m_inbox = Monitor(user_id=u.id, url="https://y.test", name="I", notify_channels=["inbox"])
            s.add_all([m_ext, m_inbox]); await s.flush()
            snap = Snapshot(monitor_id=m_ext.id); s.add(snap); await s.flush()

            ext = Change(monitor_id=m_ext.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                         summary="s", ai_importance="high")
            s.add(ext); await s.flush()
            await dispatch(s, m_ext, ext)
            assert ext.notified is False    # nothing delivered → retry via digest

            snap2 = Snapshot(monitor_id=m_inbox.id); s.add(snap2); await s.flush()
            ib = Change(monitor_id=m_inbox.id, to_snapshot_id=snap2.id, change_type=DetectionMode.auto,
                        summary="s", ai_importance="high")
            s.add(ib); await s.flush()
            await dispatch(s, m_inbox, ib)
            assert ib.notified is True       # inbox always counts as delivered
            await s.rollback()
        return True

    assert _run(_t)


# --- digest: inbox-only users don't accumulate an un-notified backlog ------

def test_digest_marks_inbox_only_consumed():
    async def _t():
        from sqlalchemy import delete
        from watcher.auth.security import hash_password
        from watcher.models import Change, DetectionMode, Monitor, Snapshot, User
        from watcher.notify import run_digests
        async with SessionLocal() as s:
            # digest on, but no external transport (telegram/ntfy/discord/smtp)
            u = User(email=_email(), password_hash=hash_password("x"), digest_enabled=True)
            s.add(u); await s.flush()
            m = Monitor(user_id=u.id, url="https://x.test", name="M", notify_channels=["inbox"])
            s.add(m); await s.flush()
            snap = Snapshot(monitor_id=m.id); s.add(snap); await s.flush()
            ch = Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                        summary="x", ai_importance="medium", notified=False)
            s.add(ch); await s.commit()
            cid = ch.id
        async with SessionLocal() as s:
            await run_digests(s)
        async with SessionLocal() as s:
            ch = await s.get(Change, cid)
            assert ch.notified is True   # consumed — not re-listed every hour forever
            await s.execute(delete(Change).where(Change.id == cid)); await s.commit()
        return True

    assert _run(_t)


# --- reliability: auto-pause after N failures ------------------------------

def test_auto_pause_after_failures():
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.config import settings
        from watcher.models import Monitor, Snapshot, User
        from watcher.runner import _fail
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x"))
            s.add(u); await s.flush()
            m = Monitor(user_id=u.id, url="https://x.test", name="M",
                        notify_channels=["inbox"], enabled=True, consecutive_failures=0)
            s.add(m); await s.commit()
            mid = m.id

        threshold = settings.auto_pause_after_failures
        for i in range(1, threshold + 1):
            async with SessionLocal() as s:
                mon = (await s.execute(
                    select(Monitor).where(Monitor.id == mid).options(selectinload(Monitor.login_flow))
                )).scalar_one()
                snap = Snapshot(monitor_id=mid)
                await _fail(s, mon, snap, error="boom", http_status=None)
                if i < threshold:
                    assert mon.enabled is True, i
                else:
                    assert mon.enabled is False   # auto-paused at threshold
                    assert mon.consecutive_failures == threshold
        return True

    assert _run(_t)
