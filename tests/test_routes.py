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
        # Reset the in-memory auth rate-limiter so accumulated login/register
        # attempts from earlier tests don't trip a later test's /register.
        from watcher.web.ratelimit import _hits
        _hits.clear()
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


def test_list_groups_batched_values_and_counts():
    """list_groups (dashboard) computes the cheapest member price and the unacked
    count per group in batched queries — verify the results are still correct."""
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.models import (Change, DetectionMode, Group, Monitor, Snapshot,
                                     SnapshotStatus, User)
        from watcher.web.routes.groups import list_groups
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
            # price group: cheapest of two members (and an older, dearer snapshot ignored)
            pg = Group(user_id=u.id, name="Price", kind="price", target_dir="below")
            s.add(pg); await s.flush()
            a = Monitor(user_id=u.id, url="https://a.test", name="A", group_id=pg.id, track_value=True)
            b = Monitor(user_id=u.id, url="https://b.test", name="B", group_id=pg.id, track_value=True)
            s.add_all([a, b]); await s.flush()
            s.add_all([
                Snapshot(monitor_id=a.id, status=SnapshotStatus.ok, numeric_value=999.0),  # older
                Snapshot(monitor_id=a.id, status=SnapshotStatus.ok, numeric_value=320.0, value_label="£320"),
                Snapshot(monitor_id=b.id, status=SnapshotStatus.ok, numeric_value=280.0, value_label="£280"),
            ])
            # stock group: two unacked changes across members
            sg = Group(user_id=u.id, name="Stock", kind="stock")
            s.add(sg); await s.flush()
            c = Monitor(user_id=u.id, url="https://c.test", name="C", group_id=sg.id)
            s.add(c); await s.flush()
            snap = Snapshot(monitor_id=c.id, status=SnapshotStatus.ok); s.add(snap); await s.flush()
            s.add_all([
                Change(monitor_id=c.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto, acknowledged=False),
                Change(monitor_id=c.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto, acknowledged=False),
                Change(monitor_id=c.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto, acknowledged=True),
            ])
            await s.commit()

            rows = await list_groups(s, u)
            by_name = {r["group"].name: r for r in rows}
            assert by_name["Price"]["best_value"] == 280.0          # cheapest, newest only
            assert by_name["Price"]["best_label"] == "£280"
            assert by_name["Price"]["count"] == 2
            assert by_name["Stock"]["recent"] == 2                  # unacked only
        return True

    assert _run(_t)


def test_list_groups_price_tie_with_null_label_does_not_crash():
    """Two price-group members with an identical value where one has no label must
    not TypeError on the dashboard (compare on value, never the (value, label) tuple)."""
    async def _t():
        from watcher.auth.security import hash_password
        from watcher.models import Group, Monitor, Snapshot, SnapshotStatus, User
        from watcher.web.routes.groups import list_groups
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
            g = Group(user_id=u.id, name="Tie", kind="price", target_dir="below")
            s.add(g); await s.flush()
            a = Monitor(user_id=u.id, url="https://a.test", name="A", group_id=g.id, track_value=True)
            b = Monitor(user_id=u.id, url="https://b.test", name="B", group_id=g.id, track_value=True)
            s.add_all([a, b]); await s.flush()
            # identical value, one with a NULL label → tuple comparison would hit None < str
            s.add_all([
                Snapshot(monitor_id=a.id, status=SnapshotStatus.ok, numeric_value=300.0, value_label=None),
                Snapshot(monitor_id=b.id, status=SnapshotStatus.ok, numeric_value=300.0, value_label="£300"),
            ])
            await s.commit()
            rows = await list_groups(s, u)              # must not raise
            assert rows[0]["best_value"] == 300.0
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


def test_totp_code_is_single_use():
    """A TOTP code accepted once can't be replayed inside its validity window."""
    async def _t():
        import pyotp
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.auth.otp import new_secret
        from watcher.auth.security import encrypt_secret, hash_password
        from watcher.models import User
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        secret = new_secret()
        async with SessionLocal() as s:
            s.add(User(email=email, password_hash=hash_password("password123"),
                       otp_enabled=True, otp_secret_enc=encrypt_secret(secret)))
            await s.commit()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}) as c:
            await c.post("/login", data={"email": email, "password": "password123"})  # → OTP gate
            code = pyotp.TOTP(secret).now()
            r = await c.post("/login/otp", data={"code": code})
            assert r.status_code in (302, 303), r.status_code          # accepted
            await c.post("/logout")
            await c.post("/login", data={"email": email, "password": "password123"})
            r = await c.post("/login/otp", data={"code": code})        # same code again
            assert r.status_code == 401                                 # replay rejected
        return True

    assert _run(_t)


def test_force_otp_hard_gate():
    async def _t():
        from watcher.app_settings import get_app_settings
        from watcher.auth.security import hash_password, new_session_token
        from watcher.main import create_app
        from watcher.models import User
        email = _email()
        async with SessionLocal() as s:
            s.add(User(email=email, password_hash=hash_password("password123"),
                       session_token=new_session_token()))     # no OTP enrolled
            app = await get_app_settings(s)
            app.force_otp = True
            await s.commit()
        try:
            transport = httpx.ASGITransport(app=create_app())
            async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                         headers={"Origin": "http://t"}) as c:
                await c.post("/login", data={"email": email, "password": "password123"})
                # force_otp on + no OTP → every non-account page redirects to /account
                r = await c.get("/", follow_redirects=False)
                assert r.status_code == 303 and "/account" in r.headers.get("location", "")
                # but the account page itself stays reachable (to enrol)
                r = await c.get("/account", follow_redirects=False)
                assert r.status_code == 200
        finally:
            async with SessionLocal() as s:
                app = await get_app_settings(s)
                app.force_otp = False
                await s.commit()
        return True

    assert _run(_t)


