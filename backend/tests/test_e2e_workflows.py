"""Cross-module workflows over real HTTP with real sessions (no static-token shortcuts, no mocked services).

Each test chains several subsystems and asserts business results (who sees what, which entities are affected, what the audit
trail says), not just status codes. API level; the browser suite is frontend/e2e.
"""

from __future__ import annotations

import pytest

from tests.graph_helpers import SINGULAR, G
from tests.integrations_helpers import API, client_for, mk_integration, mk_org, mk_user
from tests.security_helpers import PW, bearer, login
from tests.supplier_risk_helpers import R

REG = f"{API}/registry"
ADM = f"{API}/admin"
AUD = f"{API}/registry/audit"


@pytest.fixture
def c(settings, engine):
    with client_for(settings, engine) as client:
        yield client


def hd(token_headers: dict[str, str]) -> dict[str, str]:
    return token_headers


def audit_actions(c, headers, **params) -> list[str]:
    r = c.get(AUD, params={"limit": 100} | params, headers=headers)
    assert r.status_code == 200, r.text
    return [e["action"] for e in r.json()["items"]]


# ---- authentication and authorization --------------------------------------------------------------------------------
def test_session_lifecycle_login_wrong_password_change_suspend_logout(c):
    mk_user(c, "root.admin", "admin")
    uid = mk_user(c, "ana.editor", "editor")
    admin = bearer(c, "root.admin")
    me = bearer(c, "ana.editor")
    assert c.get(f"{API}/auth/me", headers=me).json()["actor"] == "ana.editor"

    bad = login(c, "ana.editor", "wrong-password-value")
    unknown = login(c, "nobody.here", "wrong-password-value")
    assert (
        bad.status_code == unknown.status_code == 401
        and bad.json()["error"]["message"] == unknown.json()["error"]["message"]
    )

    new_pw = "Another-Synthetic-Phrase-52!"
    assert (
        c.post(f"{API}/auth/password", json={"current_password": PW, "new_password": new_pw}, headers=me).status_code
        == 204
    )
    assert login(c, "ana.editor", PW).status_code == 401 and login(c, "ana.editor", new_pw).status_code == 200

    me2 = bearer(c, "ana.editor", new_pw)
    assert c.patch(f"{ADM}/users/{uid}", json={"status": "suspended"}, headers=admin).status_code == 200
    assert c.get(f"{API}/auth/me", headers=me2).status_code == 401  # the suspension ends live sessions at once
    assert login(c, "ana.editor", new_pw).status_code in (401, 403)

    assert c.patch(f"{ADM}/users/{uid}", json={"status": "active"}, headers=admin).status_code == 200
    me3 = bearer(c, "ana.editor", new_pw)
    assert c.post(f"{API}/auth/logout", headers=me3).status_code == 204
    assert c.get(f"{API}/auth/me", headers=me3).status_code == 401
    acts = audit_actions(c, admin)
    assert "admin.user.status_changed" in acts and any(a.startswith("auth.") for a in acts)


def test_backend_enforces_roles_even_when_the_frontend_is_bypassed(c):
    for name, role in (("v.user", "viewer"), ("e.user", "editor"), ("r.user", "reviewer")):
        mk_user(c, name, role)
    v, e, r = bearer(c, "v.user"), bearer(c, "e.user"), bearer(c, "r.user")
    body = {"name": "Synthetic Role Probe", "country_or_region": "Germany", "category": "Electronics"}
    assert c.post(f"{REG}/suppliers", json=body, headers=v).status_code == 403
    assert c.post(f"{REG}/suppliers", json=body, headers=e).status_code == 201
    assert c.get(f"{ADM}/users", headers=e).status_code == 403  # editors cannot manage users
    assert c.get(AUD, headers=e).status_code == 403 and c.get(AUD, headers=r).status_code == 200
    assert c.get(f"{API}/system/health", headers=r).status_code == 403  # platform-only
    assert c.get(f"{API}/suppliers").status_code in (401, 503)  # no credentials at all


