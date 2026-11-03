"""Integrations: integration registry API - lifecycle, validation, authorisation, pagination, secret hygiene, tenant isolation."""

from __future__ import annotations

import pytest

from tests.integrations_helpers import API, admin, client_for, connect, enable, h, mk_integration, mk_org, mk_user
from tests.security_helpers import bearer


@pytest.fixture
def c(settings, engine):
    with client_for(settings, engine) as client:
        yield client


def test_catalogue_labels_sandbox_and_extension_points(c):
    items = {i["key"]: i for i in c.get(f"{API}/integrations/connectors", headers=h("reviewer")).json()["items"]}
    assert items["sandbox_inventory"]["status"] == "sandbox"
    assert items["rest_json"]["status"] == "implemented"
    ext = [i for i in items.values() if i["status"] == "extension_point"]
    assert {"sap_s4hana", "oracle_erp"} & {i["key"] for i in ext}
    assert all(not i["creatable"] for i in ext)  # nothing claims a live provider


def test_extension_point_and_unknown_providers_cannot_be_created(c):
    for provider in ("sap_s4hana", "no_such_connector", "os.system"):
        r = c.post(f"{API}/integrations", json={"name": "Nope nope", "provider": provider}, headers=admin())
        assert r.status_code in (404, 422), (provider, r.text)


def test_full_lifecycle_requires_validation_then_test_before_enable(c):
    i = mk_integration(c)
    uid = i["integration_uid"]
    assert i["status"] == "draft" and i["enabled"] is False
    assert c.post(f"{API}/integrations/{uid}/enable", headers=admin()).status_code == 409  # not connected yet
    connect(c, uid)
    assert c.get(f"{API}/integrations/{uid}", headers=admin()).json()["status"] == "connected"
    en = c.post(f"{API}/integrations/{uid}/enable", headers=admin())
    assert en.status_code == 200 and en.json()["enabled"] is True
    assert c.post(f"{API}/integrations/{uid}/disable", headers=admin()).json()["status"] == "disabled"
    assert c.delete(f"{API}/integrations/{uid}", headers=admin()).status_code == 204
    assert c.get(f"{API}/integrations/{uid}", headers=admin()).status_code == 404


def test_invalid_config_is_reported_and_blocks_connection(c):
    uid = mk_integration(c, config={"records": 999999})["integration_uid"]
    v = c.post(f"{API}/integrations/{uid}/validate", headers=admin()).json()
    assert v["valid"] is False and any("records" in e for e in v["errors"])
    t = c.post(f"{API}/integrations/{uid}/test", headers=admin()).json()
    assert t["ok"] is False


def test_changing_configuration_returns_to_draft_and_uses_optimistic_locking(c):
    i = mk_integration(c)
    uid = i["integration_uid"]
    connect(c, uid)
    stale = c.patch(
        f"{API}/integrations/{uid}", json={"expected_version": 99, "config": {"records": 5}}, headers=admin()
    )
    assert stale.status_code == 409
    ok = c.patch(
        f"{API}/integrations/{uid}", json={"expected_version": i["version"], "config": {"records": 5}}, headers=admin()
    )
    assert ok.status_code == 200 and ok.json()["status"] == "draft" and ok.json()["enabled"] is False


def test_credentials_are_references_only_and_never_returned(c, monkeypatch):
    monkeypatch.setenv("ATLAS_CRED_DEMO", "super-secret-value-123")
    r = c.post(
        f"{API}/integrations",
        json={"name": "Ref test", "provider": "sandbox_inventory", "credential_ref": "env:ATLAS_CRED_DEMO"},
        headers=admin(),
    )
    assert r.status_code == 201
    assert "super-secret-value-123" not in r.text
    assert r.json()["credential"] == {"reference": "env:ATLAS_CRED_DEMO", "status": "available"}
    for bad in ("plain-secret-value", "env:ATLAS_API_TOKENS", "env:PATH", "file:/etc/passwd"):
        rr = c.post(
            f"{API}/integrations",
            json={"name": "Bad ref", "provider": "sandbox_inventory", "credential_ref": bad},
            headers=admin(),
        )
        assert rr.status_code == 422, bad
        assert bad not in rr.text or "plain-secret" not in rr.text.replace(
            bad, ""
        )  # the rejected value is not echoed back as a stored secret