def test_session_invalidated_on_password_change():
    async def _t():
        from watcher.app_settings import get_app_settings
        from watcher.auth.security import hash_password, new_session_token
        from watcher.main import create_app
        from watcher.models import User
        email = _email()
        async with SessionLocal() as s:
            s.add(User(email=email, password_hash=hash_password("password123"),
                       session_token=new_session_token()))
            app = await get_app_settings(s)
            app.force_otp = False
            await s.commit()
        app_obj = create_app()
        transport = httpx.ASGITransport(app=app_obj)
        # Two independent logged-in sessions for the same user.
        async with httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as a, \
                   httpx.AsyncClient(transport=transport, base_url="http://t", headers={"Origin": "http://t"}) as b:
            await a.post("/login", data={"email": email, "password": "password123"})
            await b.post("/login", data={"email": email, "password": "password123"})
            assert (await a.get("/account", follow_redirects=False)).status_code == 200
            assert (await b.get("/account", follow_redirects=False)).status_code == 200
            # A changes the password → other sessions die; A survives.
            r = await a.post("/account/password",
                             data={"current_password": "password123", "new_password": "newpassword123"})
            assert r.status_code in (200, 303)
            assert (await a.get("/account", follow_redirects=False)).status_code == 200
            rb = await b.get("/account", follow_redirects=False)
            assert rb.status_code in (303, 401)        # B's session invalidated
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
        # port hardening: when the Host carries an explicit port, a same-host but
        # DIFFERENT-port Origin (e.g. another app on localhost:9000) is blocked
        async with httpx.AsyncClient(transport=transport, base_url="http://t:8000") as cp:
            r = await cp.post("/changes/ack-all", headers={"Origin": "http://t:9000"})
            assert r.status_code == 403            # wrong port → blocked
            r = await cp.post("/changes/ack-all", headers={"Origin": "http://t:8000"})
            assert r.status_code != 403            # matching port → allowed
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

def test_delete_history_before_prunes_older():
    """The 'delete older history' button removes every snapshot + change strictly
    older than the in-view (pivot) snapshot, keeps the pivot and newer, and detaches
    (not deletes) a surviving change that referenced a pruned baseline snapshot."""
    async def _t():
        from datetime import datetime, timedelta, timezone
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Change, DetectionMode, Monitor, Snapshot, User

        from watcher.auth.security import hash_password
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})  # auto-logs in

            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://x.test", name="H", notify_channels=["inbox"])
                s.add(m); await s.flush()
                snaps = []
                for k in range(5):  # hours 0..4
                    sn = Snapshot(monitor_id=m.id, taken_at=base + timedelta(hours=k),
                                  screenshot_blob="b" * 64)
                    s.add(sn); snaps.append(sn)
                await s.flush()
                # old change (gets pruned) + a surviving change whose baseline is pruned
                s.add(Change(monitor_id=m.id, from_snapshot_id=snaps[0].id, to_snapshot_id=snaps[1].id,
                             change_type=DetectionMode.auto, detected_at=base + timedelta(hours=1)))
                keep = Change(monitor_id=m.id, from_snapshot_id=snaps[2].id, to_snapshot_id=snaps[3].id,
                              change_type=DetectionMode.auto, detected_at=base + timedelta(hours=3))
                s.add(keep); await s.commit()
                mid, pivot_id = m.id, snaps[3].id
                keep_id, keep_pivot, keep_newer = keep.id, snaps[3].id, snaps[4].id

                # a second user's monitor — used for the ownership check below
                u2 = User(email=_email(), password_hash=hash_password("x"))
                s.add(u2); await s.flush()
                m2 = Monitor(user_id=u2.id, url="https://y.test", name="O", notify_channels=["inbox"])
                s.add(m2); await s.commit()
                other_mid = m2.id

            # follow_redirects swallows the 303 → assert the redirect landed on the monitor
            r = await c.post(f"/monitors/{mid}/history/delete-before",
                             data={"before_snapshot_id": pivot_id})
            assert r.status_code == 200 and str(mid) in str(r.url), r.url

            async with SessionLocal() as s:
                ids = set((await s.execute(
                    select(Snapshot.id).where(Snapshot.monitor_id == mid))).scalars().all())
                assert ids == {keep_pivot, keep_newer}, ids       # 0,1,2 pruned; 3,4 kept
                changes = (await s.execute(
                    select(Change).where(Change.monitor_id == mid))).scalars().all()
                assert len(changes) == 1 and changes[0].id == keep_id   # old change gone
                assert changes[0].from_snapshot_id is None              # baseline detached, not orphaned

            # ownership: the logged-in user can't prune a monitor they don't own
            r2 = await c.post(f"/monitors/{other_mid}/history/delete-before",
                              data={"before_snapshot_id": pivot_id})
            assert r2.status_code == 404, r2.status_code
        return True

    assert _run(_t)


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


# --- consent / annoyance-blocking form wiring ------------------------------

