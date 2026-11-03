"""Domain-data tenant isolation (migration 0022): suppliers, facilities, products, the network, imports and analysis runs
belong to one organisation. Organisation A must never read, change, link to or infer organisation B's records, and work
that runs after the request (background runs) must stay inside the caller's organisation. SYNTHETIC data only."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import current_actor
from app.db.session import get_db
from app.db.tenancy import bind_request, scope_session, tenant_session_factory
from app.models import Facility, Supplier, SupplyRelationship
from app.models.enums import Provenance, RelationshipType
from tests.graph_helpers import G
from tests.registry_helpers import API
from tests.security_helpers import bearer, make_app, mk_org, mk_user

BASE = {"country_or_region": "Germany", "category": "Electronics"}


@pytest.fixture
def two(settings, engine):
    """Org 1 with reviewer anna; org 2 with reviewer beatrice. Each creates suppliers and a facility."""
    with TestClient(make_app(settings, engine)) as c:
        o2 = mk_org(c, "beta")
        mk_user(c, "anna", "reviewer", org_id=1)
        mk_user(c, "beatrice", "reviewer", org_id=o2)
        h = {n: bearer(c, n) for n in ("anna", "beatrice")}
        made = {}
        for who, names in (("anna", ["A Metals", "A Plastics"]), ("beatrice", ["B Castings"])):
            sups = []
            for name in names:
                r = c.post(f"{API}/suppliers", json=BASE | {"name": name}, headers=h[who])
                assert r.status_code == 201, r.text
                sups.append(r.json())
            f = c.post(
                f"{API}/facilities",
                json={"name": f"{who} plant", "country_or_region": "Germany", "facility_type": "MANUFACTURING"},
                headers=h[who],
            )
            assert f.status_code == 201, f.text
            made[who] = {"suppliers": sups, "facility": f.json()}
        yield {"c": c, "h": h, "org2": o2, **made}


def _supplier_names(c, h) -> set[str]:
    r = c.get("/api/v1/suppliers", params={"limit": 100}, headers=h)
    assert r.status_code == 200, r.text
    return {s["name"] for s in r.json()["items"]}


def test_supplier_lists_are_per_organisation(two):
    c, h = two["c"], two["h"]
    assert _supplier_names(c, h["anna"]) == {"A Metals", "A Plastics"}
    assert _supplier_names(c, h["beatrice"]) == {"B Castings"}


def test_registry_search_and_overview_do_not_cross_organisations(two):
    c, h = two["c"], two["h"]
    found = c.get(f"{API}/search", params={"q": "Castings"}, headers=h["anna"])
    assert found.status_code == 200
    assert "B Castings" not in found.text
    own = c.get(f"{API}/search", params={"q": "Castings"}, headers=h["beatrice"])
    assert "B Castings" in own.text


def test_cross_organisation_read_is_identical_to_missing(two):
    c, h = two["c"], two["h"]
    b_id = two["beatrice"]["suppliers"][0]["id"]
    cross = c.get(f"{API}/suppliers/{b_id}", headers=h["anna"])
    missing = c.get(f"{API}/suppliers/999999", headers=h["anna"])
    assert cross.status_code == missing.status_code == 404
    # the caller's own input is echoed; apart from that the answers must be indistinguishable
    assert cross.json()["error"]["message"].replace(str(b_id), "ID") == missing.json()["error"]["message"].replace(
        "999999", "ID"
    )


def test_cross_organisation_update_is_404_and_changes_nothing(two):
    c, h = two["c"], two["h"]
    b = two["beatrice"]["suppliers"][0]
    r = c.patch(f"{API}/suppliers/{b['id']}", json={"expected_version": 1, "name": "Hijacked"}, headers=h["anna"])
    assert r.status_code == 404
    assert c.get(f"{API}/suppliers/{b['id']}", headers=h["beatrice"]).json()["name"] == "B Castings"


def test_cannot_reference_another_organisations_record(two):
    c, h = two["c"], two["h"]
    b_sup = two["beatrice"]["suppliers"][0]
    a_fac = two["anna"]["facility"]
    body = {
        "type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": b_sup["id"],
        "destination_type": "FACILITY", "destination_id": a_fac["id"],
        "status": "CONFIRMED", "justification": "synthetic test evidence",
    }  # fmt: skip
    r = c.post(f"{G}/relationships", json=body, headers=h["anna"])
    assert r.status_code in (404, 422), r.text
    r = c.post(f"{API}/suppliers", json=BASE | {"name": "Child", "parent_supplier_id": b_sup["id"]}, headers=h["anna"])
    assert r.status_code in (404, 422), r.text
    assert _supplier_names(c, h["anna"]) == {"A Metals", "A Plastics"}


def test_network_graph_is_per_organisation(two):
    c, h = two["c"], two["h"]
    for who in ("anna", "beatrice"):
        sup, fac = two[who]["suppliers"][0], two[who]["facility"]
        body = {
            "type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": sup["id"],
            "destination_type": "FACILITY", "destination_id": fac["id"],
            "status": "CONFIRMED", "justification": "synthetic test evidence",
        }  # fmt: skip
        assert c.post(f"{G}/relationships", json=body, headers=h[who]).status_code == 201
    a = c.get(f"{G}/relationships", headers=h["anna"])
    assert a.status_code == 200, a.text
    assert "B Castings" not in a.text and "beatrice plant" not in a.text


def test_business_keys_and_import_fingerprints_are_unique_per_organisation(two):
    """Both organisations register source namespace "erp-a" and commit the same CSV. With global keys the second source
    would conflict and the second import would be a replay, revealing that the other organisation holds the same data."""
    c, h = two["c"], two["h"]
    csv = "external_id,name,country_or_region,category\nS-1,Shared Name Industries,France,Metals\n"
    for who in ("anna", "beatrice"):
        src = c.post("/api/v1/ingestion/sources", json={"namespace": "erp-a", "name": "ERP A"}, headers=h[who])
        assert src.status_code == 201, src.text
        params = {"source_id": src.json()["id"], "entity_type": "SUPPLIER", "filename": "s.csv"}
        r = c.post(
            "/api/v1/ingestion/commit", params=params, content=csv, headers=h[who] | {"Content-Type": "text/csv"}
        )
        assert r.status_code == 201, r.text
        assert not r.json().get("replay") and r.json()["batch"]["created_count"] == 1
    assert "Shared Name Industries" in _supplier_names(c, h["anna"])
    assert "Shared Name Industries" in _supplier_names(c, h["beatrice"])
    batches_a = c.get("/api/v1/ingestion/batches", headers=h["anna"]).json()["total"]
    assert batches_a == 1


def test_quality_rule_overrides_are_per_organisation(two):
    c = two["c"]
    mk_user(c, "ada", "admin", org_id=1)
    mk_user(c, "bob", "admin", org_id=two["org2"])
    ha, hb = bearer(c, "ada"), bearer(c, "bob")

    def rules(headers):
        data = c.get("/api/v1/quality/rules", headers=headers).json()
        return {i["rule_id"]: i["enabled"] for i in (data["items"] if isinstance(data, dict) else data)}

    rule_id = next(r for r, on in rules(ha).items() if on)
    r = c.patch(f"/api/v1/quality/rules/{rule_id}", json={"enabled": False, "reason": "synthetic"}, headers=ha)
    assert r.status_code == 200, r.text
    assert rules(ha)[rule_id] is False
    assert rules(hb)[rule_id] is True
    r = c.patch(f"/api/v1/quality/rules/{rule_id}", json={"enabled": False, "reason": "synthetic"}, headers=hb)
    assert r.status_code == 200, r.text  # the same rule can be overridden independently


def test_background_risk_run_stays_inside_the_callers_organisation(two):
    c, h = two["c"], two["h"]
    r = c.post("/api/v1/risk/runs", json={"scope": "NETWORK", "reason": "synthetic"}, headers=h["beatrice"])
    assert r.status_code == 202, r.text
    run = c.get(f"/api/v1/risk/runs/{r.json()['id']}", headers=h["beatrice"]).json()
    assert run["status"] in ("SUCCEEDED", "COMPLETED", "PARTIAL"), run
    assessed = c.get("/api/v1/risk/suppliers", headers=h["beatrice"])
    assert assessed.status_code == 200, assessed.text
    assert "B Castings" in assessed.text and "A Metals" not in assessed.text
    assert c.get(f"/api/v1/risk/runs/{r.json()['id']}", headers=h["anna"]).status_code == 404


def test_pull_alert_rules_read_only_the_callers_domain_data(two):
    """Detectors that read domain results (here supplier risk) are available to every organisation and see only its own."""
    c, h = two["c"], two["h"]
    for who in ("anna", "beatrice"):
        r = c.post("/api/v1/risk/runs", json={"scope": "NETWORK", "reason": "synthetic"}, headers=h[who])
        assert r.status_code == 202, r.text
    rule = {"name": "Any scored supplier", "detector": "supplier_risk_score", "severity": "LOW",
            "params": {"operator": "gte", "threshold": 0}}  # fmt: skip
    seen = {}
    for who in ("anna", "beatrice"):
        r = c.post("/api/v1/alerting/rules", json=rule, headers=h[who])
        assert r.status_code == 201, r.text  # pull detectors are no longer limited to the default organisation
        ev = c.post("/api/v1/alerting/evaluate", json={}, headers=h[who])
        assert ev.status_code == 200, ev.text
        (result,) = ev.json()["rules"]
        seen[who] = result["evaluated"] + result["no_data"]  # suppliers the detector looked at, scored or not
    # The synthetic suppliers have no delivery evidence, so none is scored (and none alerts); what matters here is that
    # each organisation's detector looked at exactly its own suppliers: two for organisation 1, one for organisation 2.
    assert seen == {"anna": 2, "beatrice": 1}


def test_cached_summaries_are_never_shared_across_organisations(two):
    """Dashboard, network and analytics responses are cached; organisation A asks first, then B must get B's numbers."""
    c, h = two["c"], two["h"]
    a = c.get("/api/v1/dashboard/summary", headers=h["anna"]).json()
    b = c.get("/api/v1/dashboard/summary", headers=h["beatrice"]).json()
    assert (a["supplier_count"], b["supplier_count"]) == (2, 1)
    na = c.get("/api/v1/network/summary", headers=h["anna"])
    nb = c.get("/api/v1/network/summary", headers=h["beatrice"])
    assert na.status_code == nb.status_code == 200, (na.text, nb.text)
    assert "A Metals" in na.text and "A Metals" not in nb.text
    oa = c.get("/api/v1/analytics/overview", headers=h["anna"])
    ob = c.get("/api/v1/analytics/overview", headers=h["beatrice"])
    assert oa.status_code == ob.status_code == 200, (oa.text, ob.text)
    assert ob.json().get("meta", {}).get("cached") is not True