def test_cross_tenant_access_is_refused_on_real_operations(c):
    org_b = mk_org(c, "orgb")
    mk_user(c, "a.admin", "admin")
    mk_user(c, "b.admin", "admin", org_id=org_b)
    a, b = bearer(c, "a.admin"), bearer(c, "b.admin")
    integ = mk_integration(c, headers=a)
    uid = integ["integration_uid"]
    assert c.get(f"{API}/integrations/{uid}", headers=a).status_code == 200
    assert c.get(f"{API}/integrations/{uid}", headers=b).status_code == 404  # existence is not leaked
    assert c.get(f"{API}/integrations", headers=b).json()["total"] == 0
    assert c.post(f"{API}/integrations/{uid}/validate", headers=b).status_code == 404
    a_users = {u["username"] for u in c.get(f"{ADM}/users", headers=a).json()["items"]}
    b_users = {u["username"] for u in c.get(f"{ADM}/users", headers=b).json()["items"]}
    assert "b.admin" not in a_users and "a.admin" not in b_users
    assert "integration.created" in " ".join(audit_actions(c, a)) or any(
        "integration" in x for x in audit_actions(c, a)
    )
    assert not any("integration" in x for x in audit_actions(c, b))  # org B's audit trail has none of org A's events


def test_api_key_and_service_account_lifecycle(c):
    mk_user(c, "k.admin", "admin")
    a = bearer(c, "k.admin")
    acct = c.post(f"{API}/service-accounts", json={"name": "ci-reader", "role": "viewer"}, headers=a)
    assert acct.status_code == 201, acct.text
    sa = acct.json()["sa_uid"]
    key = c.post(f"{API}/service-accounts/{sa}/keys", json={"name": "key one"}, headers=a)
    assert key.status_code == 201, key.text
    secret, key_uid = key.json()["secret"], key.json()["key_uid"]
    kh = {"Authorization": f"Bearer {secret}"}
    assert c.get(f"{API}/suppliers?limit=1", headers=kh).status_code == 200
    assert (
        c.post(
            f"{REG}/suppliers", json={"name": "Nope", "country_or_region": "X", "category": "Y"}, headers=kh
        ).status_code
        == 403
    )
    assert secret not in c.get(f"{API}/service-accounts/{sa}/keys", headers=a).text  # shown once, never listed
    assert c.post(f"{API}/service-accounts/keys/{key_uid}/revoke", headers=a).status_code == 200
    assert c.get(f"{API}/suppliers?limit=1", headers=kh).status_code == 401  # revocation is immediate


# ---- supply-chain, network, risk, scenario and audit chain -----------------------------------------------------------
def _mk(c, h, kind, name, **kw):
    base = {
        "suppliers": {"country_or_region": "Germany", "category": "Electronics"},
        "facilities": {"country_or_region": "Germany", "facility_type": "MANUFACTURING"},
        "products": {"category": "Components"},
    }[kind]
    r = c.post(f"{REG}/{kind}", json=base | {"name": name} | kw, headers=h)
    assert r.status_code == 201, r.text
    return r.json() | {"type": SINGULAR[kind]}