def test_dashboard_fleet_summary():
    """The dashboard shows a fleet-wide summary headline across all sites: the recent
    change count, the importance breakdown, and the top change headlines."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import (Change, DetectionMode, Monitor, Snapshot,
                                     SnapshotStatus, User)
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://a.test", name="Alpha")
                s.add(m); await s.flush()
                snap = Snapshot(monitor_id=m.id, status=SnapshotStatus.ok)
                s.add(snap); await s.flush()
                s.add(Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                             ai_headline="Price dropped to £199", ai_importance="high"))
                # An ALREADY-REVIEWED change must NOT be summarised.
                ack_snap = Snapshot(monitor_id=m.id, status=SnapshotStatus.ok); s.add(ack_snap); await s.flush()
                s.add(Change(monitor_id=m.id, to_snapshot_id=ack_snap.id, change_type=DetectionMode.auto,
                             ai_headline="Old reviewed change", ai_importance="high", acknowledged=True))
                await s.commit()
            r = (await c.get("/")).text
            assert "1 new change across 1 site" in r   # only the unreviewed one counts
            assert "Price dropped to £199" in r        # top (unreviewed) headline
            assert "1 high" in r                        # importance breakdown (unreviewed)
            # the acknowledged change is NOT summarised but DOES show in the reviewed line
            assert "1 reviewed" in r and "Old reviewed change" in r
            assert "· unreviewed ·" not in r           # no contradictory caught-up tag
        return True

    assert _run(_t)


def test_dashboard_summary_caught_up_state():
    """With everything reviewed: a clean 'all caught up' banner (no contradictory
    'unreviewed' tag, no redundant site count) plus a recently-reviewed context line."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import (Change, DetectionMode, Monitor, Snapshot,
                                     SnapshotStatus, User)
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://a.test", name="Alpha"); s.add(m); await s.flush()
                snap = Snapshot(monitor_id=m.id, status=SnapshotStatus.ok); s.add(snap); await s.flush()
                s.add(Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                             ai_headline="A change I already read", ai_importance="high", acknowledged=True))
                await s.commit()
            r = (await c.get("/")).text
            assert "You're all caught up — no new changes" in r
            assert "· unreviewed ·" not in r            # the reported contradiction is gone
            assert "1 reviewed" in r and "A change I already read" in r   # context line
        return True

    assert _run(_t)


def test_dashboard_summary_fragment_endpoint():
    """The polled /dashboard/summary endpoint returns JSON {ready, segments} so the
    page can swap the AI paragraph in without a reload (AI off in tests → not ready)."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            r = await c.get("/dashboard/summary")
            assert r.status_code == 200 and r.json()["ready"] is False
            assert r.json()["segments"] == []
        return True

    assert _run(_t)


def test_dashboard_fleet_summary_paragraph():
    """When a cached AI summary exists, the dashboard renders it as a paragraph with
    links embedded to the relevant monitors (and the text is escaped)."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import (Change, DetectionMode, Monitor, Snapshot,
                                     SnapshotStatus, User)
        from watcher.web.routes import dashboard as D
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://a.test", name="Alpha"); s.add(m); await s.flush()
                snap = Snapshot(monitor_id=m.id, status=SnapshotStatus.ok); s.add(snap); await s.flush()
                s.add(Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                             ai_headline="Price moved", ai_importance="high"))
                await s.commit()
                mid, uid = m.id, u.id
                # prime the cache with the CURRENT fingerprint so the route hits it
                summ = await D._fleet_summary(s, u, [m])
            D._fleet_cache[uid] = {"fp": summ["fingerprint"], "segments": [
                {"text": "Across your sites, ", "monitor_id": 0},
                {"text": "Alpha dropped sharply", "monitor_id": mid},
                {"text": " <careful>.", "monitor_id": 0}]}
            try:
                r = (await c.get("/")).text
            finally:
                D._fleet_cache.pop(uid, None)
            assert "Across your sites," in r
            assert f'href="/monitors/{mid}"' in r and "Alpha dropped sharply" in r
            assert "&lt;careful&gt;" in r and "<careful>" not in r     # escaped, no injection
            # collapsed-TLDR markup: a 2-line clamp + a "Show more" toggle
            assert "fleetSummary(false)" in r
            assert "line-clamp:2" in r and "Show more" in r
        return True

    assert _run(_t)


def test_settings_summary_customization():
    """Per-user dashboard-summary prefs round-trip through /settings/summary, and
    turning the summary off suppresses the AI paragraph even when one is cached."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import (Change, DetectionMode, Monitor, Snapshot,
                                     SnapshotStatus, User)
        from watcher.web.routes import dashboard as D
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            # Save custom prefs (no summary_enabled key => switched off); days clamps.
            await c.post("/settings/summary", data={
                "summary_days": "99", "summary_prompt": "Lead with price drops."})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                assert u.summary_enabled is False
                assert u.summary_days == 30                      # clamped to max
                assert u.summary_prompt == "Lead with price drops."
            assert "Lead with price drops." in (await c.get("/settings")).text
            # Off-switch: even a cached paragraph is suppressed (no AI, fall back to list).
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://a.test", name="Alpha"); s.add(m); await s.flush()
                snap = Snapshot(monitor_id=m.id, status=SnapshotStatus.ok); s.add(snap); await s.flush()
                s.add(Change(monitor_id=m.id, to_snapshot_id=snap.id, change_type=DetectionMode.auto,
                             ai_headline="Price moved", ai_importance="high"))
                await s.commit()
                summ = await D._fleet_summary(s, u, [m])
                D._fleet_cache[u.id] = {"fp": summ["fingerprint"],
                                        "segments": [{"text": "X", "monitor_id": 0}]}
                try:
                    assert await D._fleet_paragraph(s, u, summ) is None   # disabled → suppressed
                finally:
                    D._fleet_cache.pop(u.id, None)
        return True

    assert _run(_t)


def test_monitor_detail_changes_headline():
    """The detail page shows a top-of-page changes headline: the latest change's
    stored summary, or 'No changes' / 'No history' states."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import (Change, DetectionMode, Monitor, Snapshot,
                                     SnapshotStatus, User)
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                no_hist = Monitor(user_id=u.id, url="https://a.test", name="NoHist")
                stable = Monitor(user_id=u.id, url="https://b.test", name="Stable")
                changed = Monitor(user_id=u.id, url="https://d.test", name="Changed")
                s.add_all([no_hist, stable, changed]); await s.flush()
                s.add(Snapshot(monitor_id=stable.id, status=SnapshotStatus.ok))
                snap = Snapshot(monitor_id=changed.id, status=SnapshotStatus.ok)
                s.add(snap); await s.flush()
                s.add(Change(monitor_id=changed.id, to_snapshot_id=snap.id,
                             change_type=DetectionMode.auto,
                             ai_headline="Price dropped to £280", ai_importance="high"))
                await s.commit()
                ids = (no_hist.id, stable.id, changed.id)

            assert "No history yet" in (await c.get(f"/monitors/{ids[0]}")).text
            assert "No changes detected" in (await c.get(f"/monitors/{ids[1]}")).text
            r = (await c.get(f"/monitors/{ids[2]}")).text
            assert "Price dropped to £280" in r and ">high<" in r
        return True

    assert _run(_t)


