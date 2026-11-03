"""Replenishment API: contract, roles, write gate, redaction, calculations, runs, scenarios, approvals, immutability. SYNTHETIC data only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.invopt import runs as iruns
from app.invopt.config import Limits, ReplenishRequest
from app.main import create_app
from app.models.enums import SimStatus
from app.models.invopt import InventoryCostProfile, ReplenishmentDecision, ReplenishmentRun
from tests.inventory_helpers import code
from tests.invopt_helpers import RP, add_policy, load_leads, setup
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings

SC = "/api/v1/scenarios"
SRC = "/api/v1/sourcing"


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


def body(w, **kw):
    return {
        "product_id": w["p1"]["id"],
        "facility_id": w["f1"]["id"],
        "lead_time_days": 5,
        "lead_time_std_days": 1.0,
        "replications": 20,
        "simulation_days": 60,
        "seed": 5,
    } | kw


def optimize(c, w, role="editor", key=None, **kw):
    hd = h(role) | ({"Idempotency-Key": key} if key else {})
    return c.post(f"{RP}/optimize", json=body(w, **kw), headers=hd)


def finished(c, resp, role="viewer"):
    assert resp.status_code == 202, resp.text
    return c.get(f"{RP}/runs/{resp.json()['run_uid']}", headers=h(role)).json()


COSTS = {
    "currency": "USD",
    "holding_cost_per_unit_day": 0.05,
    "ordering_cost": 25.0,
    "shortage_cost_per_unit": 4.0,
    "unit_cost": 3.0,
}


def test_catalogue_health_and_authentication(client, secured):
    assert secured.get(f"{RP}/health").status_code == 401
    j = secured.get(f"{RP}/health", headers=h("viewer")).json()
    assert j["algorithm_version"] == "io-1" and any("purchase orders" in x for x in j["not_supported"])
    assert "separated by organisation" in j["statements"]["tenancy"]
    cat = secured.get(f"{RP}/catalogue", headers=h("viewer")).json()
    assert {p["id"] for p in cat["policy_types"]} == {"S_Q", "S_UPTO", "R_S"} and {
        s["id"] for s in cat["service_types"]
    } == {"CYCLE", "FILL_RATE"}


def test_no_purchase_order_endpoint_exists(secured):
    paths = secured.get("/openapi.json").json()["paths"]
    mine = [p for p in paths if p.startswith("/api/v1/replenishment")]
    assert mine and not [p for p in mine if "purchase" in p or p.endswith("/execute") or "/order" in p]


def test_roles_and_write_gate(client, secured, readonly):
    w = setup(client)
    assert optimize(secured, w, role="viewer").status_code == 403
    assert secured.post(f"{RP}/optimize", json=body(w)).status_code == 401
    r = optimize(readonly, w, role="editor")
    assert r.status_code in (403, 503) and "disabled" in r.text.lower()
    assert readonly.get(f"{RP}/health", headers=h("viewer")).json()["write_api_enabled"] is False


def test_unknown_fields_are_rejected_including_organisation_id(client, secured):
    w = setup(client)
    for extra in ({"organisation_id": 1}, {"tenant": "x"}, {"sql": "drop"}):
        r = optimize(secured, w, **extra)
        assert r.status_code == 422, r.text


def test_validation_of_ranges_and_references(client, secured):
    w = setup(client)
    assert optimize(secured, w, service_target=1.5).status_code == 422
    assert optimize(secured, w, replications=100000).status_code == 422
    assert optimize(secured, w, safety_stock_method="FIXED").status_code == 422
    assert optimize(secured, w, product_id=99999).status_code == 404
    assert optimize(secured, w, facility_id=99999).status_code == 404
    assert optimize(secured, w, scenario_id=99999).status_code == 404
    assert optimize(secured, w, forecast_run_id=99999).status_code == 404
    assert (
        optimize(secured, w, costs={"holding_cost_per_unit_day": 1.0}).status_code == 422
    )  # currency required with any cost


def test_optimize_runs_and_records_everything_needed(client, secured):
    w = setup(client, on_hand=30)
    run = finished(secured, optimize(secured, w), "editor")
    assert run["status"] == "COMPLETED" and run["result_status"] == "recommended" and run["order_now"] is True
    assert run["algorithm_version"] == "io-1" and run["snapshot_hash"] and run["recommendation_hash"]
    assert run["approval"]["state"] == "PROPOSED" and run["approval"]["executed"] is False
    assert run["provenance"]["lead_time_provenance"] == "ESTIMATED" and run["provenance"]["baseline_unchanged"] is True
    assert run["evaluation"]["synthetic"] is True and run["recommendation"]["decision_support_only"] is True
    assert (
        run["reproducibility"]["seed"] == 5
        and run["reproducibility"]["recommendation_hash"] == run["recommendation_hash"]
    )
    assert any("never places an order" in s for s in run["statements"])


def test_idempotency(client, secured):
    w = setup(client)
    a = optimize(secured, w, key="idem-key-0001")
    b = optimize(secured, w, key="idem-key-0001")
    assert a.json()["run_uid"] == b.json()["run_uid"] and b.headers.get("Idempotent-Replayed") == "true"
    c = optimize(secured, w, key="idem-key-0001", service_target=0.9)
    assert c.status_code == 409
    assert secured.get(f"{RP}/runs", headers=h("viewer")).json()["total"] == 1


def test_costs_are_redacted_for_viewers_and_complete_for_editors(client, secured):
    w = setup(client)
    r = optimize(secured, w, costs=COSTS)
    uid = r.json()["run_uid"]
    ed = secured.get(f"{RP}/runs/{uid}", headers=h("editor")).json()
    vw = secured.get(f"{RP}/runs/{uid}", headers=h("viewer")).json()
    assert ed["costs"]["inputs"]["unit_cost"] == 3.0 and ed["costs"]["recommended_policy"]["holding"] is not None
    assert (
        vw["cost_redacted"] is True
        and vw["costs"]["inputs"]["unit_cost"] is None
        and vw["costs"]["recommended_policy"]["holding"] is None
    )
    assert (
        vw["costs"]["recommended_policy"]["known_subtotal"] is None and vw["recommendation"]["purchase_spend"] is None
    )
    ev = secured.get(f"{RP}/runs/{uid}/evaluation", headers=h("viewer")).json()
    assert "holding" in str(ev["evaluation"]["candidates"][0]["metrics"]["costs"]) and all(
        c["metrics"]["costs"]["holding"] is None for c in ev["evaluation"]["candidates"]
    )
    assert secured.get(f"{RP}/runs/{uid}/snapshot", headers=h("viewer")).status_code == 403
    assert (
        secured.get(f"{RP}/runs/{uid}/snapshot", headers=h("editor")).json()["snapshot"]["costs"]["values"]["unit_cost"]
        == 3.0
    )


def test_cost_profiles_are_versioned_immutable_and_used(client, secured):
    w = setup(client)
    payload = {
        "product_id": w["p1"]["id"],
        "facility_id": w["f1"]["id"],
        "currency": "USD",
        "holding_cost_per_unit_day": 0.04,
        "ordering_cost": 30.0,
        "unit_cost": 2.5,
        "storage_capacity": 5000,
    }
    assert secured.post(f"{RP}/cost-profiles", json=payload, headers=h("viewer")).status_code == 403
    r = secured.post(f"{RP}/cost-profiles", json=payload, headers=h("editor"))
    assert r.status_code == 201, r.text
    uid = r.json()["profile_uid"]
    assert secured.get(f"{RP}/cost-profiles/{uid}", headers=h("viewer")).json()["unit_cost"] is None
    assert secured.get(f"{RP}/cost-profiles/{uid}", headers=h("editor")).json()["unit_cost"] == 2.5
    assert secured.post(f"{RP}/cost-profiles", json={**payload, "holding_cost_per_unit_day": None, "ordering_cost": None, "unit_cost": None, "storage_capacity": None}, headers=h("editor")).status_code == 422  # fmt: skip
    assert secured.post(f"{RP}/cost-profiles", json={**payload, "unit": "KG"}, headers=h("editor")).status_code == 422
    run = finished(secured, optimize(secured, w), "editor")
    assert run["costs"]["provenance"]["unit_cost"] == f"STORED_PROFILE:{uid}" and run["costs"]["missing_inputs"] == [
        "shortage_cost_per_unit"
    ]
    # request costs override the profile field by field, and say so
    run2 = finished(secured, optimize(secured, w, costs={"currency": "USD", "shortage_cost_per_unit": 9.0}), "editor")
    assert (
        run2["costs"]["provenance"]["shortage_cost_per_unit"] == "REQUEST"
        and run2["costs"]["provenance"]["unit_cost"] == f"STORED_PROFILE:{uid}"
    )
    # profiles are immutable; retiring is allowed once
    with client.app.state.session_factory() as s_:
        prof = s_.query(InventoryCostProfile).filter_by(profile_uid=uid).one()
        prof.unit_cost = 99.0
        with pytest.raises(Exception):  # noqa: B017, PT011
            s_.commit()
        s_.rollback()
    assert secured.post(f"{RP}/cost-profiles/{uid}/retire", headers=h("editor")).status_code == 200
    assert secured.post(f"{RP}/cost-profiles/{uid}/retire", headers=h("editor")).status_code == 409
    run3 = finished(secured, optimize(secured, w), "editor")
    assert run3["costs"]["provenance"] == {}  # a retired profile is not used


def test_calculation_endpoints(client, secured):
    w = setup(client, on_hand=100, receipts=True)
    pos = secured.post(f"{RP}/calc/inventory-position", json=body(w), headers=h("viewer")).json()
    assert pos["position"]["inventory_position"] == 180 and pos["position"]["on_order_counted"] == 80
    ss = secured.post(
        f"{RP}/calc/safety-stock", json=body(w, service_type="FILL_RATE", service_target=0.98), headers=h("viewer")
    ).json()
    assert (
        ss["safety_stock"]["value"] > 0
        and ss["safety_stock"]["service_type"] == "FILL_RATE"
        and "assumptions" in ss["safety_stock"]["calculated"]
    )
    rp = secured.post(f"{RP}/calc/reorder-point", json=body(w), headers=h("viewer")).json()
    assert rp["recommendation"]["policy"]["reorder_point"] > 0 and rp["label"].startswith("PROPOSED")
    assert (
        secured.post(
            f"{RP}/calc/safety-stock", json=body(w, what_if={"demand_multiplier": 2}), headers=h("viewer")
        ).status_code
        == 422
    )
    assert (
        secured.post(f"{RP}/calc/safety-stock", json=body(w, product_id=99999), headers=h("viewer")).status_code == 404
    )
    # calculations create nothing
    assert secured.get(f"{RP}/runs", headers=h("viewer")).json()["total"] == 0


def test_positions_overview_flags_gaps_and_paginates(client, secured):
    w = setup(client, on_hand=20)
    add_policy(client, w, reorder_point=50.0, maximum=150.0, lead_time_days=5.0)
    j = secured.get(f"{RP}/positions", headers=h("viewer")).json()
    assert j["total"] == 1 and j["capped"] is False
    row = j["items"][0]
    assert (
        row["status"] == "AT_OR_BELOW_REORDER_POINT"
        and row["has_policy"]
        and row["days_of_cover"] == pytest.approx(20 / row["demand_per_day"], rel=1e-2)
    )
    assert "NO_POLICY" not in row["flags"]
    assert secured.get(f"{RP}/positions?limit=500", headers=h("viewer")).status_code == 422
    assert secured.get(f"{RP}/positions?limit=1&offset=5", headers=h("viewer")).json()["items"] == []


def test_positions_without_policy_or_demand_are_never_healthy(client, secured):
    from tests.inventory_helpers import load_inventory, world

    w = world(client)
    load_inventory(
        client,
        [
            [
                code(w, "p2"),
                code(w, "f1"),
                __import__("tests.inventory_helpers", fromlist=["iso"]).iso(0.1),
                "EA",
                500,
                0,
                0,
                0,
                0,
                "",
                "",
            ]
        ],
    )
    row = secured.get(f"{RP}/positions", headers=h("viewer")).json()["items"][0]
    assert (
        row["status"] == "UNKNOWN"
        and {"NO_POLICY", "NO_USABLE_DEMAND"} <= set(row["flags"])
        and row["days_of_cover"] is None
    )


def test_approval_workflow(client, secured):
    w = setup(client, on_hand=30)
    uid = optimize(secured, w, role="editor").json()["run_uid"]
    assert finished(secured, optimize(secured, w, key="idem-key-0002"))["status"] == "COMPLETED"
    d = lambda role, dec="APPROVED", u=uid: secured.post(
        f"{RP}/runs/{u}/decision", json={"decision": dec, "rationale": "reviewed with planning"}, headers=h(role)
    )  # noqa: E731
    assert d("editor").status_code == 403
    r = d("reviewer")
    assert (
        r.status_code == 200
        and r.json()["approval"]["state"] == "APPROVED"
        and r.json()["approval"]["executed"] is False
    )
    assert d("reviewer").status_code == 409
    assert d("reviewer", "REJECTED").json()["approval"]["state"] == "REJECTED"
    hist = secured.get(f"{RP}/runs/{uid}/decisions", headers=h("viewer")).json()
    assert [x["decision"] for x in hist["history"]] == [
        "APPROVED",
        "REJECTED",
    ] and "does not create purchase orders" in hist["statement"]
    assert (
        secured.post(
            f"{RP}/runs/{uid}/decision", json={"decision": "APPROVED", "rationale": "short"}, headers=h("reviewer")
        ).status_code
        == 422
    )


def test_requester_cannot_approve_own_run_unless_admin(client, secured):
    w = setup(client, on_hand=30)
    uid = optimize(secured, w, role="reviewer").json()["run_uid"]
    dec = {"decision": "APPROVED", "rationale": "reviewed with planning"}
    assert secured.post(f"{RP}/runs/{uid}/decision", json=dec, headers=h("reviewer")).status_code == 409
    assert secured.post(f"{RP}/runs/{uid}/decision", json=dec, headers=h("admin")).status_code == 200


def test_hypothetical_incomplete_and_simulated_runs_cannot_be_approved(client, secured):
    w = setup(client, on_hand=30)
    dec = {"decision": "APPROVED", "rationale": "reviewed with planning"}
    hyp = finished(secured, optimize(secured, w, what_if={"demand_multiplier": 1.5}), "editor")
    assert hyp["hypothetical"] is True
    assert secured.post(f"{RP}/runs/{hyp['run_uid']}/decision", json=dec, headers=h("reviewer")).status_code == 409
    inc = finished(secured, optimize(secured, w, lead_time_days=None, lead_time_std_days=None), "editor")
    assert inc["result_status"] == "incomplete" and inc["status"] == "PARTIAL"
    assert secured.post(f"{RP}/runs/{inc['run_uid']}/decision", json=dec, headers=h("reviewer")).status_code == 409
    sim = secured.post(
        f"{RP}/simulate", json=body(w, proposed={"reorder_point": 60, "order_up_to": 160}), headers=h("editor")
    )
    s = finished(secured, sim, "editor")
    assert s["kind"] == "SIMULATE" and s["result_status"] == "simulated" and s["evaluation"]["selected"] == "PROPOSED"
    assert secured.post(f"{RP}/runs/{s['run_uid']}/decision", json=dec, headers=h("reviewer")).status_code == 409


def test_simulate_requires_proposed_and_optimize_rejects_it(client, secured):
    w = setup(client)
    assert secured.post(f"{RP}/simulate", json=body(w), headers=h("editor")).status_code == 422
    assert optimize(secured, w, proposed={"reorder_point": 60, "order_up_to": 160}).status_code == 422
    assert (
        secured.post(
            f"{RP}/simulate", json=body(w, proposed={"reorder_point": 100, "order_up_to": 50}), headers=h("editor")
        ).status_code
        == 422
    )
    assert (
        secured.post(
            f"{RP}/simulate", json=body(w, policy_type="S_Q", proposed={"reorder_point": 60}), headers=h("editor")
        ).status_code
        == 422
    )


def test_simulation_compares_proposed_policy_with_current_and_reference(client, secured):
    w = setup(client)
    add_policy(client, w, reorder_point=40.0, maximum=120.0, lead_time_days=5.0)
    r = finished(
        secured,
        secured.post(
            f"{RP}/simulate", json=body(w, proposed={"reorder_point": 90, "order_up_to": 200}), headers=h("editor")
        ),
        "editor",
    )
    ev = r["evaluation"]
    assert (
        ev["candidates"][0]["id"] == "PROPOSED"
        and ev["current"]["available"]
        and ev["comparison"]["fill_rate_change"] is not None
    )
    assert (
        ev["recommended"]["metrics"]["avg_on_hand"]["mean"] > ev["current"]["metrics"]["avg_on_hand"]["mean"]
    )  # a higher policy holds more stock


def test_replay_reproduces_from_stored_snapshot(client, secured):
    w = setup(client, on_hand=30)
    uid = optimize(secured, w).json()["run_uid"]
    j = secured.post(f"{RP}/runs/{uid}/replay", headers=h("editor")).json()
    assert (
        j["reproduced_from_stored_snapshot"] is True
        and j["replayed_recommendation_hash"] == j["stored_recommendation_hash"]
    )
    assert secured.post(f"{RP}/runs/{uid}/replay", headers=h("viewer")).status_code == 403


def test_finished_runs_and_decisions_are_immutable(client, secured, engine):
    w = setup(client, on_hand=30)
    uid = optimize(secured, w).json()["run_uid"]
    secured.post(
        f"{RP}/runs/{uid}/decision",
        json={"decision": "APPROVED", "rationale": "reviewed with planning"},
        headers=h("reviewer"),
    )
    sf = client.app.state.session_factory
    with sf() as s:
        run = s.query(ReplenishmentRun).filter_by(run_uid=uid).one()
        run.recommendation_hash = "tampered"
        with pytest.raises(Exception):  # noqa: B017, PT011
            s.commit()
        s.rollback()
        dec = s.query(ReplenishmentDecision).first()
        dec.rationale = "edited later"
        with pytest.raises(Exception):  # noqa: B017, PT011
            s.commit()
        s.rollback()
        s.delete(s.query(ReplenishmentDecision).first())
        with pytest.raises(Exception):  # noqa: B017, PT011
            s.commit()
        s.rollback()
        s.delete(s.query(ReplenishmentRun).filter_by(run_uid=uid).one())
        with pytest.raises((Exception, IntegrityError)):  # noqa: B017, PT011
            s.commit()
        s.rollback()
        assert s.query(InventoryCostProfile).count() == 0


def test_cancel_and_active_run_limit(client, secured):
    w = setup(client)
    sf = client.app.state.session_factory
    req = ReplenishRequest(product_id=w["p1"]["id"], facility_id=w["f1"]["id"], lead_time_days=5)
    with sf() as s, s.begin():
        runs = [
            iruns.create(s, req.model_copy(update={"note": f"n{i}"}), "editor-user", Limits(), idempotency_key=None)[0]
            for i in range(4)
        ]
        uids = [r.run_uid for r in runs]
    r = optimize(secured, w, note="fifth")
    assert r.status_code == 409 and "already queued or running" in r.text
    c = secured.post(f"{RP}/runs/{uids[0]}/cancel", headers=h("editor"))
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    assert secured.post(f"{RP}/runs/{uids[0]}/cancel", headers=h("editor")).status_code == 409
    other = secured.post(f"{RP}/runs/{uids[1]}/cancel", headers=h("viewer"))
    assert other.status_code == 403


def test_runs_listing_filters_and_pagination(client, secured):
    w = setup(client, on_hand=30)
    for i in range(3):
        finished(secured, optimize(secured, w, note=f"r{i}"))
    j = secured.get(f"{RP}/runs?limit=2", headers=h("viewer")).json()
    assert (
        j["total"] == 3
        and len(j["items"]) == 2
        and j["items"][0]["run_uid"] > ""
        and secured.get(f"{RP}/runs?limit=999", headers=h("viewer")).status_code == 422
    )
    assert secured.get(f"{RP}/runs?kind=SIMULATE", headers=h("viewer")).json()["total"] == 0
    assert (
        secured.get(f"{RP}/runs?product_id={w['p1']['id']}&result_status=recommended", headers=h("viewer")).json()[
            "total"
        ]
        == 3
    )
    assert secured.get(f"{RP}/runs/NOPE-NOPE", headers=h("viewer")).status_code == 404


def test_measured_lead_time_flows_into_the_run(client, secured):
    w = setup(client)
    load_leads(client, w, [8, 9, 7, 8, 10, 8, 9, 7, 8, 9, 8, 10])
    r = finished(
        secured,
        secured.post(
            f"{RP}/optimize",
            json={
                "product_id": w["p1"]["id"],
                "facility_id": w["f1"]["id"],
                "replications": 20,
                "simulation_days": 60,
                "seed": 5,
            },
            headers=h("editor"),
        ),
        "editor",
    )
    assert (
        r["lead_time"]["source"] == "MEASURED"
        and r["lead_time"]["provenance"] == "MEASURED"
        and r["safety_stock"]["method"] == "NORMAL_LT"
    )
    assert r["provenance"]["lead_time_provenance"] == "MEASURED"


def _offer(c, w, **kw):
    b = {"supplier_id": w["s1"]["id"], "product_id": w["p1"]["id"], "destination_facility_id": w["f1"]["id"], "approval_status": "APPROVED", "unit": "EA", "unit_price": 3.0, "currency": "USD", "price_quality": "VERIFIED", "min_order_quantity": 50, "lot_size": 10, "lead_time_days": 6, "lead_time_quality": "VERIFIED", "provenance": "SYNTHETIC_DEMO"} | kw  # fmt: skip
    r = c.post(f"{SRC}/offers", json=b, headers=h("editor"))
    assert r.status_code == 201, r.text
    return r.json()


def test_sourcing_offer_terms_are_enforced_and_provenance_recorded(client, secured):
    w = setup(client, on_hand=30)
    off = _offer(secured, w)
    r = finished(
        secured, optimize(secured, w, lead_time_days=None, lead_time_std_days=None, supplier_id=w["s1"]["id"]), "editor"
    )
    assert r["lead_time"]["source"] == "SOURCING_OFFER" and r["lead_time"]["provenance"] == "VERIFIED"
    q = r["recommendation"]["quantity"]
    assert q >= 50 and q % 10 == 0
    assert r["provenance"]["offer"]["offer_uid"] == off["offer_uid"] and r["costs"]["inputs"]["unit_cost"] == 3.0
    assert r["costs"]["provenance"]["unit_cost"].startswith("SOURCING_OFFER")


def test_ineligible_supplier_makes_the_run_infeasible_and_unapprovable(client, secured):
    w = setup(client, on_hand=30)
    _offer(secured, w, approval_status="DISQUALIFIED")
    r = finished(secured, optimize(secured, w, supplier_id=w["s1"]["id"]), "editor")
    assert (
        r["result_status"] == "infeasible"
        and r["infeasibility"][0]["code"] == "SUPPLIER_NOT_ELIGIBLE"
        and r["recommendation"]["order_now"] is False
    )
    assert (
        secured.post(
            f"{RP}/runs/{r['run_uid']}/decision",
            json={"decision": "APPROVED", "rationale": "reviewed with planning"},
            headers=h("reviewer"),
        ).status_code
        == 409
    )


def _scenario(c, events):
    r = c.post(SC, json={"name": "Supplier loss", "scenario_type": "CUSTOM", "events": events}, headers=h("editor"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert c.post(f"{SC}/{sid}/validate", headers=h("editor")).json()["can_run"] is True
    return sid


def test_scenario_is_applied_to_copies_and_unmapped_events_are_reported(client, secured):
    w = setup(client, on_hand=120)
    _offer(secured, w)
    base = finished(secured, optimize(secured, w, supplier_id=w["s1"]["id"], note="base"), "editor")
    sid = _scenario(secured, [
        {"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": w["s1"]["id"], "duration_days": 14},
        {"event_type": "SUPPLIER_CAPACITY_REDUCTION", "target_type": "SUPPLIER", "target_id": w["s1"]["id"], "magnitude_pct": 50, "duration_days": 10},
    ])  # fmt: skip
    r = finished(secured, optimize(secured, w, supplier_id=w["s1"]["id"], scenario_id=sid, note="scn"), "editor")
    assert r["status"] == "COMPLETED" and r["hypothetical"] is True and r["scenario_hash"]
    sr = r["scenario_results"][0]
    assert (
        sr["effective"]["orders_blocked"] == [1, 14]
        and sr["recommended_as_is"]["fill_rate"]["mean"] <= sr["undisrupted_recommended"]["fill_rate"]["mean"]
    )
    nap = r["provenance"]["scenario"]["not_applied"]
    assert len(nap) == 1 and "capacity" in nap[0]["reason"].lower()
    assert any(x["code"] == "SCENARIO_EVENTS_NOT_APPLIED" for x in r["warnings"]["warnings"])
    # the baseline run and the stored records are unchanged
    again = secured.get(f"{RP}/runs/{base['run_uid']}", headers=h("editor")).json()
    assert (
        again["snapshot_hash"] == base["snapshot_hash"] and again["recommendation_hash"] == base["recommendation_hash"]
    )
    assert (
        secured.post(
            f"{RP}/runs/{r['run_uid']}/decision",
            json={"decision": "APPROVED", "rationale": "reviewed with planning"},
            headers=h("reviewer"),
        ).status_code
        == 409
    )


def test_event_for_an_unrelated_supplier_is_not_applied(client, secured):
    from tests.graph_helpers import node

    w = setup(client, on_hand=120)
    _offer(secured, w)
    other = node(client, "suppliers", "Other Co")
    sid = _scenario(
        secured,
        [
            {
                "event_type": "SUPPLIER_UNAVAILABLE",
                "target_type": "SUPPLIER",
                "target_id": other["id"],
                "duration_days": 14,
            }
        ],
    )
    r = finished(secured, optimize(secured, w, supplier_id=w["s1"]["id"], scenario_id=sid), "editor")
    assert r["scenario_results"] is None or r["scenario_results"] == []
    assert len(r["provenance"]["scenario"]["not_applied"]) == 1


def test_draft_scenarios_cannot_be_used(client, secured):
    w = setup(client)
    r = secured.post(SC, json={"name": "Draft", "scenario_type": "CUSTOM", "events": []}, headers=h("editor"))
    assert optimize(secured, w, scenario_id=r.json()["id"]).status_code == 409


def test_run_with_stale_or_missing_inputs_is_flagged_not_filled(client, secured):
    from tests.inventory_helpers import load_demand, world
    from tests.invopt_helpers import noisy_rows

    w = world(client)
    load_demand(client, noisy_rows(w, "p1", "f1", 10, 3, 60))
    r = finished(secured, optimize(secured, w), "editor")
    assert (
        r["result_status"] == "incomplete"
        and r["recommendation"] is None
        and r["position"]["inventory_position"] is None
    )
    assert any(i["code"] == "INVENTORY_MISSING" for i in r["warnings"]["data_quality"])


def test_audit_entries_are_written(client, secured):
    w = setup(client, on_hand=30)
    uid = optimize(secured, w).json()["run_uid"]
    secured.post(
        f"{RP}/runs/{uid}/decision",
        json={"decision": "APPROVED", "rationale": "reviewed with planning"},
        headers=h("reviewer"),
    )
    with client.app.state.session_factory() as s:
        acts = {r[0] for r in s.execute(text("select action from audit_events")).all()}
    assert {"replenishment.run.queued", "replenishment.run.approved"} <= acts


def test_run_status_enum_unchanged(client):
    assert SimStatus.COMPLETED.value == "COMPLETED"