def test_authorisation_matrix(c):
    uid = mk_integration(c)["integration_uid"]
    assert c.get(f"{API}/integrations", headers=h("viewer")).status_code == 403  # integration.read starts at reviewer
    assert c.get(f"{API}/integrations", headers=h("reviewer")).status_code == 200
    for role in ("viewer", "editor", "reviewer"):
        assert (
            c.post(
                f"{API}/integrations", json={"name": "Denied one", "provider": "sandbox_inventory"}, headers=h(role)
            ).status_code
            == 403
        )
        assert c.post(f"{API}/integrations/{uid}/sync", json={}, headers=h(role)).status_code == 403
        assert c.delete(f"{API}/integrations/{uid}", headers=h(role)).status_code == 403
    assert c.get(f"{API}/integrations").status_code == 401


def test_list_filters_sorting_search_and_pagination(c):
    for n in ("Alpha feed", "Bravo feed", "Charlie feed"):
        mk_integration(c, name=n)
    p = c.get(f"{API}/integrations?limit=2&offset=0&sort=name", headers=admin()).json()
    assert [x["name"] for x in p["items"]] == ["Alpha feed", "Bravo feed"] and p["total"] == 3
    assert [x["name"] for x in c.get(f"{API}/integrations?q=charlie", headers=admin()).json()["items"]] == [
        "Charlie feed"
    ]
    assert c.get(f"{API}/integrations?status=connected", headers=admin()).json()["total"] == 0
    assert c.get(f"{API}/integrations?limit=1000", headers=admin()).status_code == 422
    assert c.get(f"{API}/integrations?sort=password", headers=admin()).status_code == 422


def test_duplicate_names_and_unknown_fields_are_rejected(c):
    mk_integration(c, name="Unique name")
    assert (
        c.post(
            f"{API}/integrations", json={"name": "Unique name", "provider": "sandbox_inventory"}, headers=admin()
        ).status_code
        == 409
    )
    assert (
        c.post(
            f"{API}/integrations",
            json={"name": "Extra field", "provider": "sandbox_inventory", "is_admin": True},
            headers=admin(),
        ).status_code
        == 422
    )


def test_sync_through_the_api_records_a_run_and_events_and_is_idempotent(c):
    uid = mk_integration(c, config={"records": 12})["integration_uid"]
    assert c.post(f"{API}/integrations/{uid}/sync", json={}, headers=admin()).status_code == 409  # draft: cannot sync
    enable(c, uid)
    r = c.post(f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=admin())
    assert r.status_code == 202
    runs = c.get(f"{API}/integrations/{uid}/runs", headers=admin()).json()
    run = runs["items"][0]
    assert run["status"] == "succeeded", run
    assert run["discovered"] == 12 and run["created"] == 12 and run["failed"] == 0
    again = c.post(f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=admin())
    assert again.status_code == 202
    second = c.get(f"{API}/integrations/{uid}/runs", headers=admin()).json()["items"][0]
    assert second["created"] == 0 and second["skipped"] == 12  # same idempotency keys: no duplicates
    detail = c.get(f"{API}/integrations/runs/{second['run_uid']}", headers=admin()).json()
    assert detail["errors"] == []
    assert c.get(f"{API}/integrations/{uid}/health", headers=admin()).json()["state"] == "healthy"


def test_overview_counts(c):
    mk_integration(c)
    o = c.get(f"{API}/integrations/overview", headers=admin()).json()
    assert o["total"] == 1


def test_tenant_isolation_of_integrations_and_runs(settings, engine):
    with client_for(settings, engine) as c:
        org2 = mk_org(c, "umbrella")
        mk_user(c, "bob", "admin", org_id=org2)
        mk_user(c, "alice", "admin")
        a, b = bearer(c, "alice"), bearer(c, "bob")
        uid = mk_integration(c, headers=a)["integration_uid"]
        enable(c, uid, a)
        assert c.post(f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=a).status_code == 202
        run_uid = c.get(f"{API}/integrations/{uid}/runs", headers=a).json()["items"][0]["run_uid"]
        assert c.get(f"{API}/integrations", headers=b).json()["total"] == 0
        for method, path in (
            ("get", uid),
            ("post", f"{uid}/test"),
            ("post", f"{uid}/sync"),
            ("get", f"{uid}/runs"),
            ("get", f"{uid}/health"),
            ("delete", uid),
            ("get", f"runs/{run_uid}"),
            ("post", f"runs/{run_uid}/cancel"),
        ):
            r = c.request(
                method.upper(), f"{API}/integrations/{path}", headers=b, json={} if method == "post" else None
            )
            assert r.status_code == 404, (method, path, r.status_code)
        # org2 can reuse the same name: names are unique per organisation, not globally
        assert (
            c.post(
                f"{API}/integrations", json={"name": "Sandbox feed", "provider": "sandbox_inventory"}, headers=b
            ).status_code
            == 201
        )