def test_monitor_detail_renders_with_legacy_null_sections():
    """A snapshot from before the section columns existed has NULL (None) section
    lists, not []. The detail page's history builder must tolerate that — a bare
    `|length` on None 500'd the page ('object of type NoneType has no len()')."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor, Snapshot, SnapshotStatus, User
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://a.test", name="Legacy"); s.add(m); await s.flush()
                snap = Snapshot(monitor_id=m.id, status=SnapshotStatus.ok,
                                screenshot_blob="deadbeefcafe")    # a stored capture exists
                # Simulate a pre-migration row: section lists are NULL, not [].
                snap.screenshot_sections = None
                snap.screenshot_mobile_sections = None
                s.add(snap); await s.commit()
                mid = m.id
            r = await c.get(f"/monitors/{mid}")
            assert r.status_code == 200       # was a 500 before the guard
        return True

    assert _run(_t)


def test_refine_intent_endpoint_wired():
    """The refine-intent endpoint is reachable and returns JSON; with no draft it asks
    for one, and (AI off in tests) otherwise reports AI isn't configured."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            r1 = await c.post("/monitors/refine-intent", data={"intent": "", "url": "https://a.test"})
            assert r1.json()["ok"] is False and "rough description" in r1.json()["error"].lower()
            r2 = await c.post("/monitors/refine-intent",
                              data={"intent": "watch the price", "url": "https://a.test"})
            assert r2.headers["content-type"].startswith("application/json")
            assert r2.json()["ok"] is False        # AI not configured in tests
        return True

    assert _run(_t)


def test_analyze_page_endpoint_wired():
    """The page-profiler endpoint is reachable on an owned monitor and returns JSON
    (AI isn't configured in tests, so it reports that rather than crashing)."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor, User
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://a.test", name="P"); s.add(m); await s.commit()
                mid = m.id
            r = await c.post(f"/monitors/{mid}/analyze-page")
            assert r.headers["content-type"].startswith("application/json")
            assert r.json()["ok"] is False        # no AI key configured in tests
        return True

    assert _run(_t)


def test_status_page():
    """The /status page renders per-monitor success rate + summary for the user."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor, Snapshot, SnapshotStatus, User
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://x.test", name="S"); s.add(m); await s.flush()
                s.add_all([
                    Snapshot(monitor_id=m.id, status=SnapshotStatus.ok, render_ms=100),
                    Snapshot(monitor_id=m.id, status=SnapshotStatus.ok, render_ms=300),
                    Snapshot(monitor_id=m.id, status=SnapshotStatus.error, render_ms=None),
                ])
                await s.commit()
            r = await c.get("/status")
            assert r.status_code == 200
            assert "Per-monitor health" in r.text
            assert "67%" in r.text                 # 2 ok / 3 checks
        return True

    assert _run(_t)


def test_retention_prune_keeps_n_and_referenced():
    """SQL prune keeps the newest-N snapshots + any referenced by a change, and
    deletes the rest (here: the unreferenced middle ones)."""
    async def _t():
        from datetime import datetime, timedelta, timezone
        from watcher.config import settings
        from watcher.auth.security import hash_password
        from watcher.models import Change, DetectionMode, Monitor, Snapshot, User
        from watcher.storage import retention
        keep0, days0 = settings.retention_max_snapshots, settings.retention_max_days
        settings.retention_max_snapshots = 3
        settings.retention_max_days = 365000          # no age-based deletion in this test
        try:
            base = datetime(2020, 1, 1, tzinfo=timezone.utc)
            async with SessionLocal() as s:
                u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
                m = Monitor(user_id=u.id, url="https://x.test", name="R"); s.add(m); await s.flush()
                snaps = [Snapshot(monitor_id=m.id, taken_at=base + timedelta(hours=k)) for k in range(6)]
                s.add_all(snaps); await s.flush()
                s.add(Change(monitor_id=m.id, to_snapshot_id=snaps[0].id,   # pin the OLDEST
                             change_type=DetectionMode.auto, detected_at=base))
                await s.commit()
                mid = m.id
                newest3 = {snaps[5].id, snaps[4].id, snaps[3].id}
                pinned = snaps[0].id
            await retention.prune()
            async with SessionLocal() as s:
                left = set((await s.execute(
                    select(Snapshot.id).where(Snapshot.monitor_id == mid))).scalars().all())
            assert left == newest3 | {pinned}, left   # middle two unreferenced ones pruned
        finally:
            settings.retention_max_snapshots, settings.retention_max_days = keep0, days0
        return True

    assert _run(_t)


