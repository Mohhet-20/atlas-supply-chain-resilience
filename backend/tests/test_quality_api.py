from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.graph_helpers import node, ok
from tests.ingest_helpers import SUPPLIER_CSV
from tests.quality_helpers import TABLE, Q, db_supplier, evaluate, facility_with_coords, findings, h
from tests.registry_helpers import make_settings, mk


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE)
    with TestClient(create_app(s, engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    s = make_settings(
        settings,
        write_api_enabled=False,
        environment="production",
        api_tokens=TABLE,
        cors_allowed_origins=["https://x.example"],
    )
    with TestClient(create_app(s, engine)) as c:
        yield c


# ------------------------------------------------------------------------------------------------ empty states


def test_overview_empty_state(client):
    o = client.get(f"{Q}/overview").json()
    assert o["has_evaluation"] is False and o["scores"] is None and o["empty_message"]
    assert o["findings"]["open"] == 0 and "does not prove" in o["disclaimer"]


def test_unevaluated_record_reports_not_evaluated_not_zero(client):
    sid = db_supplier(client, "Fresh Supplier")
    s = {"id": sid}
    sc = client.get(f"{Q}/scores/SUPPLIER/{s['id']}").json()
    assert sc["state"] == "NOT_EVALUATED" and sc["score"] is None
    ent = client.get(f"{Q}/entities/SUPPLIER/{s['id']}").json()
    assert ent["open_findings"] == [] and ent["score"]["state"] == "NOT_EVALUATED"


def test_unknown_ids_and_types(client):
    assert client.get(f"{Q}/entities/SUPPLIER/9999").status_code == 404
    assert client.get(f"{Q}/entities/BOGUS/1").status_code == 422
    assert client.get(f"{Q}/findings/9999").status_code == 404
    assert client.get(f"{Q}/runs/9999").status_code == 404
    assert client.get(f"{Q}/batches/9999").status_code == 404
    assert client.get(f"{Q}/lineage/SUPPLIER/9999").status_code == 404
    assert client.get(f"{Q}/findings", params={"limit": 1000}).status_code == 422
    assert client.get(f"{Q}/findings", params={"status": "NOPE"}).status_code == 422


# ----------------------------------------------------------------------------------------------- runs and rules


def test_post_run_returns_202_and_completes(client):
    mk(client, "suppliers", name="Run Supplier")
    r = client.post(f"{Q}/runs", json={"scope": "NETWORK"})
    assert r.status_code == 202, r.text
    run = client.get(f"{Q}/runs/{r.json()['id']}").json()
    assert run["status"] in {"COMPLETED", "PARTIAL"} and run["examined"]["SUPPLIER"] == 1
    assert run["catalog_hash"] and run["formula_version"]
    lst = client.get(f"{Q}/runs").json()
    assert lst["total"] >= 1 and lst["items"][0]["id"] == run["id"]
    ov = client.get(f"{Q}/overview").json()
    assert ov["has_evaluation"] and ov["scores"]["overall"]["subjects"] >= 1


def test_run_scope_validation(client):
    assert client.post(f"{Q}/runs", json={"scope": "ENTITY"}).status_code == 422
    assert (
        client.post(f"{Q}/runs", json={"scope": "ENTITY", "entity_type": "SUPPLIER", "entity_id": 99999}).status_code
        == 404
    )
    assert client.post(f"{Q}/runs", json={"scope": "BATCH", "batch_id": 99999}).status_code == 404
    assert client.post(f"{Q}/runs", json={"scope": "RELATIONSHIPS", "refs": ["junk"]}).status_code == 422
    assert client.post(f"{Q}/runs", json={"scope": "NETWORK", "extra": 1}).status_code == 422


def test_rule_catalog_and_admin_only_toggle(secured):
    rules = secured.get(f"{Q}/rules", headers=h("viewer")).json()
    assert rules["total"] >= 30 and all("remediation" in r for r in rules["items"])
    body = {"enabled": False, "reason": "noisy for this tenant"}
    assert secured.patch(f"{Q}/rules/QR141", json=body, headers=h("reviewer")).status_code == 403
    assert secured.patch(f"{Q}/rules/QR999", json=body, headers=h("admin")).status_code == 404
    assert (
        secured.patch(f"{Q}/rules/QR141", json={"enabled": False, "reason": "x"}, headers=h("admin")).status_code == 422
    )
    c1 = secured.get(f"{Q}/config", headers=h("viewer")).json()["catalog_hash"]
    assert secured.patch(f"{Q}/rules/QR141", json=body, headers=h("admin")).status_code == 200
    c2 = secured.get(f"{Q}/config", headers=h("viewer")).json()["catalog_hash"]
    assert c1 != c2
    item = next(r for r in secured.get(f"{Q}/rules", headers=h("viewer")).json()["items"] if r["rule_id"] == "QR141")
    assert item["enabled"] is False and item["override"]["updated_by"] == "admin-user"


# -------------------------------------------------------------------------------------------- roles and auth


def test_roles_enforced_on_quality_endpoints(secured):
    assert secured.get(f"{Q}/overview").status_code == 401
    assert secured.get(f"{Q}/overview", headers=h("viewer")).status_code == 200
    assert secured.post(f"{Q}/runs", json={"scope": "NETWORK"}, headers=h("viewer")).status_code == 403
    assert secured.post(f"{Q}/runs", json={"scope": "NETWORK"}, headers=h("editor")).status_code == 403
    assert secured.post(f"{Q}/runs", json={"scope": "NETWORK"}, headers=h("reviewer")).status_code == 202


def test_finding_actions_by_role_and_notes(secured):
    mk(
        secured,
        "facilities",
        headers=h("editor"),
        name="Zero Site",
        facility_type="MANUFACTURING",
        latitude=0.0,
        longitude=0.0,
    )
    assert secured.post(f"{Q}/runs", json={"scope": "NETWORK"}, headers=h("reviewer")).status_code == 202
    f = secured.get(f"{Q}/findings", params={"rule_id": "QR115"}, headers=h("viewer")).json()["items"][0]
    p = f"{Q}/findings/{f['id']}"
    v = f["version"]
    assert (
        secured.post(
            f"{p}/transition", json={"action": "acknowledge", "expected_version": v}, headers=h("viewer")
        ).status_code
        == 403
    )
    r = secured.post(f"{p}/transition", json={"action": "acknowledge", "expected_version": v}, headers=h("editor"))
    assert r.status_code == 200 and r.json()["status"] == "ACKNOWLEDGED" and r.json()["version"] == v + 1
    stale = secured.post(
        f"{p}/transition", json={"action": "start_remediation", "expected_version": v}, headers=h("editor")
    )
    assert stale.status_code == 409
    assert (
        secured.post(
            f"{p}/transition",
            json={"action": "resolve", "note": "ok done", "expected_version": v + 1},
            headers=h("editor"),
        ).status_code
        == 403
    )
    assert (
        secured.post(
            f"{p}/transition",
            json={"action": "accept_exception", "note": "short", "expected_version": v + 1},
            headers=h("reviewer"),
        ).status_code
        == 422
    )
    assert (
        secured.post(
            f"{p}/transition", json={"action": "bogus", "expected_version": v + 1}, headers=h("reviewer")
        ).status_code
        == 422
    )
    done = secured.post(
        f"{p}/transition",
        json={"action": "accept_exception", "note": "Survey pending, tracked elsewhere.", "expected_version": v + 1},
        headers=h("reviewer"),
    )
    assert (
        done.status_code == 200
        and done.json()["status"] == "ACCEPTED_EXCEPTION"
        and done.json()["resolved_by"] == "reviewer-user"
    )
    bad = secured.post(
        f"{p}/transition", json={"action": "acknowledge", "expected_version": v + 2}, headers=h("editor")
    )
    assert bad.status_code == 409
    re = secured.post(
        f"{p}/transition",
        json={"action": "reopen", "note": "survey failed", "expected_version": v + 2},
        headers=h("reviewer"),
    )
    assert re.status_code == 200 and re.json()["status"] == "REOPENED" and re.json()["reopened_count"] == 1
    detail = secured.get(p, headers=h("viewer")).json()
    acts = [e["action"] for e in detail["events"]]
    assert acts[-3:] == ["ACKNOWLEDGE", "ACCEPT_EXCEPTION", "REOPEN"], acts
    assert detail["rule"]["rule_id"] == "QR115"
    ass = secured.post(
        f"{p}/assign", json={"assignee": "dana", "expected_version": detail["finding"]["version"]}, headers=h("editor")
    )
    assert ass.status_code == 403
    ass = secured.post(
        f"{p}/assign",
        json={"assignee": "dana", "expected_version": detail["finding"]["version"]},
        headers=h("reviewer"),
    )
    assert ass.status_code == 200 and ass.json()["assigned_to"] == "dana"


def test_writes_disabled_in_readonly_deployments(readonly):
    r = readonly.post(f"{Q}/runs", json={"scope": "NETWORK"}, headers=h("admin"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "write_api_disabled", r.text
    assert (
        readonly.patch(f"{Q}/rules/QR141", json={"enabled": False, "reason": "nope"}, headers=h("admin")).status_code
        == 403
    )
    assert readonly.get(f"{Q}/overview", headers=h("viewer")).status_code == 200


# ----------------------------------------------------------------------------------------- list / filter / page


def test_findings_filters_pagination_search_sorting(client):
    for i in range(4):
        facility_with_coords(client, f"Zero Site {i}", 0.0, 0.0)
    mk(client, "suppliers", name="  Padded Name ")
    evaluate(client)
    page1 = client.get(f"{Q}/findings", params={"limit": 2, "offset": 0}).json()
    page2 = client.get(f"{Q}/findings", params={"limit": 2, "offset": 2}).json()
    assert page1["total"] == page2["total"] >= 5
    assert not {f["id"] for f in page1["items"]} & {f["id"] for f in page2["items"]}
    sev = [f["severity"] for f in findings(client)]
    order = {"CRITICAL": 4, "ERROR": 3, "WARNING": 2, "INFO": 1}
    assert [order[s] for s in sev] == sorted([order[s] for s in sev], reverse=True)
    assert all(f["dimension"] == "validity" for f in findings(client, dimension="validity"))
    assert len(findings(client, entity_type="FACILITY", rule_id="QR115")) == 4
    assert findings(client, q="placeholder")[0]["entity_name"].startswith("Zero Site")
    assert all(f["status"] == "OPEN" for f in findings(client, status="OPEN_ANY"))
    assert findings(client, min_severity="ERROR") and all(
        f["severity"] in ("ERROR", "CRITICAL") for f in findings(client, min_severity="ERROR")
    )
    assert findings(client, q="%") == [] and findings(client, q="'; DROP TABLE x;--") == []


def test_entity_quality_detail_and_history_comparability(client):
    fac = facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    evaluate(client)
    d = client.get(f"{Q}/entities/FACILITY/{fac['id']}").json()
    assert d["entity"]["name"] == "Zero Site" and d["score"]["state"] in {"SCORED", "INSUFFICIENT_EVIDENCE"}
    assert any(f["rule_id"] == "QR115" for f in d["open_findings"])
    assert len(d["history"]) == 3 and d["history"][0]["comparable_with_latest"] is True
    assert d["score"]["breakdown"] is not None and d["score"]["formula_version"]


def test_relationship_quality_visible_on_entity_view(client):
    s, f = node(client, "suppliers", "Edge Supplier"), node(client, "facilities", "Edge Plant")
    edge = ok(client, "SUPPLIES", s, f)
    evaluate(client)
    rid = int(edge["ref"].split(":")[1])
    d = client.get(f"{Q}/entities/RELATIONSHIP/{rid}").json()
    assert d["entity"]["type"] == "RELATIONSHIP" and d["score"]["state"] != "NOT_EVALUATED"
    sup = client.get(f"{Q}/entities/SUPPLIER/{s['id']}").json()
    assert any(r["ref"] == edge["ref"] for r in sup["related_relationships"])


# ----------------------------------------------------------------------------------------------- remediation


def test_safe_fix_clears_placeholder_coordinates_only_with_confirmation(secured):
    fac = mk(
        secured,
        "facilities",
        headers=h("editor"),
        name="Zero Site",
        facility_type="MANUFACTURING",
        latitude=0.0,
        longitude=0.0,
    )
    secured.post(f"{Q}/runs", json={"scope": "NETWORK"}, headers=h("reviewer"))
    f = secured.get(f"{Q}/findings", params={"rule_id": "QR115"}, headers=h("viewer")).json()["items"][0]
    assert f["fix_action"] == "CLEAR_PLACEHOLDER_COORDINATES"
    path = f"{Q}/findings/{f['id']}/remediate"
    body = {"expected_version": f["version"], "confirm": True}
    assert secured.post(path, json={**body, "confirm": False}, headers=h("reviewer")).status_code == 422
    assert secured.post(path, json=body, headers=h("editor")).status_code == 403
    r = secured.post(path, json=body, headers=h("reviewer"))
    assert r.status_code == 200 and r.json()["status"] == "APPLIED", r.text
    from sqlalchemy.orm import Session

    from app.models.facility import Facility

    with Session(secured.app.state.engine) as db:
        row = db.get(Facility, fac["id"])
        assert row.latitude is None and row.longitude is None and row.location_status.value == "UNKNOWN"
    left = secured.get(f"{Q}/findings", params={"rule_id": "QR115", "status": "OPEN_ANY"}, headers=h("viewer")).json()[
        "items"
    ]
    assert left == []
    assert secured.post(path, json=body, headers=h("reviewer")).status_code in (409, 422)


def test_trim_fix_never_empties_required_fields(client):
    sid = db_supplier(client, "Trim Me Co", address="  1 Main St  ")
    s = {"id": sid}
    evaluate(client)
    f = findings(client, rule_id="QR111", entity_id=s["id"])
    assert f and f[0]["fix_action"] == "TRIM_TEXT"
    r = client.post(f"{Q}/findings/{f[0]['id']}/remediate", json={"expected_version": f[0]["version"], "confirm": True})
    assert r.status_code == 200, r.text
    from sqlalchemy.orm import Session

    from app.models.supplier import Supplier

    with Session(client.app.state.engine) as db:
        assert db.get(Supplier, s["id"]).address == "1 Main St"
    again = client.post(
        f"{Q}/findings/{f[0]['id']}/remediate", json={"expected_version": f[0]["version"] + 1, "confirm": True}
    )
    assert again.status_code in (409, 422)


def test_bulk_remediation_bounded_and_per_item(client):
    ids = []
    for i in range(3):
        db_supplier(client, f"Trim {i} Co", address=f" {i} Main St ")
    evaluate(client)
    fs = findings(client, rule_id="QR111")
    items = [{"finding_id": f["id"], "expected_version": f["version"]} for f in fs] + [
        {"finding_id": 99999, "expected_version": 1}
    ]
    ids = items
    r = client.post(f"{Q}/findings/bulk-remediate", json={"items": ids, "confirm": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == len(fs) and body["skipped"] == 1
    too_many = [{"finding_id": i + 1, "expected_version": 1} for i in range(26)]
    assert client.post(f"{Q}/findings/bulk-remediate", json={"items": too_many, "confirm": True}).status_code == 422
    assert client.post(f"{Q}/findings/bulk-remediate", json={"items": ids, "confirm": False}).status_code == 422


def test_no_fix_for_findings_without_safe_action(client):
    s = mk(client, "suppliers", name="Numeric Country Co", country_or_region="12345")
    evaluate(client)
    f = findings(client, rule_id="QR112", entity_id=s["id"])
    assert f and f[0]["fix_action"] is None
    r = client.post(f"{Q}/findings/{f[0]['id']}/remediate", json={"expected_version": f[0]["version"], "confirm": True})
    assert r.status_code == 422


# ------------------------------------------------------------------------------- ingestion and provenance


def _import(client, csv=SUPPLIER_CSV, ns="erp-q"):
    src = client.post(
        "/api/v1/ingestion/sources", json={"namespace": ns, "name": f"Source {ns}", "source_type": "CSV_FILE"}
    )
    assert src.status_code == 201, src.text
    r = client.post(
        "/api/v1/ingestion/commit",
        params={"source_id": src.json()["id"], "entity_type": "SUPPLIER", "filename": "f.csv"},
        content=csv.encode(),
        headers={"Content-Type": "text/csv"},
    )
    assert r.status_code in (200, 201, 422), r.text
    return src.json(), r.json()


def test_import_triggers_batch_evaluation_and_report_accounts_for_every_row(client):
    csv = "external_id,name,country_or_region,category,status\nS-1,Acme Parts,Germany,Electronics,ACTIVE\nS-2,,Mexico,Metal,ACTIVE\nS-1,Acme Parts,Germany,Electronics,ACTIVE\nS-4,Padded , Poland,Pack,ACTIVE\n"
    src, res = _import(client, csv)
    bid = res["batch"]["id"]
    rep = client.get(f"{Q}/batches/{bid}").json()
    assert sum(rep["counts"].values()) == rep["batch"]["total_rows"] == 4
    assert rep["counts"]["REJECTED"] >= 1 and rep["counts"]["DUPLICATE_SKIPPED"] >= 1
    assert rep["all_rows_accounted_for"] is True
    assert {r["classification"] for r in rep["rows"]["items"]} <= set(rep["counts"])
    only = client.get(f"{Q}/batches/{bid}", params={"classification": "REJECTED"}).json()
    assert only["rows"]["total"] == rep["counts"]["REJECTED"]
    assert client.get(f"{Q}/batches/{bid}", params={"classification": "WRONG"}).status_code == 422
    runs = client.get(f"{Q}/runs", params={"scope": "BATCH"}).json()
    assert runs["total"] == 1 and runs["items"][0]["trigger"] == "IMPORT"
    assert "Raw source rows are not stored" in rep["raw_payload"]
    srcs = client.get(f"{Q}/sources").json()["items"]
    me = next(s for s in srcs if s["source"]["namespace"] == "erp-q")
    assert me["indicators"]["rows_imported"] == 4 and "No single reliability number" in me["method"]


def test_batch_report_redacts_observed_values_for_viewers(secured):
    src = secured.post(
        "/api/v1/ingestion/sources",
        json={"namespace": "erp-r", "name": "R", "source_type": "CSV_FILE"},
        headers=h("editor"),
    ).json()
    csv = "external_id,name,country_or_region,category,status\nS-1,Acme Parts,Germany,Electronics,ACTIVE\nS-2,Bad Co,12345,Metal,ACTIVE\n"
    r = secured.post(
        "/api/v1/ingestion/commit",
        params={"source_id": src["id"], "entity_type": "SUPPLIER", "filename": "f.csv"},
        content=csv.encode(),
        headers={**h("editor"), "Content-Type": "text/csv"},
    )
    assert r.status_code in (200, 201), r.text
    bid = r.json()["batch"]["id"]
    seen = {}
    for role in ("viewer", "editor"):
        rep = secured.get(f"{Q}/batches/{bid}", headers=h(role)).json()
        seen[role] = [f for row in rep["rows"]["items"] for f in row["findings"]]
    assert (
        any(f["observed_value"] is None and f["observed_value_redacted"] for f in seen["viewer"]) or not seen["viewer"]
    )
    assert all(not f["observed_value_redacted"] for f in seen["editor"])


def test_lineage_layers_and_limitations(client):
    src, res = _import(client)
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.supplier import Supplier

    with Session(client.app.state.engine) as s:
        sid = s.scalars(select(Supplier.id).order_by(Supplier.id)).first()
    d = client.get(f"{Q}/lineage/SUPPLIER/{sid}").json()
    assert "ingestion_lineage" in d["layers_present"] and d["has_source_record"] is True
    assert d["ingestion"][0]["record_fingerprint"] and "not proof against" in " ".join(d["limitations"])
    manual = mk(client, "suppliers", name="Manual Co")
    m = client.get(f"{Q}/lineage/SUPPLIER/{manual['id']}").json()
    assert m["has_source_record"] is False and "audit_trail" in m["layers_present"]


def test_lineage_survives_merge(client):
    a, b = (
        mk(client, "suppliers", name="Acme Components GmbH"),
        mk(client, "suppliers", name="ACME Components G.m.b.H."),
    )
    from tests.registry_helpers import API, confirm_pair, merge_body

    cid = confirm_pair(client, "SUPPLIER", a, b)
    assert (
        client.post(
            f"{API}/merges",
            json=merge_body("SUPPLIER", a, b, field_decisions={"name": {"choice": "KEEP_SURVIVOR"}}, candidate_id=cid),
        ).status_code
        == 201
    )
    d = client.get(f"{Q}/lineage/SUPPLIER/{a['id']}").json()
    assert d["merges"] and d["merges"][0]["role"] == "survivor"
    gone = client.get(f"{Q}/lineage/SUPPLIER/{b['id']}").json()
    assert gone["merges"][0]["role"] == "merged_away" and gone["record"]["merged_into_id"] == a["id"]


def test_analytics_has_definitions_and_no_invented_values(client):
    empty = client.get(f"{Q}/analytics").json()
    assert empty["trend"] == [] and empty["time_to_resolve_hours"]["median"] is None
    assert "definitions" in empty and "missing_vs_zero" in empty["definitions"]
    facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    evaluate(client)
    a = client.get(f"{Q}/analytics").json()
    assert len(a["trend"]) == 2 and a["trend"][1]["comparable_with_previous"] is True
    assert a["stale_distribution"]["by_entity_type"]["FACILITY"]["total"] == 1
    assert a["relationship_quality_distribution"]["state"] in {"UNAVAILABLE", "SCORED"}


def test_audit_records_quality_actions(client):
    fac = facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    f = findings(client, rule_id="QR115")[0]
    client.post(f"{Q}/findings/{f['id']}/transition", json={"action": "acknowledge", "expected_version": f["version"]})
    client.post(f"{Q}/runs", json={"scope": "ENTITY", "entity_type": "FACILITY", "entity_id": fac["id"]})
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.registry import AuditEvent

    with Session(client.app.state.engine) as db:
        actions = set(db.scalars(select(AuditEvent.action)))
    assert {"quality.finding.acknowledge", "quality.run.queued"} <= actions
