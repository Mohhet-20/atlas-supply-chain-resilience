"""Guest access for a public demo: off by default, never an administrator, no password involved. Synthetic users only."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.registry import AuditEvent
from tests.security_helpers import make_app, mk_user


def guest_client(settings, engine, *, enabled: bool = True, role: str = "editor", **user_kw) -> TestClient:
    c = TestClient(make_app(settings, engine, guest_access_enabled=enabled, guest_username="guest"))
    c.__enter__()
    mk_user(c, "guest", role, **user_kw)
    return c


def test_guest_access_is_off_unless_enabled(settings, engine):
    c = guest_client(settings, engine, enabled=False)
    try:
        assert c.post("/api/v1/auth/guest").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_guest_session_signs_in_as_the_guest_editor_without_a_password(settings, engine):
    c = guest_client(settings, engine)
    try:
        r = c.post("/api/v1/auth/guest")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token"].startswith("atl_s_") and body["user"]["username"] == "guest"
        assert body["user"]["role"] == "editor" and body["user"]["platform_admin"] is False
        me = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me.status_code == 200 and me.json()["role"] == "editor"
        with c.app.state.session_factory() as db:
            assert "auth.guest.started" in [e.action for e in db.scalars(select(AuditEvent))]
    finally:
        c.__exit__(None, None, None)


def test_guest_session_is_refused_for_an_admin_account(settings, engine):
    c = guest_client(settings, engine, role="admin")
    try:
        assert c.post("/api/v1/auth/guest").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_guest_session_is_refused_for_a_platform_admin(settings, engine):
    c = guest_client(settings, engine, role="editor", platform_admin=True)
    try:
        assert c.post("/api/v1/auth/guest").status_code == 404
    finally:
        c.__exit__(None, None, None)


def test_guest_cannot_manage_users(settings, engine):
    c = guest_client(settings, engine)
    try:
        token = c.post("/api/v1/auth/guest").json()["token"]
        r = c.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        c.__exit__(None, None, None)