def test_auto_relogin_toggle_persists():
    """The per-monitor auto-relogin opt-in round-trips through create (checked) and
    edit (unchecked => off) into the Monitor row."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            base = {"url": "https://example.com", "engine": "chromium", "detection_mode": "text",
                    "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox",
                    "name": "Relogin Monitor"}
            await c.post("/monitors", data={**base, "auto_relogin_enabled": "on"})
            async with SessionLocal() as s:
                m = (await s.execute(
                    select(Monitor).where(Monitor.name == "Relogin Monitor"))).scalar_one()
                assert m.auto_relogin_enabled is True            # checked => on
                mid = m.id
            await c.post(f"/monitors/{mid}", data=base)          # edit, field omitted
            async with SessionLocal() as s:
                m = (await s.execute(select(Monitor).where(Monitor.id == mid))).scalar_one()
                assert m.auto_relogin_enabled is False           # unchecked => off
        return True

    assert _run(_t)


def test_intent_change_clears_page_profile():
    """Editing 'what to watch for' clears the page understanding (it's derived from the
    intent, so it goes stale); an edit that leaves the intent unchanged keeps it."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor
        settings.registration_open = True
        transport = httpx.ASGITransport(app=create_app())
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            base = {"url": "https://example.com", "engine": "chromium", "detection_mode": "text",
                    "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox",
                    "name": "Profiled"}
            await c.post("/monitors", data={**base, "ai_watch_intent": "only reviews"})
            async with SessionLocal() as s:
                m = (await s.execute(select(Monitor).where(Monitor.name == "Profiled"))).scalar_one()
                m.ai_page_profile = "Understanding: reviews matter, jobs are churn"
                await s.commit(); mid = m.id
            # Same intent → profile preserved.
            await c.post(f"/monitors/{mid}", data={**base, "ai_watch_intent": "only reviews"})
            async with SessionLocal() as s:
                assert (await s.get(Monitor, mid)).ai_page_profile is not None
            # Changed intent → profile cleared (will regenerate).
            await c.post(f"/monitors/{mid}", data={**base, "ai_watch_intent": "only the price"})
            async with SessionLocal() as s:
                assert (await s.get(Monitor, mid)).ai_page_profile is None
        return True

    assert _run(_t)


def test_auto_relogin_recovery_flow():
    """_run_relogin refreshes the session + re-checks on a successful agent run, and
    on a fail-fast (captcha/OTP) sets a cooldown and does NOT re-check. The agent and
    scheduler are stubbed, so this exercises the orchestration with no browser/AI."""
    async def _t():
        from datetime import datetime, timezone
        import watcher.runner as R
        from watcher import scheduler
        from watcher.auth import ai_login
        from watcher.auth.login_flows import build_secret_map, session_is_valid
        from watcher.auth.security import hash_password
        from watcher.models import LoginFlow, Monitor, User

        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
            m = Monitor(user_id=u.id, url="https://site.test", name="L",
                        auto_relogin_enabled=True, notify_channels=["inbox"])
            s.add(m); await s.flush()
            s.add(LoginFlow(
                monitor_id=m.id,
                encrypted_secrets=build_secret_map({"username": "u@e.test", "password": "pw"}),
                session_state={"cookies": [{"name": "old", "value": "1"}]},
                session_valid_until=datetime(2000, 1, 1, tzinfo=timezone.utc)))  # expired
            await s.commit()
            mid, uid = m.id, u.id

        orig_run, orig_trig = ai_login.run_agent, scheduler.trigger_now
        triggered: list = []
        scheduler.trigger_now = lambda i: triggered.append(i)
        R._relogin_inflight.clear()

        # success: agent persists a fresh session and reports done
        async def ok_agent(sess, action_fn, persist_fn, **kw):
            await persist_fn({"cookies": [{"name": "new", "value": "2"}]})
            sess.status = "done"
        ai_login.run_agent = ok_agent
        try:
            await R._run_relogin(mid, uid, "model", None, "sk-test")
        finally:
            ai_login.run_agent = orig_run
        async with SessionLocal() as s:
            fl = (await s.execute(select(LoginFlow).where(LoginFlow.monitor_id == mid))).scalar_one()
            assert session_is_valid(fl)                          # refreshed → future TTL
            assert fl.session_state["cookies"][0]["name"] == "new"
            assert fl.relogin_cooldown_until is None             # cleared on success
        assert triggered == [mid]                                # re-checked

        # fail-fast: agent errors (needs a human) → cooldown set, no re-check
        triggered.clear()
        async def fail_agent(sess, action_fn, persist_fn, **kw):
            sess.status, sess.error = "error", "needs a human"
        ai_login.run_agent = fail_agent
        try:
            await R._run_relogin(mid, uid, "model", None, "sk-test")
        finally:
            ai_login.run_agent = orig_run; scheduler.trigger_now = orig_trig
        async with SessionLocal() as s:
            fl = (await s.execute(select(LoginFlow).where(LoginFlow.monitor_id == mid))).scalar_one()
            assert fl.relogin_cooldown_until is not None          # cooldown persisted
        assert triggered == []
        return True

    assert _run(_t)


