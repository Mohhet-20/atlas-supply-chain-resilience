"""Integrations: service accounts and API keys - hashing, one-time display, scopes, expiry, revocation, rotation, tenancy."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.integrations import apikeys
from app.models.integrations import ApiKey
from app.models.types import utcnow
from tests.integrations_helpers import API, admin, client_for, h, mk_integration, mk_org, mk_user
from tests.security_helpers import bearer

SA = f"{API}/service-accounts"


@pytest.fixture
def c(settings, engine):
    with client_for(settings, engine) as client:
        yield client


def mk_sa(c, name="Reporting bot", role="reviewer", headers=None):
    r = c.post(SA, json={"name": name, "role": role}, headers=headers or admin())
    assert r.status_code == 201, r.text
    return r.json()["sa_uid"]


def mk_key(c, sa, headers=None, **body):
    r = c.post(f"{SA}/{sa}/keys", json={"name": "ci key"} | body, headers=headers or admin())
    assert r.status_code == 201, r.text
    return r.json()


def use(c, key, path="/integrations"):
    return c.get(f"{API}{path}", headers={"Authorization": f"Bearer {key['secret']}"})


def test_secret_is_shown_once_stored_only_as_a_hash_and_never_listed(c):
    sa = mk_sa(c)
    key = mk_key(c, sa)
    assert key["secret"].startswith("atl_k_") and len(key["secret"]) > 40
    assert key["prefix"] == key["secret"][:12]
    listed = c.get(f"{SA}/{sa}/keys", headers=admin())
    assert key["secret"] not in listed.text and "secret" not in listed.json()["items"][0]
    with c.app.state.session_factory() as db:
        row = db.scalar(select(ApiKey).where(ApiKey.key_uid == key["key_uid"]).execution_options(skip_tenant=True))
        assert row.key_hash == apikeys.digest(key["secret"]) and key["secret"] not in row.key_hash
    assert c.post(f"{SA}/{sa}/keys", json={"name": "ci key"}, headers=admin()).headers["cache-control"] == "no-store"


def test_key_authenticates_with_its_role_and_is_audited(c):
    key = mk_key(c, mk_sa(c, role="reviewer"))
    assert use(c, key).status_code == 200  # reviewer holds integration.read
    assert (
        c.post(
            f"{API}/integrations",
            json={"name": "By key", "provider": "sandbox_inventory"},
            headers={"Authorization": f"Bearer {key['secret']}"},
        ).status_code
        == 403
    )
    audit = c.get(f"{API}/registry/audit?limit=100", headers=admin())
    assert audit.status_code == 200 and "api_key.created" in audit.text and key["secret"] not in audit.text


def test_scopes_only_narrow_permission_checked_routes(c):
    sa = mk_sa(c, role="reviewer")
    narrow = mk_key(c, sa, scopes=["alert.read"])
    assert use(c, narrow).status_code == 403  # integration.read is outside the scope list
    assert use(c, narrow, "/alerting/alerts").status_code == 200
    assert (
        c.post(
            f"{SA}/{sa}/keys", json={"name": "bad scope", "scopes": ["integration.admin"]}, headers=admin()
        ).status_code
        == 422
    )
    assert (
        c.post(f"{SA}/{sa}/keys", json={"name": "bad scope", "scopes": ["user.manage"]}, headers=admin()).status_code
        == 422
    )


def test_service_accounts_are_never_administrators_and_keys_cannot_exceed_them(c):
    assert c.post(SA, json={"name": "Root bot", "role": "admin"}, headers=admin()).status_code == 422
    sa = mk_sa(c, role="editor")
    assert c.post(f"{SA}/{sa}/keys", json={"name": "too much", "role": "reviewer"}, headers=admin()).status_code == 422


def test_only_integration_admins_manage_accounts_and_keys(c):
    for role in ("viewer", "editor", "reviewer"):
        assert c.get(SA, headers=h(role)).status_code == 403
        assert c.post(SA, json={"name": "Denied bot"}, headers=h(role)).status_code == 403
    assert c.get(SA).status_code == 401


def test_expiry_is_mandatory_bounded_and_enforced(c):
    sa = mk_sa(c)
    assert c.post(f"{SA}/{sa}/keys", json={"name": "forever", "ttl_days": 5000}, headers=admin()).status_code == 422
    key = mk_key(c, sa)
    assert key["expires_at"] is not None
    with c.app.state.session_factory() as db:
        row = db.scalar(select(ApiKey).where(ApiKey.key_uid == key["key_uid"]).execution_options(skip_tenant=True))
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    r = use(c, key)
    assert r.status_code == 401
    assert (
        r.json()["error"]["message"] == "The API key is not valid, expired or revoked."
    )  # one message for every cause


def test_revocation_is_immediate_and_failures_look_identical(c):
    sa = mk_sa(c)
    key = mk_key(c, sa)
    assert use(c, key).status_code == 200
    assert c.post(f"{SA}/keys/{key['key_uid']}/revoke", headers=admin()).status_code == 200
    revoked = use(c, key)
    unknown = c.get(f"{API}/integrations", headers={"Authorization": "Bearer atl_k_" + "x" * 43})
    assert revoked.status_code == unknown.status_code == 401
    strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
    assert strip(revoked) == strip(unknown)


def test_rotation_gives_a_grace_period_then_the_old_key_stops(c):
    sa = mk_sa(c)
    old = mk_key(c, sa)
    new = c.post(f"{SA}/keys/{old['key_uid']}/rotate", headers=admin()).json()
    assert new["secret"] != old["secret"] and new["rotated_from"] == old["key_uid"]
    assert use(c, old).status_code == 200 and use(c, new).status_code == 200  # both valid during the grace period
    with c.app.state.session_factory() as db:
        row = db.scalar(select(ApiKey).where(ApiKey.key_uid == old["key_uid"]).execution_options(skip_tenant=True))
        assert row.expires_at.replace(tzinfo=None) <= (utcnow() + timedelta(hours=24, minutes=1)).replace(tzinfo=None)
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    assert use(c, old).status_code == 401 and use(c, new).status_code == 200
    assert c.post(f"{SA}/keys/{old['key_uid']}/revoke", headers=admin()).status_code == 200
    assert c.post(f"{SA}/keys/{old['key_uid']}/rotate", headers=admin()).status_code == 409


def test_usage_is_tracked_and_disabling_the_account_revokes_keys(c):
    sa = mk_sa(c)
    key = mk_key(c, sa)
    use(c, key)
    listed = c.get(f"{SA}/{sa}/keys", headers=admin()).json()["items"][0]
    assert listed["use_count"] >= 1 and listed["last_used_at"]
    assert c.post(f"{SA}/{sa}/disable", headers=admin()).json()["status"] == "disabled"
    assert use(c, key).status_code == 401
    assert c.post(f"{SA}/{sa}/keys", json={"name": "after disable"}, headers=admin()).status_code == 409


def test_a_key_is_confined_to_its_own_organisation(settings, engine):
    with client_for(settings, engine) as c:
        org2 = mk_org(c, "umbrella")
        mk_user(c, "bob", "admin", org_id=org2)
        b = bearer(c, "bob")
        mk_integration(c)  # belongs to the default organisation
        sa2 = mk_sa(c, "Org two bot", "reviewer", headers=b)
        key2 = mk_key(c, sa2, headers=b)
        assert use(c, key2).json()["total"] == 0  # sees organisation 2 only
        assert c.get(f"{SA}", headers=b).json()["total"] == 1
        sa1 = mk_sa(c)
        assert c.get(f"{SA}/{sa1}/keys", headers=b).status_code == 404  # cannot see or manage organisation 1's account
        k1 = mk_key(c, sa1)
        assert c.post(f"{SA}/keys/{k1['key_uid']}/revoke", headers=b).status_code == 404
        assert use(c, k1).json()["total"] == 1
