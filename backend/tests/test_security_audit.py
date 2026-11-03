"""Identity audit-logging regression tests: security events recorded, no secrets in details, chain integrity, append-only, org scoping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app.models.registry import AuditEvent
from app.registry import audit
from tests.quality_helpers import h
from tests.security_helpers import ADM, PW, A, bearer, login, make_app, mk_org, mk_user


@pytest.fixture
def client(settings, engine):
    with TestClient(make_app(settings, engine)) as c:
        mk_user(c, "alice", "admin")
        mk_user(c, "vera", "viewer")
        yield c


def events(c):
    with c.app.state.session_factory() as db:
        return list(db.scalars(select(AuditEvent).order_by(AuditEvent.id)))


def actions(c):
    return [e.action for e in events(c)]


def test_authentication_lifecycle_is_audited(client):
    login(client, "vera", "Wrong-Password-Value-1!")
    hd = bearer(client, "vera")
    client.post(f"{A}/logout", headers=hd)
    got = actions(client)
    for a in ("auth.login.failed", "auth.login.succeeded", "auth.logout"):
        assert a in got, got


def test_admin_actions_are_audited_with_actor_and_target(client):
    ah = bearer(client, "alice")
    users = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}
    vid = users["vera"]["user_uid"]
    client.patch(f"{ADM}/users/{vid}", json={"role": "editor"}, headers=ah)
    client.post(f"{ADM}/users/{vid}/password-reset", headers=ah)
    client.post(f"{ADM}/users/{vid}/revoke-sessions", headers=ah)
    by_action = {e.action: e for e in events(client)}
    assert (
        by_action["admin.user.role_changed"].actor == "alice" and by_action["admin.user.role_changed"].target_id == vid
    )
    assert "admin.user.password_reset_issued" in by_action and "admin.user.sessions_revoked" in by_action


def test_refused_privilege_change_is_audited_as_denied(client):
    ah = bearer(client, "alice")
    me = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}["alice"]
    client.patch(f"{ADM}/users/{me['user_uid']}", json={"role": "viewer"}, headers=ah)
    assert any(e.action == "admin.user.change_refused" and e.outcome.value == "DENIED" for e in events(client))


def test_permission_denials_are_audited(client):
    client.get(f"{ADM}/users", headers=h("viewer"))
    assert any(e.outcome.value == "DENIED" for e in events(client))


def test_audit_details_never_contain_secrets(client):
    ah = bearer(client, "alice")
    vid = {u["username"]: u for u in client.get(f"{ADM}/users", headers=ah).json()["items"]}["vera"]["user_uid"]
    tok = client.post(f"{ADM}/users/{vid}/password-reset", headers=ah).json()["token"]
    client.post(f"{A}/password-reset/confirm", json={"token": tok, "new_password": "Reset-Synthetic-Pass-88!"})
    client.post(
        f"{ADM}/users", json={"username": "newbie", "password": "Brand-New-Synthetic-5!", "role": "viewer"}, headers=ah
    )
    login(client, "vera", "Wrong-Password-Value-1!")
    blob = " ".join(f"{e.details} {e.target_id} {e.actor}" for e in events(client))
    session_tok = ah["Authorization"][7:]
    for secret in (
        tok,
        PW,
        "Reset-Synthetic-Pass-88!",
        "Brand-New-Synthetic-5!",
        "Wrong-Password-Value-1!",
        session_tok,
    ):
        assert secret not in blob
    assert "scrypt$" not in blob


def test_hash_chain_verifies_across_org_scoped_and_unscoped_events(client):
    o2 = mk_org(client, "beta")
    mk_user(client, "bob-b", "admin", org_id=o2)
    bearer(client, "bob-b")
    login(client, "alice", "Wrong-Password-Value-1!")
    orgs = {e.organization_id for e in events(client)}
    assert o2 in orgs
    with client.app.state.session_factory() as db:
        assert audit.verify_chain(db)["ok"] is True


def test_tampering_with_an_event_is_detected_by_the_chain(client):
    bearer(client, "alice")
    with client.app.state.session_factory() as db:
        on = " ON audit_events" if db.bind.dialect.name == "postgresql" else ""
        db.execute(text(f"DROP TRIGGER IF EXISTS trg_audit_events_no_update{on}"))
        eid = db.scalars(select(AuditEvent.id).order_by(AuditEvent.id)).first()
        db.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE id = :i"), {"i": eid})
        db.commit()
        res = audit.verify_chain(db)
    assert res["ok"] is False and res["first_bad_event_id"] == eid


def test_audit_table_is_append_only_in_the_database(client):
    bearer(client, "alice")
    with client.app.state.session_factory() as db:
        for sql in ("UPDATE audit_events SET actor = 'x'", "DELETE FROM audit_events"):
            with pytest.raises(DatabaseError):
                db.execute(text(sql))
            db.rollback()


def test_audit_endpoint_requires_reviewer_and_verify_requires_admin(client):
    assert client.get("/api/v1/registry/audit", headers=h("viewer")).status_code == 403
    assert client.get("/api/v1/registry/audit", headers=h("reviewer")).status_code == 200
    assert client.get("/api/v1/registry/audit/verify", headers=h("reviewer")).status_code == 403
    assert client.get("/api/v1/registry/audit/verify", headers=h("admin")).json()["ok"] is True


def test_org_admins_only_see_their_own_organisations_events(client):
    o2 = mk_org(client, "beta")
    mk_user(client, "bob-b", "admin", org_id=o2)
    bh = bearer(client, "bob-b")
    bearer(client, "alice")
    seen = client.get("/api/v1/registry/audit?limit=100", headers=bh).json()["items"]
    assert seen and all(e["actor"] != "alice" for e in seen)
    summary = client.get(f"{ADM}/security/summary", headers=bh)
    assert summary.status_code == 200 and "alice" not in summary.text


def test_security_summary_counts_failed_logins(client):
    ah = bearer(client, "alice")
    for _ in range(2):
        login(client, "vera", "Wrong-Password-Value-1!")
    s = client.get(f"{ADM}/security/summary?hours=1", headers=ah).json()
    assert "auth.login.failed" in str(s)