def test_check_monitor_escalates_to_camoufox_on_block():
    """A non-stealth render blocked by anti-bot retries on Camoufox and uses the good
    render — but only switches the monitor's engine permanently after a streak of
    blocks (a one-off block must NOT migrate it to the heavier stealth engine)."""
    async def _t():
        import watcher.runner as R
        from watcher.auth.security import hash_password
        from watcher.engines import RenderResult
        from watcher.models import Engine, Monitor, Snapshot, SnapshotStatus, User
        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
            m = Monitor(user_id=u.id, url="https://cf.test", name="CF", engine=Engine.chromium,
                        enabled=True, notify_channels=["inbox"])
            s.add(m); await s.commit(); mid = m.id

        orig = R._render

        async def render(_m, engine=None):
            if engine == Engine.camoufox:      # stealth clears the wall
                return RenderResult(ok=True, http_status=200,
                                    rendered_text="real content here", html="<p>hi</p>")
            return RenderResult(ok=False, http_status=403,
                                error="403 Cloudflare anti-bot protection")
        R._render = render
        R._escalate_cooldown.clear(); R._escalate_streak.clear()
        try:
            # first block: escalation used (snapshot ok) but engine NOT yet switched
            await R.check_monitor(mid)
            async with SessionLocal() as s:
                m = (await s.execute(select(Monitor).where(Monitor.id == mid))).scalar_one()
                snap = (await s.execute(select(Snapshot).where(Snapshot.monitor_id == mid)
                        .order_by(Snapshot.id.desc()))).scalars().first()
            assert snap.status == SnapshotStatus.ok and m.engine == Engine.chromium

            # after the streak threshold, it switches permanently
            for _ in range(R._ESCALATE_PERSIST_AFTER - 1):
                await R.check_monitor(mid)
            async with SessionLocal() as s:
                m = (await s.execute(select(Monitor).where(Monitor.id == mid))).scalar_one()
            assert m.engine == Engine.camoufox
        finally:
            R._render = orig
            R._escalate_streak.clear()
        return True

    assert _run(_t)


def test_check_monitor_triggers_relogin_on_failed_expired_session():
    """End-to-end hook: a failed render + expired session on an opted-in monitor
    fires the recovery (no failure counted, soft snapshot, re-check on success)."""
    async def _t():
        from datetime import datetime, timezone
        import watcher.runner as R
        from watcher import scheduler
        from watcher.app_settings import get_app_settings
        from watcher.auth import ai_login
        from watcher.auth.login_flows import build_secret_map
        from watcher.auth.security import encrypt_secret, hash_password
        from watcher.engines import RenderResult
        from watcher.models import LoginFlow, Monitor, Snapshot, SnapshotStatus, User

        async with SessionLocal() as s:
            u = User(email=_email(), password_hash=hash_password("x")); s.add(u); await s.flush()
            m = Monitor(user_id=u.id, url="https://gated.test", name="G", enabled=True,
                        auto_relogin_enabled=True, notify_channels=["inbox"], consecutive_failures=0)
            s.add(m); await s.flush()
            s.add(LoginFlow(monitor_id=m.id,
                            encrypted_secrets=build_secret_map({"username": "u", "password": "p"}),
                            session_state={"cookies": [{"name": "old", "value": "1"}]},
                            session_valid_until=datetime(2000, 1, 1, tzinfo=timezone.utc)))
            app = await get_app_settings(s)
            app.ai_enabled = True
            app.openrouter_key_enc = encrypt_secret("sk-test")
            await s.commit(); mid = m.id

        orig_render, orig_run, orig_trig = R._render, ai_login.run_agent, scheduler.trigger_now
        triggered: list = []

        async def fail_render(_m, engine=None):
            return RenderResult(ok=False, error="login wall", http_status=403)

        async def ok_agent(sess, action_fn, persist_fn, **kw):
            await persist_fn({"cookies": [{"name": "new", "value": "2"}]})
            sess.status = "done"

        R._render = fail_render
        ai_login.run_agent = ok_agent
        scheduler.trigger_now = lambda i: triggered.append(i)
        R._relogin_inflight.clear()
        try:
            await R.check_monitor(mid)
            for t in list(R._relogin_tasks):          # await the spawned recovery task
                try:
                    await asyncio.wait_for(t, 10)
                except Exception:
                    pass
        finally:
            R._render, ai_login.run_agent, scheduler.trigger_now = orig_render, orig_run, orig_trig

        async with SessionLocal() as s:
            m = (await s.execute(select(Monitor).where(Monitor.id == mid))).scalar_one()
            snap = (await s.execute(select(Snapshot).where(Snapshot.monitor_id == mid)
                    .order_by(Snapshot.id.desc()))).scalars().first()
        assert m.consecutive_failures == 0            # recovery in progress is NOT a failure
        assert snap.status == SnapshotStatus.error and "re-login" in (snap.error or "").lower()
        assert triggered == [mid]                     # agent succeeded → re-checked
        return True

    assert _run(_t)


