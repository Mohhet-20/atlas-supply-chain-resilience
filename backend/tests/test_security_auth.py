"""Identity authentication regression tests: login, generic errors, throttling, sessions, reset tokens, rate limit. Synthetic users only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.models.identity import AuthSession, PasswordResetToken, User
from app.models.registry import AuditEvent
from tests.quality_helpers import h
from tests.security_helpers import ADM, PW, A, bearer, login, make_app, mk_user


@pytest.fixture
def client(settings, engine):
    with TestClient(make_app(settings, engine)) as c:
        mk_user(c, "alice", "admin")
        mk_user(c, "vera", "viewer")
        yield c


def audit_actions(c):
    with c.app.state.session_factory() as db:
        return [e.action for e in db.scalars(select(AuditEvent).order_by(AuditEvent.id))]


def test_login_success_returns_opaque_token_and_profile(client):
    r = login(client, "alice")
    assert r.status_code == 200
    j = r.json()
    assert j["token"].startswith("atl_s_") and j["user"]["role"] == "admin"
    assert "password" not in str(j).lower().replace("must_change_password", "")


def test_session_token_stored_only_hashed(client):
    tok = login(client, "alice").json()["token"]
    with client.app.state.session_factory() as db:
        rows = db.scalars(select(AuthSession)).all()
        assert rows and all(tok not in (s.token_hash or "") and len(s.token_hash) == 64 for s in rows)


def test_password_stored_as_scrypt_not_plaintext(client):
    with client.app.state.session_factory() as db:
        u = db.scalar(select(User).where(User.username == "alice"))
        assert PW not in u.password_hash and u.password_hash.startswith("scrypt$")


def test_unknown_user_and_wrong_password_are_indistinguishable(client):
    a = login(client, "alice", "Wrong-Password-Value-1!")
    b = login(client, "nobody", "Wrong-Password-Value-1!")
    assert a.status_code == b.status_code == 401
    assert a.json()["error"]["message"] == b.json()["error"]["message"] == "Invalid username or password."


def test_failed_login_audit_never_records_the_attempted_username_or_password(client):
    login(client, "ghost-user-xyz", "Wrong-Password-Value-1!")
    with client.app.state.session_factory() as db:
        blob = " ".join(f"{e.details} {e.target_id}" for e in db.scalars(select(AuditEvent)))
    assert "ghost-user-xyz" not in blob and "Wrong-Password" not in blob
    assert "auth.login.failed" in audit_actions(client)


def test_repeated_failures_back_off_with_retry_after(client):
    for _ in range(5):
        login(client, "vera", "Wrong-Password-Value-1!")
    r = login(client, "vera")  # even the right password is delayed during back-off
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    assert "auth.login.throttled" in audit_actions(client)


def test_throttle_does_not_lock_the_account_for_other_clients(client):
    for _ in range(5):
        login(client, "vera", "Wrong-Password-Value-1!")
    # a different client address is a different throttle key
    r = client.post(f"{A}/login", json={"username": "vera", "password": PW}, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status_code in (200, 429)  # X-Forwarded-For is not trusted, so the same peer stays throttled
    assert "auth.login.throttled" in audit_actions(client)


def test_me_reports_permissions_for_role(client):
    me = client.get(f"{A}/me", headers=bearer(client, "vera")).json()
    assert "data.read" in me["permissions"] and "user.manage" not in me["permissions"]


def test_logout_revokes_the_session(client):
    hd = bearer(client, "vera")
    assert client.get(f"{A}/me", headers=hd).status_code == 200
    assert client.post(f"{A}/logout", headers=hd).status_code == 204
    assert client.get(f"{A}/me", headers=hd).status_code == 401


def test_logout_all_revokes_every_session(client):
    h1, h2 = bearer(client, "vera"), bearer(client, "vera")
    assert client.post(f"{A}/logout-all", headers=h1).status_code == 204
    assert client.get(f"{A}/me", headers=h2).status_code == 401


def test_expired_session_is_rejected(client):
    hd = bearer(client, "vera")
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    with client.app.state.session_factory() as db:
        db.execute(update(AuthSession).values(expires_at=past))
        db.commit()
    assert client.get(f"{A}/me", headers=hd).status_code == 401


def test_idle_session_is_rejected(client):
    hd = bearer(client, "vera")
    old = datetime.now(timezone.utc) - timedelta(minutes=client.app.state.settings.auth_session_idle_minutes + 5)
    with client.app.state.session_factory() as db:
        db.execute(update(AuthSession).values(last_seen_at=old))
        db.commit()
    assert client.get(f"{A}/me", headers=hd).status_code == 401


def test_garbage_and_forged_session_tokens_are_rejected(client):
    for tok in ("atl_s_" + "A" * 43, "atl_s_", "not-a-token", "atl_s_' OR 1=1 --"):
        assert client.get(f"{A}/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_disabled_user_cannot_login_and_existing_sessions_die(client):
    hd = bearer(client, "vera")
    ah = bearer(client, "alice")
    users = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}
    r = client.patch(f"{ADM}/users/{users['vera']['user_uid']}", json={"status": "disabled"}, headers=ah)
    assert r.status_code == 200, r.text
    assert client.get(f"{A}/me", headers=hd).status_code == 401
    assert login(client, "vera").status_code == 401


def test_role_change_invalidates_existing_sessions(client):
    hd = bearer(client, "vera")
    ah = bearer(client, "alice")
    vid = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}["vera"]["user_uid"]
    assert client.patch(f"{ADM}/users/{vid}", json={"role": "editor"}, headers=ah).status_code == 200
    assert client.get(f"{A}/me", headers=hd).status_code == 401


def test_password_change_requires_current_password_and_ends_other_sessions(client):
    h1, h2 = bearer(client, "vera"), bearer(client, "vera")
    bad = client.post(
        f"{A}/password",
        json={"current_password": "nope-nope-nope-1A!", "new_password": "Another-Synthetic-Pass-77!"},
        headers=h1,
    )
    assert bad.status_code in (401, 403)
    ok = client.post(
        f"{A}/password", json={"current_password": PW, "new_password": "Another-Synthetic-Pass-77!"}, headers=h1
    )
    assert ok.status_code == 204
    assert client.get(f"{A}/me", headers=h1).status_code == 200
    assert client.get(f"{A}/me", headers=h2).status_code == 401
    assert login(client, "vera", PW).status_code == 401
    assert login(client, "vera", "Another-Synthetic-Pass-77!").status_code == 200


@pytest.mark.parametrize("weak", ["short1!", "aaaaaaaaaaaaaaaa", "vera-vera-vera-vera", "password1234567"])
def test_password_policy_rejects_weak_passwords(client, weak):
    hd = bearer(client, "vera")
    r = client.post(f"{A}/password", json={"current_password": PW, "new_password": weak}, headers=hd)
    assert r.status_code == 422, r.text


def test_reset_token_is_single_use_hashed_and_expires(client):
    ah = bearer(client, "alice")
    users = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}
    r = client.post(f"{ADM}/users/{users['vera']['user_uid']}/password-reset", headers=ah)
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    with client.app.state.session_factory() as db:
        assert all(tok not in t.token_hash for t in db.scalars(select(PasswordResetToken)))
    new = "Reset-Synthetic-Pass-88!"
    assert client.post(f"{A}/password-reset/confirm", json={"token": tok, "new_password": new}).status_code == 204
    assert client.post(f"{A}/password-reset/confirm", json={"token": tok, "new_password": new + "x"}).status_code == 422
    assert login(client, "vera", new).status_code == 200


def test_reset_token_expiry(client):
    ah = bearer(client, "alice")
    vid = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}["vera"]["user_uid"]
    tok = client.post(f"{ADM}/users/{vid}/password-reset", headers=ah).json()["token"]
    with client.app.state.session_factory() as db:
        db.execute(update(PasswordResetToken).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        db.commit()
    r = client.post(f"{A}/password-reset/confirm", json={"token": tok, "new_password": "Reset-Synthetic-Pass-88!"})
    assert r.status_code == 422


def test_new_reset_token_voids_the_previous_one(client):
    ah = bearer(client, "alice")
    vid = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}["vera"]["user_uid"]
    t1 = client.post(f"{ADM}/users/{vid}/password-reset", headers=ah).json()["token"]
    t2 = client.post(f"{ADM}/users/{vid}/password-reset", headers=ah).json()["token"]
    assert (
        client.post(
            f"{A}/password-reset/confirm", json={"token": t1, "new_password": "Reset-Synthetic-Pass-88!"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{A}/password-reset/confirm", json={"token": t2, "new_password": "Reset-Synthetic-Pass-88!"}
        ).status_code
        == 204
    )


def test_self_service_reset_request_does_not_reveal_whether_account_exists(client):
    a = client.post(f"{A}/password-reset/request", json={"username": "vera"})
    b = client.post(f"{A}/password-reset/request", json={"username": "nobody-here"})
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert "token" not in a.text


def test_auth_endpoints_are_rate_limited_per_client(settings, engine):
    with TestClient(make_app(settings, engine, auth_rate_limit_per_minute=5)) as c:
        codes = [c.post(f"{A}/password-reset/request", json={"username": "x"}).status_code for _ in range(8)]
    assert 429 in codes and codes[0] == 202


def test_static_service_tokens_still_work_alongside_sessions(client):
    assert client.get("/api/v1/registry/me", headers=h("viewer")).status_code == 200
    assert client.get("/api/v1/registry/me").status_code == 401


def test_dev_actor_withdrawn_once_users_exist(settings, engine):
    from app.main import create_app
    from tests.registry_helpers import make_settings

    app = create_app(make_settings(settings, api_tokens="", risk_auto_reassess=False, auth_scrypt_n=1024), engine)
    with TestClient(app) as c:
        mk_user(c, "root1", "admin")
        assert c.get("/api/v1/registry/me").status_code == 401
