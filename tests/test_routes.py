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