def test_consent_clicks_and_block_annoyances_persist():
    """The manual consent_clicks override + block_annoyances toggle round-trip
    through the create form into the Monitor row."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor
        settings.registration_open = True
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            await c.post("/login", data={"email": email, "password": "password123"})

            # block_annoyances omitted (checkbox unchecked) + two manual selectors
            r = await c.post("/monitors", data={
                "url": "https://example.com", "engine": "chromium", "detection_mode": "text",
                "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox",
                "name": "Consent Monitor",
                "consent_clicks": "#accept-cookies\nbutton.close-modal\n  \n",
            })
            assert r.status_code == 200

        async with SessionLocal() as s:
            m = (await s.execute(
                select(Monitor).where(Monitor.name == "Consent Monitor"))).scalar_one()
            assert m.block_annoyances is False               # unchecked => off
            assert m.consent_clicks == ["#accept-cookies", "button.close-modal"]  # blanks stripped
        return True

    assert _run(_t)


# --- Check Now: async queue + status polling --------------------------------

def test_check_now_returns_json_and_status_polls():
    """POST /check queues a check and returns a baseline; /check-status flips to
    done once a newer snapshot exists; cooldown is reported as JSON."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import User, Monitor, Snapshot, Engine, DetectionMode, SnapshotStatus, utcnow
        import watcher.scheduler.jobs as J
        settings.registration_open = True
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=False) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            await c.post("/login", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://x.test", name="CN", engine=Engine.chromium,
                            detection_mode=DetectionMode.text, interval_seconds=3600)
                s.add(m); await s.flush()
                s.add(Snapshot(monitor_id=m.id, status=SnapshotStatus.ok)); await s.flush()
                mid = m.id
                base = (await s.execute(select(Snapshot.id).where(Snapshot.monitor_id == mid)
                        .order_by(Snapshot.id.desc()))).scalars().first()
                await s.commit()

            r = await c.post(f"/monitors/{mid}/check")
            d = r.json()
            assert d["ok"] and d["baseline_id"] == base
            assert J.is_checking(mid)                       # queued => checking

            r = await c.get(f"/monitors/{mid}/check-status?since={base}")
            assert r.json() == {"done": False, "checking": True}

            # a newer snapshot appears => done (covers success/failure alike)
            async with SessionLocal() as s:
                s.add(Snapshot(monitor_id=mid, status=SnapshotStatus.error, error="boom")); await s.commit()
            J._inflight.discard(mid)
            d = (await c.get(f"/monitors/{mid}/check-status?since={base}")).json()
            assert d["done"] and d["id"] == base + 1 and d["status"] == "error" and d["checking"] is False

            # cooldown is JSON, not a redirect
            async with SessionLocal() as s:
                mm = (await s.execute(select(Monitor).where(Monitor.id == mid))).scalar_one()
                mm.last_checked_at = utcnow(); await s.commit()
            assert (await c.post(f"/monitors/{mid}/check")).json()["cooldown"] > 0
        return True

    assert _run(_t)


# --- block-page capture: store the interstitial on failure --------------------