# ---- mechanism -------------------------------------------------------------------------------------------------------


def _supplier(code: str, org: int) -> Supplier:
    return Supplier(
        supplier_code=code, name=code, country_or_region="DE", category="X", status="ACTIVE",
        provenance=Provenance.SYNTHETIC_DEMO, organization_id=org,
    )  # fmt: skip


def test_scoped_session_filters_reads_and_stamps_inserts(engine, settings):
    with TestClient(make_app(settings, engine)) as c:
        o2 = mk_org(c, "gamma")
    with Session(engine) as s, s.begin():
        s.add_all([_supplier("SAME-CODE", 1), _supplier("SAME-CODE", o2)])  # per-organisation uniqueness
    with Session(engine) as s:
        scope_session(s, o2)
        assert [x.organization_id for x in s.scalars(select(Supplier))] == [o2]
        s.add(Facility(facility_code="F-NEW", name="F", country_or_region="DE", facility_type="MANUFACTURING",
                       status="ACTIVE", provenance=Provenance.SYNTHETIC_DEMO))  # fmt: skip
        s.commit()
        assert s.scalar(select(Facility.organization_id).where(Facility.facility_code == "F-NEW")) == o2


def test_a_row_cannot_point_at_another_organisations_row(engine, settings):
    with TestClient(make_app(settings, engine)) as c:
        o2 = mk_org(c, "delta")
    with Session(engine) as s, s.begin():
        theirs = _supplier("THEIRS", 1)
        s.add(theirs)
        fac = Facility(facility_code="F-MINE", name="F", country_or_region="DE", facility_type="MANUFACTURING",
                       status="ACTIVE", provenance=Provenance.SYNTHETIC_DEMO, organization_id=o2)  # fmt: skip
        s.add(fac)
        s.flush()
        ids = theirs.id, fac.id
    with Session(engine) as s:
        scope_session(s, o2)
        s.add(SupplyRelationship(relationship_code="R-X", relationship_type=RelationshipType.SUPPLIES, supplier_id=ids[0],
                                 target_facility_id=ids[1], provenance=Provenance.SYNTHETIC_DEMO))  # fmt: skip
        with pytest.raises(PermissionError, match="another organisation"):
            s.flush()


def test_tenant_session_factory_scopes_background_sessions(engine):
    from app.db.session import make_session_factory

    scoped = tenant_session_factory(make_session_factory(engine), 7)
    with scoped() as s:
        assert s.info["org_id"] == 7


def test_request_session_refuses_queries_before_authentication(engine):
    class State:  # a request state on which authentication has not stored an actor
        pass

    with Session(engine) as s:
        bind_request(s, State())
        with pytest.raises(RuntimeError, match="before the caller was authenticated"):
            s.scalars(select(Supplier)).all()


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def test_every_route_with_a_database_session_authenticates(settings, engine):
    """A request session is scoped by the authenticated actor, so a route that opens one without authenticating would
    fail closed (see the test above). This keeps the list of such routes explicit."""
    app = make_app(settings, engine)
    unauthenticated = sorted(
        r.path
        for r in app.routes
        if isinstance(r, APIRoute) and _depends_on(r.dependant, get_db) and not _depends_on(r.dependant, current_actor)
    )
    assert unauthenticated == [], unauthenticated
