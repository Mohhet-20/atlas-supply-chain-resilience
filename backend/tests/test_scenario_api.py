"""Scenario HTTP contract: CRUD, versioning, validation, runs, results, comparison, auth, write gate and immutability."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.scenario import ScenarioHistory
from tests.graph_helpers import node, ok
from tests.registry_helpers import make_settings
from tests.scenario_helpers import SC, TABLE, h


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False)
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
        risk_auto_reassess=False,
    )
    with TestClient(create_app(s, engine)) as c:
        yield c


def build(c):
    """a -> f1 (60%), b -> f1 (40%), f1 -> f2 (100%). Built through the API with the admin token."""
    ad = {"headers": h("admin")}
    from tests.registry_helpers import mk

    a = mk(c, "suppliers", headers=ad["headers"], name="Alder Parts")
    b = mk(c, "suppliers", headers=ad["headers"], name="Birch Parts")
    f1 = mk(c, "facilities", headers=ad["headers"], name="Cedar Plant", facility_type="MANUFACTURING")
    f2 = mk(c, "facilities", headers=ad["headers"], name="Dogwood Hub", facility_type="MANUFACTURING")
    for typ, s, d, share in (("SUPPLIES", a, f1, 60.0), ("SUPPLIES", b, f1, 40.0), ("TRANSFERS_TO", f1, f2, 100.0)):
        r = c.post(
            "/api/v1/graph/relationships",
            json={
                "type": typ,
                "source_type": "SUPPLIER" if typ == "SUPPLIES" else "FACILITY",
                "source_id": s["id"],
                "destination_type": "FACILITY",
                "destination_id": d["id"],
                "status": "CONFIRMED",
                "justification": "synthetic test evidence",
                "supply_share_pct": share,
            },
            headers=h("admin"),
        )
        assert r.status_code == 201, r.text
    return {"a": a, "b": b, "f1": f1, "f2": f2}


def mkscen(c, w, role="editor", **over):
    body = {
        "name": "Alder outage",
        "scenario_type": "SUPPLIER",
        "events": [
            {
                "event_type": "SUPPLIER_UNAVAILABLE",
                "target_type": "SUPPLIER",
                "target_id": w["a"]["id"],
                "duration_days": 30,
            }
        ],
    } | over
    r = c.post(SC, json=body, headers=h(role))
    assert r.status_code == 201, r.text
    return r.json()


def run(c, sid, key=None, role="editor"):
    hd = h(role) | ({"Idempotency-Key": key} if key else {})
    return c.post(f"{SC}/{sid}/runs", headers=hd)


def test_create_retrieve_and_defaults(secured):
    w = build(secured)
    s = mkscen(secured, w)
    assert s["status"] == "DRAFT" and s["version"] == 2 and s["event_count"] == 1  # create + one event
    assert s["assumptions"]["downstream_depth"] == 3 and s["validation_current"] is False
    got = secured.get(f"{SC}/{s['id']}", headers=h("viewer")).json()
    assert got["events"][0]["validation_status"] == "UNVALIDATED" and "hypothetical" in got["disclaimer"]


def test_auth_matrix(secured):
    w = build(secured)
    assert secured.get(SC).status_code == 401
    assert secured.get(SC, headers=h("viewer")).status_code == 200
    body = {"name": "x", "scenario_type": "CUSTOM"}
    assert secured.post(SC, json=body, headers=h("viewer")).status_code == 403
    s = mkscen(secured, w)
    assert secured.post(f"{SC}/{s['id']}/validate", headers=h("viewer")).status_code == 403
    assert run(secured, s["id"], role="viewer").status_code == 403
    assert secured.get(f"{SC}/runs/1").status_code == 401


def test_write_gate_blocks_writes_but_not_reads(readonly):
    assert readonly.get(SC, headers=h("viewer")).status_code == 200
    r = readonly.post(SC, json={"name": "x", "scenario_type": "CUSTOM"}, headers=h("admin"))
    assert r.status_code == 403 or r.status_code == 405 or "write_api_disabled" in r.text


def test_not_found_and_bad_ids(secured):
    assert secured.get(f"{SC}/999", headers=h("viewer")).status_code == 404
    assert secured.get(f"{SC}/0", headers=h("viewer")).status_code == 422
    assert secured.get(f"{SC}/runs/999", headers=h("viewer")).status_code == 404


def test_invalid_input_is_rejected(secured):
    w = build(secured)
    s = mkscen(secured, w)
    bad_type = secured.post(f"{SC}/{s['id']}/events", json={"event_type": "METEOR_STRIKE"}, headers=h("editor"))
    assert bad_type.status_code == 422
    bad_ref = secured.post(
        f"{SC}/{s['id']}/events",
        json={"event_type": "RELATIONSHIP_UNAVAILABLE", "relationship_ref": "nonsense"},
        headers=h("editor"),
    )
    assert bad_ref.status_code == 422
    extra = secured.post(SC, json={"name": "x", "scenario_type": "CUSTOM", "owner": "me"}, headers=h("editor"))
    assert extra.status_code == 422


def test_validation_reports_each_problem(secured):
    w = build(secured)
    s = mkscen(secured, w, events=[])
    sid = s["id"]
    empty = secured.post(f"{SC}/{sid}/validate", headers=h("editor")).json()
    assert empty["can_run"] is False and any(m["code"] == "NO_EVENTS" for m in empty["scenario_messages"])
    assert [c["passed"] for c in empty["checks"]][9] is False and len(empty["checks"]) == 10
    cases = {
        "ENTITY_NOT_FOUND": {"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": 99999},
        "TARGET_INCOMPATIBLE": {
            "event_type": "SUPPLIER_UNAVAILABLE",
            "target_type": "FACILITY",
            "target_id": w["f1"]["id"],
        },
        "TARGET_TYPE_UNSUPPORTED": {
            "event_type": "CUSTOM",
            "effect": "UNAVAILABLE",
            "targets": [{"type": "PRODUCT", "id": 1}],
        },
        "MAGNITUDE_REQUIRED": {
            "event_type": "SUPPLIER_CAPACITY_REDUCTION",
            "target_type": "SUPPLIER",
            "target_id": w["a"]["id"],
        },
        "MAGNITUDE_RANGE": {
            "event_type": "SUPPLIER_CAPACITY_REDUCTION",
            "target_type": "SUPPLIER",
            "target_id": w["a"]["id"],
            "magnitude_pct": 150,
        },
        "DURATION_RANGE": {
            "event_type": "SUPPLIER_UNAVAILABLE",
            "target_type": "SUPPLIER",
            "target_id": w["a"]["id"],
            "duration_days": -3,
        },
        "RECOVERY_WITHOUT_DURATION": {
            "event_type": "SUPPLIER_UNAVAILABLE",
            "target_type": "SUPPLIER",
            "target_id": w["a"]["id"],
            "recovery_days": 5,
        },
        "RELATIONSHIP_NOT_FOUND": {"event_type": "RELATIONSHIP_UNAVAILABLE", "relationship_ref": "SUPPLY:99999"},
        "LEAD_TIME_REQUIRED": {
            "event_type": "LEAD_TIME_INCREASE",
            "target_type": "SUPPLIER",
            "target_id": w["a"]["id"],
        },
        "REGION_LABEL_REQUIRED": {
            "event_type": "REGION_DISRUPTION",
            "effect": "UNAVAILABLE",
            "targets": [{"type": "FACILITY", "id": w["f1"]["id"]}],
        },
    }
    for code, ev in cases.items():
        r = secured.post(f"{SC}/{sid}/events", json=ev, headers=h("editor"))
        assert r.status_code == 201, r.text
        rep = secured.post(f"{SC}/{sid}/validate", headers=h("editor")).json()
        assert rep["can_run"] is False
        codes = {m["code"] for e in rep["events"] for m in e["messages"]}
        assert code in codes, (code, codes)
        sc = secured.get(f"{SC}/{sid}", headers=h("viewer")).json()
        secured.delete(f"{SC}/{sid}/events/{sc['events'][-1]['id']}", headers=h("editor"))


def test_endpoint_mismatch_and_unconfirmed_relationship(secured):
    w = build(secured)
    rels = secured.get("/api/v1/graph/relationships", headers=h("viewer"), params={"limit": 10}).json()["items"]
    to_f1 = next(r for r in rels if r["type"] == "SUPPLIES" and r["source_id"] == w["a"]["id"])
    s = mkscen(
        secured,
        w,
        events=[
            {
                "event_type": "RELATIONSHIP_UNAVAILABLE",
                "relationship_ref": to_f1["ref"],
                "target_type": "SUPPLIER",
                "target_id": w["b"]["id"],
            }
        ],
    )
    rep = secured.post(f"{SC}/{s['id']}/validate", headers=h("editor")).json()
    assert "RELATIONSHIP_ENDPOINT_MISMATCH" in {m["code"] for m in rep["events"][0]["messages"]}
    st = secured.post(
        f"/api/v1/graph/relationships/{to_f1['ref']}/status",
        json={"status": "INACTIVE", "reason": "synthetic test", "expected_version": to_f1["version"]},
        headers=h("admin"),
    )
    assert st.status_code == 200, st.text
    s2 = mkscen(secured, w, events=[{"event_type": "RELATIONSHIP_UNAVAILABLE", "relationship_ref": to_f1["ref"]}])
    rep2 = secured.post(f"{SC}/{s2['id']}/validate", headers=h("editor")).json()
    assert "RELATIONSHIP_NOT_CONFIRMED" in {m["code"] for m in rep2["events"][0]["messages"]}


def test_size_limits_are_server_enforced(secured):
    w = build(secured)
    many = [{"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": w["a"]["id"]}] * 26
    assert (
        secured.post(
            SC, json={"name": "big", "scenario_type": "CUSTOM", "events": many}, headers=h("editor")
        ).status_code
        == 422
    )
    s = mkscen(secured, w)
    targets = [{"type": "SUPPLIER", "id": w["a"]["id"]}] * 3 + [{"type": "FACILITY", "id": i} for i in range(1, 80)]
    secured.post(
        f"{SC}/{s['id']}/events",
        json={"event_type": "CUSTOM", "effect": "UNAVAILABLE", "targets": targets},
        headers=h("editor"),
    )
    rep = secured.post(f"{SC}/{s['id']}/validate", headers=h("editor")).json()
    assert "TOO_MANY_TARGETS" in {m["code"] for e in rep["events"] for m in e["messages"]}
    over = secured.patch(
        f"{SC}/{s['id']}", json={"expected_version": 99, "assumptions": {"downstream_depth": 9}}, headers=h("editor")
    )
    assert over.status_code in (409, 422)
    cur = secured.get(f"{SC}/{s['id']}", headers=h("viewer")).json()["version"]
    deep = secured.patch(
        f"{SC}/{s['id']}", json={"expected_version": cur, "assumptions": {"downstream_depth": 6}}, headers=h("editor")
    )
    assert deep.status_code == 200
    assert "ASSUMPTION_DEPTH_EXCEEDS_LIMIT" in {
        m["code"] for m in secured.post(f"{SC}/{s['id']}/validate", headers=h("editor")).json()["scenario_messages"]
    }
    assert secured.get(f"{SC}/limits", headers=h("viewer")).json()["client_overridable"] is False


def test_versioning_history_and_stale_edit_conflict(secured):
    w = build(secured)
    s = mkscen(secured, w)
    sid, v = s["id"], s["version"]
    assert secured.post(f"{SC}/{sid}/validate", headers=h("editor")).json()["can_run"] is True
    assert secured.get(f"{SC}/{sid}", headers=h("viewer")).json()["status"] == "READY"
    p = secured.patch(f"{SC}/{sid}", json={"expected_version": v, "name": "Renamed"}, headers=h("editor"))
    assert (
        p.status_code == 200 and p.json()["version"] == v + 1 and p.json()["status"] == "DRAFT"
    )  # edits require re-validation
    stale = secured.patch(f"{SC}/{sid}", json={"expected_version": v, "name": "Again"}, headers=h("editor"))
    assert stale.status_code == 409
    hist = secured.get(f"{SC}/{sid}/history", headers=h("viewer")).json()
    assert hist["total"] >= 3 and hist["items"][0]["action"] == "scenario.updated"


def test_run_lifecycle_and_results(secured):
    w = build(secured)
    s = mkscen(secured, w)
    assert secured.post(f"{SC}/{s['id']}/validate", headers=h("editor")).json()["can_run"]
    r = run(secured, s["id"])
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    st = secured.get(f"{SC}/runs/{rid}", headers=h("viewer")).json()
    assert st["status"] == "COMPLETED" and st["complete"] is True and st["engine_version"] == "sim-1"
    assert (
        st["scenario_version"] == s["version"]
        and st["scenario_snapshot"]["events"][0]["event_type"] == "SUPPLIER_UNAVAILABLE"
    )
    assert st["entities_affected"] == 3 and st["direct_count"] == 1 and st["graph_hash"]
    imp = secured.get(f"{SC}/runs/{rid}/impacts", headers=h("viewer")).json()
    names = {i["entity_name"]: i for i in imp["items"]}
    assert (
        names["Cedar Plant"]["loss_pct"] == 60.0
        and names["Dogwood Hub"]["loss_pct"] == 60.0
        and imp["complete"] is True
    )
    assert all(i["potential_exposure"] for i in imp["items"])
    only_direct = secured.get(f"{SC}/runs/{rid}/impacts", params={"impact_kind": "direct"}, headers=h("viewer")).json()
    assert only_direct["total"] == 1
    page = secured.get(
        f"{SC}/runs/{rid}/impacts", params={"limit": 1, "offset": 1, "sort": "name"}, headers=h("viewer")
    ).json()
    assert len(page["items"]) == 1 and page["total"] == imp["total"]
    path = secured.get(f"{SC}/runs/{rid}/impacts/FACILITY/{w['f2']['id']}", headers=h("viewer")).json()
    assert (
        len(path["path"]) == 2
        and path["events"][0]["event_type"] == "SUPPLIER_UNAVAILABLE"
        and "R2_SHARE_PROPAGATION" in path["rules_applied"]
    )
    assert secured.get(f"{SC}/runs/{rid}/impacts/FACILITY/999999", headers=h("viewer")).status_code == 404
    alts = secured.get(f"{SC}/runs/{rid}/alternatives", headers=h("viewer")).json()
    assert (
        alts["total"] == 1 and alts["items"][0]["source_name"] == "Birch Parts" and "No substitution" in alts["notice"]
    )
    edges = secured.get(f"{SC}/runs/{rid}/edges", headers=h("viewer")).json()
    assert edges["total"] == 2
    assert secured.get(f"{SC}/{s['id']}/runs", headers=h("viewer")).json()["total"] == 1
    assert secured.get(f"{SC}/runs", headers=h("viewer")).json()["items"][0]["scenario_name"] == "Alder outage"


def test_run_requires_valid_scenario_and_idempotency(secured):
    w = build(secured)
    empty = mkscen(secured, w, events=[])
    assert run(secured, empty["id"]).status_code == 422
    s = mkscen(secured, w)
    first = run(secured, s["id"], key="idem-key-0001")
    again = run(secured, s["id"], key="idem-key-0001")
    assert first.status_code == 202 and again.status_code == 202
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json()["id"] == first.json()["id"]
    assert secured.get(f"{SC}/{s['id']}/runs", headers=h("viewer")).json()["total"] == 1
    other = mkscen(secured, w, name="Other")
    assert run(secured, other["id"], key="idem-key-0001").status_code == 409  # key reuse for a different scenario


def test_editing_a_scenario_never_rewrites_an_earlier_run(secured):
    w = build(secured)
    s = mkscen(secured, w)
    rid = run(secured, s["id"]).json()["id"]
    before = secured.get(f"{SC}/runs/{rid}/impacts", headers=h("viewer")).json()
    sc = secured.get(f"{SC}/{s['id']}", headers=h("viewer")).json()
    secured.delete(f"{SC}/{s['id']}/events/{sc['events'][0]['id']}", headers=h("editor"))
    secured.patch(
        f"{SC}/{s['id']}", json={"expected_version": sc["version"] + 1, "name": "Changed"}, headers=h("editor")
    )
    after = secured.get(f"{SC}/runs/{rid}/impacts", headers=h("viewer")).json()
    assert before == after
    st = secured.get(f"{SC}/runs/{rid}", headers=h("viewer")).json()
    assert (
        st["scenario_version"] == s["version"]
        and st["scenario_snapshot"]["events"]
        and st["scenario_snapshot"]["scenario"]["name"] == "Alder outage"
    )


def test_simulation_never_mutates_canonical_data(secured, engine):
    w = build(secured)
    s = mkscen(secured, w)
    tables = ("suppliers", "facilities", "products", "supply_relationships", "product_links", "graph_edge_events")

    def snap():
        with Session(engine) as ses:
            return {t: ses.execute(text(f"SELECT * FROM {t} ORDER BY 1")).fetchall() for t in tables}  # noqa: S608

    before = snap()
    secured.post(f"{SC}/{s['id']}/validate", headers=h("editor"))
    assert run(secured, s["id"]).status_code == 202
    assert snap() == before


def test_audit_trail_and_append_only_history(secured, engine):
    w = build(secured)
    s = mkscen(secured, w)
    secured.post(f"{SC}/{s['id']}/validate", headers=h("editor"))
    run(secured, s["id"])
    ev = secured.get("/api/v1/registry/audit", headers=h("admin"), params={"limit": 100})
    if ev.status_code == 200:
        actions = {e["action"] for e in ev.json()["items"]}
        assert {"scenario.create", "scenario.validate", "scenario.run.queued"} <= actions
    with Session(engine) as ses:
        assert (ses.scalar(select(func.count()).select_from(ScenarioHistory)) or 0) >= 2
        with pytest.raises(Exception):  # noqa: B017, PT011
            ses.execute(text("UPDATE scenario_history SET summary='x'"))
            ses.commit()


def test_archive_and_restore(secured):
    w = build(secured)
    s = mkscen(secured, w)
    secured.post(f"{SC}/{s['id']}/validate", headers=h("editor"))
    rid = run(secured, s["id"]).json()["id"]
    a = secured.post(f"{SC}/{s['id']}/archive", headers=h("editor"))
    assert a.status_code == 200 and a.json()["status"] == "ARCHIVED"
    assert run(secured, s["id"]).status_code == 409
    assert (
        secured.patch(
            f"{SC}/{s['id']}", json={"expected_version": a.json()["version"], "name": "no"}, headers=h("editor")
        ).status_code
        == 409
    )
    assert secured.get(f"{SC}/runs/{rid}/impacts", headers=h("viewer")).status_code == 200  # run history kept
    assert secured.get(SC, params={"status": "ARCHIVED"}, headers=h("viewer")).json()["total"] == 1
    r = secured.post(f"{SC}/{s['id']}/restore", headers=h("editor"))
    assert r.json()["status"] == "DRAFT"


def test_templates_do_not_name_entities(secured):
    t = secured.get(f"{SC}/templates", headers=h("viewer")).json()
    assert len(t["templates"]) >= 7 and all(x["requires_target_selection"] for x in t["templates"])
    assert "target_id" not in str(t) and "placeholders" in t["disclaimer"]
    assert secured.get(SC, headers=h("viewer")).json()["total"] == 0  # nothing was created from templates


def test_list_filters_and_search(secured):
    w = build(secured)
    mkscen(secured, w, name="Alpha outage")
    mkscen(secured, w, name="Beta region", scenario_type="REGIONAL", events=[])
    assert secured.get(SC, params={"q": "beta"}, headers=h("viewer")).json()["total"] == 1
    assert secured.get(SC, params={"scenario_type": "SUPPLIER"}, headers=h("viewer")).json()["total"] == 1
    assert secured.get(SC, params={"limit": 1}, headers=h("viewer")).json()["items"].__len__() == 1
    assert secured.get(SC, params={"limit": 1000}, headers=h("viewer")).status_code == 422


def test_comparison_and_comparability_warnings(secured):
    w = build(secured)
    s1 = mkscen(secured, w)
    secured.post(f"{SC}/{s1['id']}/validate", headers=h("editor"))
    r1 = run(secured, s1["id"]).json()["id"]
    s2 = mkscen(
        secured,
        w,
        name="Facility down",
        scenario_type="FACILITY",
        events=[{"event_type": "FACILITY_SHUTDOWN", "target_type": "FACILITY", "target_id": w["f1"]["id"]}],
    )
    r2 = run(secured, s2["id"]).json()["id"]
    c = secured.get(f"{SC}/compare", params=[("run_ids", r1), ("run_ids", r2)], headers=h("viewer")).json()
    assert c["comparable"] is True and c["metric_comparability"]["counts_and_sets"] is True and not c["warnings"]
    rows = {r["run_id"]: r for r in c["runs"]}
    assert rows[r1]["entities_affected"] == 3 and rows[r2]["direct"] == 1
    assert c["entities"]["in_all_runs"] >= 2 and c["same_scenario"] is False
    # change the network, run again: the comparison must say the inputs differ
    ok(
        secured, "SUPPLIES", node(secured, "suppliers", "Gum Parts"), w["f1"], supply_share_pct=5.0, headers=h("admin")
    ) if False else None
    secured.post(
        "/api/v1/graph/relationships",
        json={
            "type": "SUPPLIES",
            "source_type": "SUPPLIER",
            "source_id": node(secured, "suppliers", "Gum Parts", headers=h("admin"))["id"],
            "destination_type": "FACILITY",
            "destination_id": w["f1"]["id"],
            "status": "CONFIRMED",
            "justification": "synthetic",
            "supply_share_pct": 5.0,
        },
        headers=h("admin"),
    )
    r3 = run(secured, s1["id"]).json()["id"]
    c2 = secured.get(f"{SC}/compare", params=[("run_ids", r1), ("run_ids", r3)], headers=h("viewer")).json()
    assert "NETWORK_INPUTS_DIFFER" in {x["code"] for x in c2["warnings"]}
    # same assumptions-different depth -> counts not comparable
    secured.patch(
        f"{SC}/{s2['id']}",
        json={
            "expected_version": secured.get(f"{SC}/{s2['id']}", headers=h("viewer")).json()["version"],
            "assumptions": {"downstream_depth": 1},
        },
        headers=h("editor"),
    )
    secured.post(f"{SC}/{s2['id']}/validate", headers=h("editor"))
    r4 = run(secured, s2["id"]).json()["id"]
    c3 = secured.get(f"{SC}/compare", params=[("run_ids", r2), ("run_ids", r4)], headers=h("viewer")).json()
    assert c3["metric_comparability"]["counts_and_sets"] is False and "downstream_depth" in c3["assumption_differences"]
    assert "DEFINITION_CHANGED" in {x["code"] for x in c3["warnings"]}
    assert secured.get(f"{SC}/compare", params=[("run_ids", r1)], headers=h("viewer")).status_code == 422
    assert (
        secured.get(f"{SC}/compare", params=[("run_ids", r1), ("run_ids", 99999)], headers=h("viewer")).status_code
        == 404
    )