def test_blocked_render_persists_block_page():
    """A blocked/challenge render saves its screenshot + HTML on the error
    snapshot (content-addressed), and the detail page surfaces it for review."""
    async def _t():
        from types import SimpleNamespace
        from watcher.config import settings
        from watcher.main import create_app
        from watcher import runner
        from watcher.storage import blobs
        from watcher.models import User, Monitor, Snapshot, Engine, DetectionMode, SnapshotStatus
        settings.registration_open = True
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            await c.post("/login", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                m = Monitor(user_id=u.id, url="https://uk.jbl.test/p", name="JBL", engine=Engine.camoufox,
                            detection_mode=DetectionMode.text, interval_seconds=3600)
                s.add(m); await s.flush(); mid = m.id; await s.commit()

            png = b"\x89PNG\r\n block-interstitial-pixels"
            fake = SimpleNamespace(screenshot_png=png, screenshot_mobile_png=None,
                                   html="<title>jbl.com</title>Access is temporarily restricted", title="jbl.com")
            async with SessionLocal() as s:
                # selectinload login_flow so _fail's session-expiry check works
                m = (await s.execute(select(Monitor).where(Monitor.id == mid)
                     .options(selectinload(Monitor.login_flow)))).scalar_one()
                snap = Snapshot(monitor_id=mid)
                await runner._fail(s, m, snap,
                                   error="Blocked — HTTP 403 · DataDome anti-bot protection",
                                   http_status=403, title="jbl.com", result=fake)
                sid = snap.id

            async with SessionLocal() as s:
                snap = (await s.execute(select(Snapshot).where(Snapshot.id == sid))).scalar_one()
                assert snap.status == SnapshotStatus.error
                assert snap.screenshot_blob and blobs.get_bytes(snap.screenshot_blob) == png
                assert snap.html_blob  # block-page HTML stored too

            # detail page surfaces the captured block page
            r = await c.get(f"/monitors/{mid}")
            assert r.status_code == 200
            assert "View the captured block page" in r.text
            assert "blocked / challenge page" in r.text     # history badge
            # and the image is servable
            assert (await c.get(f"/monitors/{mid}/snapshots/{sid}/image")).status_code == 200
        return True

    assert _run(_t)


# --- group: hide members from the dashboard, view inside the group -----------

def test_group_hide_members_from_dashboard():
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import User, Monitor, Group, Engine, DetectionMode
        settings.registration_open = True
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        email = _email()
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            await c.post("/login", data={"email": email, "password": "password123"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                g = Group(user_id=u.id, name="Hidden grp", kind="change", hide_members=True)
                s.add(g); await s.flush()
                mk = dict(user_id=u.id, engine=Engine.chromium, detection_mode=DetectionMode.text,
                          interval_seconds=3600)
                hidden = Monitor(url="https://hidden.test", name="HiddenMon", group_id=g.id, **mk)
                shown = Monitor(url="https://shown.test", name="ShownMon", **mk)
                s.add_all([hidden, shown]); await s.flush()
                hid, sid, gid = hidden.id, shown.id, g.id
                await s.commit()

            # dashboard: hidden member's card is gone; standalone + group card remain
            r = await c.get("/")
            assert r.status_code == 200
            assert f'href="/monitors/{sid}"' in r.text          # standalone still shown
            assert f'href="/monitors/{hid}"' not in r.text       # hidden-group member collapsed
            assert f'/groups/{gid}"' in r.text                   # the group itself still on dashboard

            # inside the group: the member renders as a dashboard-style card
            r = await c.get(f"/groups/{gid}")
            assert r.status_code == 200 and f'href="/monitors/{hid}"' in r.text

            # untick hide → member returns to the dashboard
            await c.post(f"/groups/{gid}", data={"name": "Hidden grp", "kind": "change"})
            r = await c.get("/")
            assert f'href="/monitors/{hid}"' in r.text
        return True

    assert _run(_t)


def test_cookie_viewer_and_editor():
    """GET lists stored cookies (values hidden unless ?values=1); POST applies
    edits/deletes; clearing all drops the session."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import LoginFlow, Monitor, User
        settings.registration_open = True
        email = _email()
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     headers={"Origin": "http://t"}, follow_redirects=True) as c:
            await c.post("/register", data={"email": email, "password": "password123"})
            await c.post("/monitors", data={
                "url": "https://glassdoor.com", "engine": "camoufox", "detection_mode": "auto",
                "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox", "name": "GD"})
            async with SessionLocal() as s:
                u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                mid = (await s.execute(select(Monitor.id).where(Monitor.user_id == u.id))).scalars().first()
                s.add(LoginFlow(monitor_id=mid, session_state={"cookies": [
                    {"name": "sess", "value": "SECRET", "domain": ".glassdoor.com", "path": "/", "expires": 1900000000},
                    {"name": "gdId", "value": "ID", "domain": ".glassdoor.com", "path": "/", "expires": -1},
                    {"name": "PPID", "value": "PP", "domain": ".indeed.com", "path": "/"},
                ], "origins": []}))
                await s.commit()

            # GET without values: rows present, no secret leaked
            r = await c.get(f"/monitors/{mid}/cookies")
            assert r.status_code == 200
            d = r.json()
            assert d["ok"] and len(d["cookies"]) == 3 and d["summary"]["count"] == 3
            assert "SECRET" not in r.text
            # GET with values: secrets included
            r = await c.get(f"/monitors/{mid}/cookies?values=1")
            assert any(c["value"] == "SECRET" for c in r.json()["cookies"])

            # POST: delete PPID, change sess value
            r = await c.post(f"/monitors/{mid}/cookies", json={"cookies": [
                {"name": "sess", "domain": ".glassdoor.com", "path": "/", "value": "CHANGED"},
                {"name": "gdId", "domain": ".glassdoor.com", "path": "/"},
            ]})
            assert r.status_code == 200 and r.json()["summary"]["count"] == 2
            async with SessionLocal() as s:
                m = (await s.execute(select(Monitor).where(Monitor.id == mid)
                     .options(selectinload(Monitor.login_flow)))).scalar_one()
                cks = {c["name"]: c for c in m.login_flow.session_state["cookies"]}
                assert set(cks) == {"sess", "gdId"} and cks["sess"]["value"] == "CHANGED"

            # POST empty → session cleared
            r = await c.post(f"/monitors/{mid}/cookies", json={"cookies": []})
            assert r.status_code == 200
            async with SessionLocal() as s:
                m = (await s.execute(select(Monitor).where(Monitor.id == mid)
                     .options(selectinload(Monitor.login_flow)))).scalar_one()
                assert m.login_flow.session_state is None

            # editor renders on the form page
            r = await c.get(f"/monitors/{mid}")
            assert r.status_code == 200
        return True

    assert _run(_t)


def test_manual_login_start_needs_no_creds_or_ai():
    """The 'Open site & do it manually' button starts a manual session without
    credentials or an AI key (the AI-driven start still requires them)."""
    async def _t():
        from watcher.config import settings
        from watcher.main import create_app
        from watcher.models import Monitor, User
        from watcher.auth import ai_login
        settings.registration_open = True
        email = _email()
        transport = httpx.ASGITransport(app=create_app())

        captured = {}
        async def _fake_run(sess, *a, **k):
            captured["mode"] = sess.mode
            captured["url"] = sess.url
        orig = ai_login.run_agent
        ai_login.run_agent = _fake_run
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                         headers={"Origin": "http://t"}, follow_redirects=True) as c:
                await c.post("/register", data={"email": email, "password": "password123"})
                await c.post("/monitors", data={
                    "url": "https://example.com/page", "engine": "chromium", "detection_mode": "auto",
                    "interval_minutes": "60", "wait_until": "load", "notify_channels": "inbox", "name": "M"})
                async with SessionLocal() as s:
                    u = (await s.execute(select(User).where(User.email == email))).scalar_one()
                    mid = (await s.execute(select(Monitor.id).where(Monitor.user_id == u.id))).scalars().first()

                # manual=1 → ok even with no AI key and no credentials
                r = await c.post(f"/monitors/{mid}/ai-login/start", data={"manual": "1"})
                assert r.status_code == 200 and r.json().get("ok"), r.text
                assert captured["mode"] == "manual"
                assert captured["url"] == "https://example.com/page"   # the monitored URL

                # the AI-driven start (no manual) still requires creds/AI
                r = await c.post(f"/monitors/{mid}/ai-login/start", data={})
                assert r.status_code == 400 and not r.json().get("ok")

                # SSRF: a private/internal target is refused (default policy), so
                # the login agent can't be steered at 169.254.169.254 / localhost.
                async with SessionLocal() as s:
                    m = (await s.execute(select(Monitor).where(Monitor.id == mid))).scalar_one()
                    m.url = "http://169.254.169.254/latest/meta-data/"
                    await s.commit()
                r = await c.post(f"/monitors/{mid}/ai-login/start", data={"manual": "1"})
                assert r.status_code == 400 and not r.json().get("ok"), r.text
        finally:
            ai_login.run_agent = orig
        return True

    assert _run(_t)