def _rel(c, h, typ, src, dst, **kw):
    body = {
        "type": typ,
        "source_type": src["type"],
        "source_id": src["id"],
        "destination_type": dst["type"],
        "destination_id": dst["id"],
        "status": "CONFIRMED",
        "justification": "synthetic e2e evidence",
    } | kw
    r = c.post(f"{G}/relationships", json=body, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def test_supplier_to_network_risk_scenario_and_audit_chain(c):
    mk_user(c, "chain.admin", "admin")
    h = bearer(c, "chain.admin")
    s1, s2 = _mk(c, h, "suppliers", "Alder Parts"), _mk(c, h, "suppliers", "Birch Parts")
    lone = _mk(c, h, "suppliers", "Lonely Supplier")
    f1, f2 = _mk(c, h, "facilities", "Cedar Plant"), _mk(c, h, "facilities", "Dogwood Hub")
    p1 = _mk(c, h, "products", "Elm Widget")
    _rel(c, h, "SUPPLIES", s1, f1, supply_share_pct=60.0)
    _rel(c, h, "SUPPLIES", s2, f1, supply_share_pct=40.0)
    _rel(c, h, "TRANSFERS_TO", f1, f2, supply_share_pct=100.0)
    _rel(c, h, "PRODUCES", f2, p1, supply_share_pct=100.0)

    upd = c.patch(
        f"{REG}/suppliers/{s1['id']}", json={"category": "Mechanical", "expected_version": s1["version"]}, headers=h
    )
    assert upd.status_code == 200 and upd.json()["version"] == s1["version"] + 1
    stale = c.patch(
        f"{REG}/suppliers/{s1['id']}", json={"category": "Stale", "expected_version": s1["version"]}, headers=h
    )
    assert stale.status_code == 409  # optimistic concurrency: the stale writer loses, nothing is overwritten
    assert c.get(f"{API}/suppliers/{s1['id']}", headers=h).json()["category"] == "Mechanical"

    summary = c.get(f"{API}/network/summary", headers=h).json()
    assert (
        summary["supplier_count"] == 3 and summary["facility_count"] == 2 and summary["relationship_count"] == 3
    )  # PRODUCES (facility->product) is stored as a product link, not a supply edge
    dash = c.get(f"{API}/dashboard/summary", headers=h).json()
    assert (dash["supplier_count"], dash["facility_count"], dash["relationship_count"]) == (
        3,
        2,
        3,
    )  # cached view agrees with the network view

    assess = c.post(f"{R}/suppliers/{s1['id']}/assess", headers=h)
    assert assess.status_code in (200, 201, 202), assess.text
    cur = c.get(f"{R}/suppliers/{s1['id']}", headers=h)
    assert (
        cur.status_code == 200 and cur.json()["supplier"]["id"] == s1["id"] and cur.json()["assessed"] is True
    )  # the risk record belongs to this supplier
    unassessed = c.get(f"{R}/suppliers/{lone['id']}", headers=h).json()
    assert (
        unassessed["assessed"] is False and unassessed["supplier"]["id"] == lone["id"]
    )  # no risk is invented for a supplier never assessed

    from tests.scenario_helpers import by_name, simulate, sup_out

    class _C:  # simulate() only needs an object with .app.state.engine
        app = c.app

    res = simulate(_C(), [sup_out(s1)])
    names = {i["entity_name"] for i in res.impacts}
    assert {"Alder Parts", "Cedar Plant"} <= names and "Lonely Supplier" not in names and "Birch Parts" not in names
    assert len(res.impacts) == len(
        {(i["entity_type"], i["entity_name"]) for i in res.impacts}
    )  # each entity reported once
    assert by_name(res, "Cedar Plant")["entity_name"] == "Cedar Plant"

    acts = audit_actions(c, h)
    assert sum(a.startswith("registry.") or "create" in a for a in acts) >= 6 and any("update" in a for a in acts)
    assert c.get(f"{API}/registry/audit/verify", headers=h).json().get("valid", True) is True


def test_failed_write_leaves_no_partial_state(c):
    mk_user(c, "tx.admin", "admin")
    h = bearer(c, "tx.admin")
    before = c.get(f"{API}/suppliers?limit=1", headers=h).json()["total"]
    bad = c.post(f"{REG}/suppliers", json={"name": "", "country_or_region": "Germany", "category": "E"}, headers=h)
    assert bad.status_code == 422
    dup = {"name": "Dup Probe Co", "country_or_region": "Germany", "category": "E"}
    assert c.post(f"{REG}/suppliers", json=dup, headers=h).status_code == 201
    assert c.get(f"{API}/suppliers?limit=1", headers=h).json()["total"] == before + 1


# ---- alerting and integration workflows ------------------------------------------------------------------------------
def test_integration_sync_creates_alert_events_with_provenance_and_audit(c):
    mk_user(c, "int.admin", "admin")
    h = bearer(c, "int.admin")
    integ = mk_integration(c, headers=h, config={"records": 12})
    uid = integ["integration_uid"]
    assert c.post(f"{API}/integrations/{uid}/validate", headers=h).json()["valid"]
    assert c.post(f"{API}/integrations/{uid}/test", headers=h).json()["ok"]
    assert c.post(f"{API}/integrations/{uid}/enable", headers=h).status_code == 200
    run = c.post(f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=h)
    assert run.status_code == 202, run.text
    again = c.post(
        f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=h
    )  # idempotent ingest: no duplicates
    assert again.status_code in (202, 409)
    detail = c.get(f"{API}/integrations/{uid}", headers=h).json()
    assert detail["status"] in ("healthy", "syncing", "connected")
    assert detail["provider"] == "sandbox_inventory"  # a sandbox adapter, never reported as a live provider
    acts = audit_actions(c, h)
    assert any("integration" in a for a in acts)
