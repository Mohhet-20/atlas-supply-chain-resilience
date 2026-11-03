"""Routing API: contract, roles, write gate, idempotency, immutability, scenario comparison, replay and reliability. Synthetic data only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.enums import SimStatus
from app.models.transport import RoutingRun, TransportLane
from app.routing import data as rdata
from app.routing import runs as rruns
from app.routing.config import Limits, RouteRequest
from tests.graph_helpers import node
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings
from tests.routing_helpers import NOW, R, diamond, mk_lane, mk_net

SC = "/api/v1/scenarios"


@pytest.fixture
def secured(settings, engine):
    with TestClient(create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False), engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    with TestClient(
        create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, write_api_enabled=False), engine)
    ) as c:
        yield c


def world(client, secured):
    """Four facilities A-D and the diamond lanes (A->D has ROAD A-B-D, AIR A-C-D, SEA A-D, RAIL B->C)."""
    f = {k: node(client, "facilities", f"Hub {k}")["id"] for k in "ABCD"}
    spec = [("A", "B", "ROAD", 10, 100, 0.95), ("B", "D", "ROAD", 12, 120, 0.95), ("A", "C", "AIR", 3, 900, 0.99), ("C", "D", "AIR", 3, 800, 0.99), ("A", "D", "SEA", 70, 150, 0.80), ("B", "C", "RAIL", 5, 60, 0.90)]  # fmt: skip
    uids = []
    for o, d, mode, hrs, cost, rel in spec:
        r = secured.post(f"{R}/lanes", json={"origin_facility_id": f[o], "destination_facility_id": f[d], "mode": mode, "duration_hours": hrs, "duration_quality": "VERIFIED", "cost_amount": cost, "currency": "USD", "cost_quality": "VERIFIED", "capacity_amount": 20, "capacity_unit": "t", "reliability_prior": rel, "provenance": "SYNTHETIC_DEMO", "observed_at": NOW.isoformat()}, headers=h("editor"))  # fmt: skip
        assert r.status_code == 201, r.text
        uids.append(r.json()["lane_uid"])
    return f, uids


def optimize(c, f, role="editor", key=None, **over):
    body = {
        "origin_facility_id": f["A"],
        "destination_facility_id": f["D"],
        "objective": "FASTEST",
        "alternatives": 4,
    } | over
    hd = h(role) | ({"Idempotency-Key": key} if key else {})
    return c.post(f"{R}/optimize", json=body, headers=hd)


def finished(c, resp):
    assert resp.status_code == 202, resp.text
    return c.get(f"{R}/runs/{resp.json()['run_uid']}", headers=h("viewer")).json()


def test_health_discloses_capabilities_limits_and_exclusions(client):
    j = client.get(f"{R}/health").json()
    assert j["algorithm_version"] == "rt-1" and "ROAD" in j["modes"] and j["limits"]["max_hops"] == 8
    assert any("booking" in x for x in j["not_supported"])
    assert not any("organisation" in x for x in j["not_supported"])  # data is organisation-scoped
    assert "tenancy" in j["statements"] and "cost" in j["statements"]
    assert {o["name"] for o in client.get(f"{R}/objectives").json()["objectives"]} >= {"BALANCED", "CUSTOM"}
    assert "UNVERIFIABLE" in client.get(f"{R}/constraints").json()["states"]


def test_lane_validation_rejects_bad_input(client, secured):
    f, _ = world(client, secured)
    base = {"origin_facility_id": f["A"], "destination_facility_id": f["B"], "mode": "ROAD"}
    bad = [
        {"destination_facility_id": f["A"]}, {"cost_amount": 5}, {"currency": "USD"}, {"duration_hours": -1}, {"cost_amount": -5, "currency": "USD"},
        {"capacity_amount": 5}, {"capacity_amount": 0, "capacity_unit": "t"}, {"mode": "TELEPORT"}, {"reliability_prior": 1.2}, {"currency": "usd", "cost_amount": 1},
        {"organisation_id": 3}, {"duration_quality": "VERIFIED"}, {"observed_at": "2026-01-01T00:00:00"}, {"restrictions": ["bad tag"]},
        {"effective_from": "2026-02-01T00:00:00Z", "effective_to": "2026-01-01T00:00:00Z"},
    ]  # fmt: skip
    for extra in bad:
        r = secured.post(f"{R}/lanes", json=base | extra, headers=h("editor"))
        assert r.status_code in (422, 400), (extra, r.text)
    assert (
        secured.post(f"{R}/lanes", json=base | {"destination_facility_id": 99999}, headers=h("editor")).status_code
        == 404
    )
    assert secured.post(f"{R}/lanes", json=base | {"relationship_id": 99999}, headers=h("editor")).status_code == 404
    assert secured.post(f"{R}/lanes", json=base, headers=h("editor")).status_code == 201  # figures may all be unknown


def test_database_constraints_back_up_the_api(engine, client, secured):
    f, _ = world(client, secured)
    with Session(engine) as s:
        s.add(
            TransportLane(
                lane_uid="LN-BAD1",
                origin_facility_id=f["A"],
                destination_facility_id=f["A"],
                mode="ROAD",
                provenance="USER_PROVIDED",
                created_by="t",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(
            TransportLane(
                lane_uid="LN-BAD2",
                origin_facility_id=f["A"],
                destination_facility_id=f["B"],
                mode="ROAD",
                cost_amount=5,
                provenance="USER_PROVIDED",
                created_by="t",
            )
        )
        with pytest.raises(IntegrityError):
            s.commit()


def test_lane_rows_are_immutable_and_status_transitions_are_controlled(engine, client, secured):
    f, uids = world(client, secured)
    with Session(engine) as s:
        row = s.query(TransportLane).filter_by(lane_uid=uids[0]).one()
        row.cost_amount = 1.0
        with pytest.raises(ValueError, match="immutable"):
            s.flush()
        s.rollback()
        s.delete(s.query(TransportLane).filter_by(lane_uid=uids[0]).one())
        with pytest.raises(ValueError, match="cannot be deleted"):
            s.flush()
    ed = h("editor")
    assert (
        secured.post(f"{R}/lanes/{uids[0]}/status", json={"to": "SUSPENDED"}, headers=ed).json()["status"]
        == "SUSPENDED"
    )
    assert secured.post(f"{R}/lanes/{uids[0]}/status", json={"to": "ACTIVE"}, headers=ed).json()["status"] == "ACTIVE"
    assert (
        secured.post(f"{R}/lanes/{uids[0]}/status", json={"to": "RETIRED"}, headers=ed).json()["retired_at"] is not None
    )
    assert secured.post(f"{R}/lanes/{uids[0]}/status", json={"to": "ACTIVE"}, headers=ed).status_code == 409
    assert secured.post(f"{R}/lanes/{uids[0]}/status", json={"to": "NOPE"}, headers=ed).status_code == 422


def test_auth_matrix_write_gate_and_cost_redaction(client, secured, readonly):
    f, uids = world(client, secured)
    assert secured.get(f"{R}/lanes").status_code == 401
    lane = {"origin_facility_id": f["A"], "destination_facility_id": f["B"], "mode": "RAIL"}
    assert secured.post(f"{R}/lanes", json=lane, headers=h("viewer")).status_code == 403
    assert (
        secured.post(
            f"{R}/optimize", json={"origin_facility_id": 1, "destination_facility_id": 2}, headers=h("viewer")
        ).status_code
        == 403
    )
    assert secured.post(f"{R}/feasibility", json={"lane_uids": uids[:1]}, headers=h("viewer")).status_code == 403
    v = secured.get(f"{R}/lanes/{uids[0]}", headers=h("viewer")).json()
    e = secured.get(f"{R}/lanes/{uids[0]}", headers=h("editor")).json()
    assert v["cost_amount"] is None and v["cost_redacted"] is True and e["cost_amount"] == 100
    net_v = secured.get(f"{R}/network", headers=h("viewer")).json()
    assert all(x["cost"] is None for x in net_v["lanes"]) and any(
        x["cost"] for x in secured.get(f"{R}/network", headers=h("editor")).json()["lanes"]
    )
    run = finished(secured, optimize(secured, f))
    assert secured.get(f"{R}/runs/{run['run_uid']}/cost-breakdown", headers=h("viewer")).status_code == 403
    cb = secured.get(f"{R}/runs/{run['run_uid']}/cost-breakdown", headers=h("editor")).json()
    assert cb["total"]["total"] == 1700 and [x["cost"] for x in cb["lines"]] == [900, 800]
    r = readonly.post(f"{R}/lanes", json=lane, headers=h("admin"))
    assert (
        r.status_code == 403 and r.json()["error"]["code"] == "write_api_disabled"
        if "error" in r.json()
        else r.status_code == 403
    )
    assert readonly.get(f"{R}/lanes", headers=h("viewer")).status_code == 200


def test_optimize_end_to_end_with_traceability(client, secured):
    f, uids = world(client, secured)
    run = finished(secured, optimize(secured, f, objective="LOWEST_COST"))
    assert run["status"] == "COMPLETED" and run["result_status"] == "feasible" and run["kind"] == "OPTIMIZE"
    assert run["routes"][0]["metrics"]["lane_uids"] == [uids[4]] and run["routes"][0]["metrics"]["cost"]["total"] == 150
    net = secured.get(f"{R}/network", headers=h("viewer")).json()
    assert run["network_hash"] == net["network_hash"] and run["reproducibility"]["network_hash"] == net["network_hash"]
    assert run["reproducibility"]["algorithm_version"] == "rt-1" and run["request"]["objective"] == "LOWEST_COST"
    assert run["provenance"]["lane_provenance"] == {"SYNTHETIC_DEMO": 6} and any(
        "synthetic" in w for w in run["warnings"]
    )
    assert (
        run["summary"]["search"]["optimality"] == "EXACT_WITHIN_HOP_LIMIT"
        and run["disruptions"] is None
        and run["comparison"] is None
    )
    assert any("tenancy" in s.lower() or "organisation" in s for s in run["statements"])
    one = secured.get(f"{R}/runs/{run['run_uid']}/routes/2", headers=h("viewer")).json()["route"]
    assert one["rank"] == 2 and "explanation" in one and one["constraints"] == []
    assert secured.get(f"{R}/runs/{run['run_uid']}/routes/10", headers=h("viewer")).status_code == 404
    assert secured.get(f"{R}/runs/{run['run_uid']}/comparison", headers=h("viewer")).status_code == 422
    assert secured.get(f"{R}/runs/{run['run_uid']}/routes", headers=h("viewer")).json()["pareto_front"]
    hist = secured.get(f"{R}/runs?origin={f['A']}&destination={f['D']}&kind=OPTIMIZE", headers=h("viewer")).json()
    assert hist["total"] == 1 and hist["items"][0]["best_route"] == [uids[4]]


def test_idempotency_and_replay_header(client, secured):
    f, _ = world(client, secured)
    a = optimize(secured, f, key="idem-key-0001")
    b = optimize(secured, f, key="idem-key-0001")
    assert (
        a.status_code == b.status_code == 202
        and a.json()["run_uid"] == b.json()["run_uid"]
        and b.headers["Idempotent-Replayed"] == "true"
    )
    assert optimize(secured, f, key="idem-key-0001", objective="LOWEST_COST").status_code == 409
    assert secured.get(f"{R}/runs", headers=h("viewer")).json()["total"] == 1


def test_validation_errors_and_bounds(client, secured):
    f, _ = world(client, secured)
    for over in ({"alternatives": 11}, {"max_hops": 9}, {"objective": "BEST"}, {"organisation_id": 1}, {"weights": {"cost": 1}}, {"destination_facility_id": f["A"]}, {"constraints": {"max_cost": 5}}, {"constraints": {"min_capacity": 1}}, {"constraints": {"allowed_modes": ["BOAT"]}}, {"as_of": "2026-01-01T00:00:00"}):  # fmt: skip
        r = optimize(secured, f, **over)
        assert r.status_code == 422, (over, r.text)
    assert optimize(secured, f, origin_facility_id=99999).status_code == 404
    assert optimize(secured, f, lane_closures=["LN-DOESNOTEXIST"]).status_code == 422
    assert optimize(secured, f, scenario_id=99999).status_code == 404
    assert secured.get(f"{R}/runs/NOPE-NOPE", headers=h("viewer")).status_code == 404
    assert secured.get(f"{R}/runs/bad%20uid!", headers=h("viewer")).status_code == 422
    big = {"lanes": [{"origin_facility_id": f["A"], "destination_facility_id": f["B"], "mode": "ROAD"}] * 201}
    assert secured.post(f"{R}/lanes/bulk", json=big, headers=h("editor")).status_code == 422


def test_bulk_lane_import_is_atomic(client, secured):
    f, _ = world(client, secured)
    ok = {"origin_facility_id": f["B"], "destination_facility_id": f["C"], "mode": "ROAD", "duration_hours": 4}
    bad = {"origin_facility_id": f["B"], "destination_facility_id": 99999, "mode": "ROAD"}
    before = secured.get(f"{R}/lanes?limit=200", headers=h("viewer")).json()["total"]
    assert secured.post(f"{R}/lanes/bulk", json={"lanes": [ok, bad]}, headers=h("editor")).status_code == 404
    assert secured.get(f"{R}/lanes?limit=200", headers=h("viewer")).json()["total"] == before
    r = secured.post(f"{R}/lanes/bulk", json={"lanes": [ok, ok]}, headers=h("editor"))
    assert r.status_code == 201 and r.json()["created"] == 2
    dup = secured.get(f"{R}/network/validation", headers=h("viewer")).json()
    assert "DUPLICATE_LANE" in dup["by_code"]


def test_no_feasible_route_is_structured_and_never_relaxed(client, secured):
    f, _ = world(client, secured)
    run = finished(secured, optimize(secured, f, constraints={"max_transit_hours": 2}))
    assert run["result_status"] == "no_feasible_route" and run["routes"] == []
    inf = run["rejections"]["infeasibility"]
    assert inf["network_connected"] is True and "No constraint was relaxed" in inf["relaxation"] and inf["reasons"]
    assert run["rejections"]["cut_before_completion"]["max_transit_hours"] >= 1


def test_feasibility_endpoint_validates_a_proposed_route(client, secured):
    f, uids = world(client, secured)
    ok = secured.post(
        f"{R}/feasibility",
        json={
            "lane_uids": [uids[0], uids[1]],
            "constraints": {"max_transit_hours": 30, "max_cost": 500, "currency": "USD"},
        },
        headers=h("editor"),
    )
    j = ok.json()
    assert (
        ok.status_code == 200
        and j["kind"] == "FEASIBILITY"
        and j["routes"][0]["classification"] == "FEASIBLE"
        and j["status"] == "COMPLETED"
    )
    bad = secured.post(
        f"{R}/feasibility",
        json={"lane_uids": [uids[0], uids[1]], "constraints": {"max_transit_hours": 20}},
        headers=h("editor"),
    ).json()
    assert (
        bad["routes"][0]["classification"] == "INFEASIBLE" and bad["routes"][0]["constraints"][0]["state"] == "VIOLATED"
    )
    gap = secured.post(f"{R}/feasibility", json={"lane_uids": [uids[0], uids[3]]}, headers=h("editor")).json()
    assert gap["routes"][0]["valid_structure"] is False
    assert secured.post(f"{R}/feasibility", json={"lane_uids": ["LN-NOPE"]}, headers=h("editor")).status_code == 422


def _scenario(c, events, ready=True):
    r = c.post(SC, json={"name": "Hub C closure", "scenario_type": "CUSTOM", "events": events}, headers=h("editor"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    if ready:
        assert c.post(f"{SC}/{sid}/validate", headers=h("editor")).json()["can_run"] is True
    return sid


def test_scenario_comparison_leaves_the_registered_network_untouched(client, secured, engine):
    f, uids = world(client, secured)
    before = secured.get(f"{R}/network", headers=h("viewer")).json()["network_hash"]
    sid = _scenario(
        secured,
        [{"event_type": "FACILITY_SHUTDOWN", "target_type": "FACILITY", "target_id": f["C"], "duration_days": 30}],
    )
    run = finished(secured, optimize(secured, f, scenario_id=sid, objective="FASTEST"))
    assert run["status"] == "COMPLETED" and run["scenario_id"] == sid and run["scenario_hash"]
    assert run["baseline_routes"][0]["metrics"]["lane_uids"] == [uids[2], uids[3]]
    assert run["routes"][0]["metrics"]["lane_uids"] == [uids[0], uids[1]]
    cmp = secured.get(f"{R}/runs/{run['run_uid']}/comparison", headers=h("viewer")).json()
    assert cmp["comparison"]["best_route_changed"] is True and cmp["comparison"]["best_route_deltas"]["duration"] == 16
    states = {tuple(x["lane_uids"]): x["state"] for x in cmp["comparison"]["baseline_routes_in_scenario"]}
    assert states[(uids[2], uids[3])] == "UNAVAILABLE" and cmp["disruptions"]["baseline_unchanged"] is True
    assert (
        cmp["disruptions"]["scenario"]["scenario_id"] == sid
        and cmp["disruptions"]["applied"][0]["kind"] == "CLOSE_NODE"
    )
    assert any("timing windows" in w for w in run["warnings"])
    assert secured.get(f"{R}/network", headers=h("viewer")).json()["network_hash"] == before == run["network_hash"]
    with Session(engine) as s:
        assert {r.status for r in s.query(TransportLane)} == {"ACTIVE"}
    # a request-level closure works with no scenario at all, and an unmappable event is reported, not ignored
    r2 = finished(secured, optimize(secured, f, lane_closures=[uids[4]], objective="LOWEST_COST"))
    assert r2["baseline_routes"][0]["metrics"]["lane_uids"] == [uids[4]] and r2["routes"][0]["metrics"][
        "lane_uids"
    ] != [uids[4]]
    sup = node(client, "suppliers", "Birch Parts")
    sid2 = _scenario(
        secured,
        [{"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": sup["id"], "duration_days": 5}],
    )
    r3 = finished(secured, optimize(secured, f, scenario_id=sid2))
    assert r3["disruptions"]["not_applied"][0]["event_type"] == "SUPPLIER_UNAVAILABLE" and any(
        "could not be mapped" in w for w in r3["warnings"]
    )


def test_draft_scenarios_cannot_be_used(client, secured):
    f, _ = world(client, secured)
    sid = _scenario(
        secured,
        [{"event_type": "FACILITY_SHUTDOWN", "target_type": "FACILITY", "target_id": f["C"], "duration_days": 30}],
        ready=False,
    )
    assert optimize(secured, f, scenario_id=sid).status_code == 409


def test_lane_adjustments_change_the_copy_only(client, secured):
    f, uids = world(client, secured)
    run = finished(
        secured,
        optimize(secured, f, objective="FASTEST", lane_adjustments=[{"lane_uid": uids[2], "duration_multiplier": 10}]),
    )
    assert (
        run["routes"][0]["metrics"]["lane_uids"] == [uids[0], uids[6 - 2 - 1 + 0]]
        or run["routes"][0]["metrics"]["duration"]["total_hours"] >= 18
    )
    assert run["baseline_routes"][0]["metrics"]["duration"]["total_hours"] == 6
    assert secured.get(f"{R}/lanes/{uids[2]}", headers=h("editor")).json()["duration_hours"] == 3


def test_runs_are_frozen_once_finished_and_replay_detects_network_change(engine, client, secured):
    f, uids = world(client, secured)
    run = finished(secured, optimize(secured, f))
    with Session(engine) as s:
        row = s.query(RoutingRun).filter_by(run_uid=run["run_uid"]).one()
        row.error = "tamper"
        with pytest.raises(ValueError, match="immutable"):
            s.flush()
        s.rollback()
        s.delete(s.query(RoutingRun).filter_by(run_uid=run["run_uid"]).one())
        with pytest.raises(ValueError, match="cannot be deleted"):
            s.flush()
    rep = secured.post(f"{R}/runs/{run['run_uid']}/replay", headers=h("editor")).json()
    assert rep["network_unchanged"] is True and rep["routes_identical"] is True
    secured.post(f"{R}/lanes/{uids[2]}/status", json={"to": "SUSPENDED"}, headers=h("editor"))
    rep2 = secured.post(f"{R}/runs/{run['run_uid']}/replay", headers=h("editor")).json()
    assert rep2["network_unchanged"] is False and rep2["routes_identical"] is False
    newer = finished(secured, optimize(secured, f))
    assert (
        newer["network_hash"] != run["network_hash"]
        and newer["routes"][0]["metrics"]["lane_uids"] != run["routes"][0]["metrics"]["lane_uids"]
    )


def test_cancel_and_active_run_limit(engine, client, secured):
    f, _ = world(client, secured)
    req = RouteRequest(origin_facility_id=f["A"], destination_facility_id=f["D"])
    with Session(engine) as s, s.begin():
        run, _ = rruns.create(s, req, "editor-user", Limits())
        uid = run.run_uid
    c = secured.post(f"{R}/runs/{uid}/cancel", headers=h("editor"))
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    assert secured.post(f"{R}/runs/{uid}/cancel", headers=h("editor")).status_code == 409
    other = {"origin_facility_id": f["A"], "destination_facility_id": f["D"], "alternatives": 2}
    with Session(engine) as s, s.begin():
        for i in range(4):
            rruns.create(s, RouteRequest(**other | {"max_hops": 2 + i}), "someone", Limits())
        uid2 = s.query(RoutingRun).filter_by(status=SimStatus.QUEUED).first().run_uid
    assert optimize(secured, f, alternatives=7).status_code == 409  # four runs already queued
    assert secured.post(f"{R}/runs/{uid2}/cancel", headers=h("editor")).status_code == 403  # not the requester


def test_network_view_gives_coordinates_only_when_valid(engine, client, secured):
    f, _ = world(client, secured)
    with Session(engine) as s, s.begin():
        from app.models.facility import Facility

        s.get(Facility, f["A"]).latitude, s.get(Facility, f["A"]).longitude = 51.5, -0.12
        s.get(Facility, f["B"]).latitude, s.get(Facility, f["B"]).longitude = 0.0, 0.0
    nodes = {n["facility_id"]: n for n in secured.get(f"{R}/network", headers=h("viewer")).json()["nodes"]}
    assert nodes[f["A"]]["coordinates"]["latitude"] == 51.5
    assert nodes[f["B"]]["coordinates"] is None and nodes[f["C"]]["coordinates"] is None


def test_unavailable_lanes_are_listed_with_the_reason(engine, client, secured):
    f, uids = world(client, secured)
    secured.post(f"{R}/lanes/{uids[0]}/status", json={"to": "SUSPENDED"}, headers=h("editor"))
    secured.post(f"{R}/lanes", json={"origin_facility_id": f["A"], "destination_facility_id": f["B"], "mode": "RAIL", "effective_from": (NOW + timedelta(days=400)).isoformat()}, headers=h("editor"))  # fmt: skip
    j = secured.get(f"{R}/network/validation", headers=h("viewer")).json()
    reasons = {e["lane_uid"]: e["reason"] for e in j["excluded"]}
    assert reasons[uids[0]] == "SUSPENDED" and "NOT_YET_EFFECTIVE" in reasons.values() and uids[0] not in {x["lane_uid"] for x in secured.get(f"{R}/network", headers=h("viewer")).json()["lanes"]}  # fmt: skip


def test_measured_reliability_needs_the_minimum_sample_and_falls_back_to_the_prior(monkeypatch):
    from datetime import datetime, timezone

    from app.leadtime.semantics import Delivery

    lanes = [
        mk_lane(1, 1, 2, "ROAD", 10, 100, reliability_prior=0.5),
        mk_lane(2, 1, 2, "SEA", 40, 50, reliability_prior=0.7),
        mk_lane(3, 2, 3, "ROAD", 10, 100),
    ]
    net = mk_net(lanes)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def d(i, o, dest, mode, late, state="DELIVERED"):
        promise = base + timedelta(days=10 * i + 5)
        return Delivery(key=f"k{i}", ref=f"R{i}", product_id=1, supplier_id=1, destination_id=dest, origin_id=o, mode=mode, order_at=base + timedelta(days=10 * i), dispatched_at=base + timedelta(days=10 * i + 1), original_promised_at=promise, latest_promised_at=promise, arrived_at=promise + timedelta(days=2 if late else -1), received_at=promise + timedelta(days=2 if late else -1), reported_status=state)  # fmt: skip

    ds = (
        [d(i, 1, 2, "ROAD", late=(i % 5 == 0)) for i in range(25)]
        + [d(100 + i, 1, 2, "SEA", late=False) for i in range(5)]
        + [d(200, 2, 3, None, False)]
    )
    monkeypatch.setattr(rdata, "load_deliveries", lambda *a, **k: ds)
    rel, warns = rdata.measured(None, net, NOW, 20, 1000)
    assert (
        rel["LN-T0001"].source == "MEASURED"
        and rel["LN-T0001"].n == 25
        and rel["LN-T0001"].value == pytest.approx(20 / 25)
    )
    assert rel["LN-T0001"].interval[0] < 0.8 < rel["LN-T0001"].interval[1]
    assert rel["LN-T0002"].value is None and rel["LN-T0002"].n == 5  # too few: not a measurement
    assert rel["LN-T0003"].value is None and rel["LN-T0003"].n == 1  # below the minimum: reported, never used

    monkeypatch.setattr(rdata, "facility_risk", lambda *a, **k: {})
    ctx, _ = rdata.build_context(None, net, use_measured=True, min_n=20, max_rows=1000, now=NOW)
    assert (
        ctx.rel["LN-T0001"].source == "MEASURED"
        and ctx.rel["LN-T0002"].source == "PRIOR"
        and ctx.rel["LN-T0003"].source == "UNKNOWN"
    )
    ctx2, _ = rdata.build_context(None, net, use_measured=False, min_n=20, max_rows=1000, now=NOW)
    assert ctx2.rel["LN-T0001"].source == "PRIOR" and ctx2.rel["LN-T0001"].value == 0.5
    assert warns == []


def test_network_issue_detection(client, secured, engine):
    f, _ = world(client, secured)
    a = {
        "origin_facility_id": f["B"],
        "destination_facility_id": f["D"],
        "mode": "ROAD",
        "duration_hours": 99,
        "cost_amount": 5,
        "currency": "EUR",
    }
    assert secured.post(f"{R}/lanes", json=a, headers=h("editor")).status_code == 201
    j = secured.get(f"{R}/network/validation", headers=h("viewer")).json()
    assert {"CONFLICTING_DUPLICATE", "MIXED_CURRENCY", "UNDATED"} <= set(j["by_code"])
    run = finished(secured, optimize(secured, f, objective="FASTEST", alternatives=6))
    conflicted = [r for r in run["routes"] if r["metrics"]["data_flags"]]
    assert conflicted and any("conflicting" in c.lower() for r in conflicted for c in r["explanation"]["caveats"])


def test_diamond_helper_is_consistent_with_the_world():
    assert [ln.uid for ln in diamond()] == [f"LN-T000{i}" for i in range(1, 7)]
