"""Sourcing API: contract, roles, write gate, redaction, discovery, optimisation, scenarios, approvals, immutability. Synthetic data only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.enums import SimStatus
from app.models.sourcing import SourcingDecision, SourcingOffer, SourcingRun
from app.sourcing import data as sdata
from app.sourcing import runs as sruns
from app.sourcing.config import Limits, SourcingRequest
from tests.graph_helpers import node, ok
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings
from tests.sourcing_helpers import NOW, S

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


def offer_body(w, key, price=10.0, cap=100.0, **kw):
    return {"supplier_id": w[key]["id"], "product_id": w["p"]["id"], "destination_facility_id": w["dest"]["id"], "approval_status": "APPROVED", "unit": "kg", "unit_price": price, "currency": "USD", "price_quality": "VERIFIED", "capacity_amount": cap, "capacity_quality": "VERIFIED", "lead_time_days": 10, "lead_time_quality": "VERIFIED", "defect_rate": 0.01, "quality_quality": "VERIFIED", "provenance": "SYNTHETIC_DEMO", "observed_at": NOW.isoformat()} | kw  # fmt: skip


def world(client, secured, prices=((10.0, 40.0), (12.0, 100.0), (15.0, 100.0))):
    w = {"p": node(client, "products", "Resin", unit_of_measure="kg"), "dest": node(client, "facilities", "Plant D")}
    w["s1"] = node(client, "suppliers", "Alpha Resins")
    w["s2"] = node(client, "suppliers", "Beta Polymers")
    w["s3"] = node(client, "suppliers", "Gamma Chemicals")
    for k in ("s1", "s2", "s3"):
        ok(client, "SUPPLIES_PRODUCT", w[k], w["p"])
    w["offers"] = {}
    for k, (price, cap) in zip(("s1", "s2", "s3"), prices, strict=True):
        r = secured.post(f"{S}/offers", json=offer_body(w, k, price, cap), headers=h("editor"))
        assert r.status_code == 201, r.text
        w["offers"][k] = r.json()["offer_uid"]
    return w


def req_body(w, **kw):
    return {
        "product_id": w["p"]["id"],
        "destination_facility_id": w["dest"]["id"],
        "quantity": 60,
        "unit": "kg",
        "currency": "USD",
    } | kw


def optimize(c, w, role="editor", key=None, **kw):
    hd = h(role) | ({"Idempotency-Key": key} if key else {})
    return c.post(f"{S}/optimize", json=req_body(w, **kw), headers=hd)


def finished(c, resp, role="viewer"):
    assert resp.status_code == 202, resp.text
    return c.get(f"{S}/runs/{resp.json()['run_uid']}", headers=h(role)).json()


def alloc(run):
    return {a["supplier_code"]: a["quantity"] for a in run["allocations"]}


def test_catalogues_and_disclosure(client):
    j = client.get(f"{S}/health").json()
    assert (
        j["algorithm_version"] == "sc-1"
        and any("purchase orders" in x for x in j["not_supported"])
        and not any("organisation" in x for x in j["not_supported"])
    )
    assert {o["id"] for o in client.get(f"{S}/objectives").json()["objectives"]} == {
        "LOWEST_COST",
        "LOWEST_LANDED_COST",
        "RELIABILITY",
        "LOWEST_RISK",
        "BALANCED",
        "RESILIENCE",
    }
    assert "MISSING_INFORMATION" in client.get(f"{S}/constraints").json()["statuses"]


def test_offer_validation_rejects_bad_input(client, secured):
    w = world(client, secured)
    post = lambda b: secured.post(f"{S}/offers", json=b, headers=h("editor"))  # noqa: E731
    base = offer_body(w, "s1")
    assert post(base | {"currency": None}).status_code == 422  # price without currency
    assert post(base | {"unit": None}).status_code == 422  # capacity without unit
    assert post(base | {"capacity_amount": None}).status_code == 422  # quality claimed for a missing value
    assert post(base | {"unit_price": -1}).status_code == 422
    assert post(base | {"defect_rate": 1.5}).status_code == 422
    assert post(base | {"observed_at": "2026-01-01T00:00:00"}).status_code == 422  # naive timestamp
    assert (
        post(base | {"organisation_id": 7}).status_code == 422
    )  # the organisation comes from the credential: never accepted in a body
    assert post(base | {"supplier_id": 99999}).status_code == 404
    assert post(base | {"product_id": 99999}).status_code == 404
    assert post(base | {"destination_facility_id": 99999}).status_code == 404
    assert (
        post(
            base
            | {"contract_valid_from": "2026-05-01T00:00:00+00:00", "contract_valid_to": "2026-01-01T00:00:00+00:00"}
        ).status_code
        == 422
    )
    assert post(base | {"freight_per_unit": 1.0, "unit_price": None, "currency": None}).status_code == 422


def test_database_constraints_and_immutability(engine, client, secured):
    w = world(client, secured)
    with Session(engine) as s:
        o = s.scalar(select(SourcingOffer))
        o.unit_price = 99.0
        with pytest.raises(ValueError, match="immutable"):
            s.flush()
        s.rollback()
        o = s.scalar(select(SourcingOffer))
        s.delete(o)
        with pytest.raises(ValueError, match="cannot be deleted"):
            s.flush()
        s.rollback()
    with Session(engine) as s:
        with pytest.raises(IntegrityError):
            s.execute(text("update sourcing_offers set defect_rate = 2 where id = 1"))
            s.commit()
        s.rollback()
    uid = w["offers"]["s1"]
    assert (
        secured.post(f"{S}/offers/{uid}/status", json={"to": "SUSPENDED"}, headers=h("editor")).json()["status"]
        == "SUSPENDED"
    )
    assert secured.post(f"{S}/offers/{uid}/status", json={"to": "ACTIVE"}, headers=h("editor")).status_code == 200
    assert secured.post(f"{S}/offers/{uid}/status", json={"to": "RETIRED"}, headers=h("editor")).status_code == 200
    assert (
        secured.post(f"{S}/offers/{uid}/status", json={"to": "ACTIVE"}, headers=h("editor")).status_code == 409
    )  # final
    with Session(engine) as s:
        assert s.scalar(select(SourcingOffer).where(SourcingOffer.offer_uid == uid)).retired_at is not None


def test_auth_matrix_write_gate_and_price_redaction(client, secured, readonly):
    w = world(client, secured)
    assert secured.get(f"{S}/offers").status_code == 401
    assert secured.post(f"{S}/offers", json=offer_body(w, "s1"), headers=h("viewer")).status_code == 403
    assert secured.post(f"{S}/optimize", json=req_body(w), headers=h("viewer")).status_code == 403
    uid = w["offers"]["s1"]
    v = secured.get(f"{S}/offers/{uid}", headers=h("viewer")).json()
    e = secured.get(f"{S}/offers/{uid}", headers=h("editor")).json()
    assert v["unit_price"] is None and v["price_redacted"] is True and e["unit_price"] == 10.0
    assert all(o["unit_price"] is None for o in secured.get(f"{S}/offers", headers=h("viewer")).json()["items"])
    alt_v = secured.post(f"{S}/alternatives", json=req_body(w), headers=h("viewer")).json()
    assert alt_v["cost_redacted"] is True and all(c["terms"]["unit_price"] is None for c in alt_v["candidates"])
    price_comp = next(x for c in alt_v["candidates"] for x in c["score"]["components"] if x["metric"] == "price")
    assert price_comp["raw"] is None and price_comp["normalised"] is not None
    alt_e = secured.post(f"{S}/alternatives", json=req_body(w), headers=h("editor")).json()
    assert {c["terms"]["unit_price"] for c in alt_e["candidates"]} == {10.0, 12.0, 15.0}
    run = finished(secured, optimize(secured, w))
    assert run["cost_redacted"] is True and run["purchase_cost"] is None and run["metrics"]["purchase_cost"] is None
    assert all(a["unit_price"] is None and a["purchase_cost"] is None for a in run["allocations"])
    assert run["allocations"][0]["quantity"] > 0  # quantities stay visible
    uid_run = run["run_uid"]
    assert secured.get(f"{S}/runs/{uid_run}/breakdown", headers=h("viewer")).status_code == 403
    assert secured.get(f"{S}/runs/{uid_run}/snapshot", headers=h("viewer")).status_code == 403
    bd = secured.get(f"{S}/runs/{uid_run}/breakdown", headers=h("editor")).json()
    assert bd["metrics"]["purchase_cost"] == 640.0 and [x["unit_price"] for x in bd["lines"]] == [10.0, 12.0]
    r = readonly.post(f"{S}/offers", json=offer_body(w, "s1"), headers=h("admin"))
    assert r.status_code == 403
    assert readonly.get(f"{S}/offers", headers=h("viewer")).status_code == 200


def test_alternatives_report_status_evidence_and_missing_information(client, secured):
    w = world(client, secured)
    # a supplier with a confirmed relationship but no offer, one pending qualification, one with unknown approval
    w["s4"] = node(client, "suppliers", "Delta Link Only")
    ok(client, "SUPPLIES_PRODUCT", w["s4"], w["p"])
    w["s5"] = node(client, "suppliers", "Epsilon Pending")
    ok(client, "SUPPLIES_PRODUCT", w["s5"], w["p"])
    assert (
        secured.post(
            f"{S}/offers",
            json=offer_body(w, "s5", 5.0, 500.0, approval_status="PENDING_QUALIFICATION"),
            headers=h("editor"),
        ).status_code
        == 201
    )
    w["s6"] = node(client, "suppliers", "Zeta Unknown")
    ok(client, "SUPPLIES_PRODUCT", w["s6"], w["p"])
    assert (
        secured.post(
            f"{S}/offers", json=offer_body(w, "s6", 4.0, 500.0, approval_status="UNKNOWN"), headers=h("editor")
        ).status_code
        == 201
    )
    j = secured.post(f"{S}/alternatives", json=req_body(w), headers=h("editor")).json()
    st = {c["supplier_id"]: c for c in j["candidates"]}
    assert j["counts"] == {"ELIGIBLE": 3, "MISSING_INFORMATION": 2, "ELIGIBLE_AFTER_QUALIFICATION": 1}
    assert (
        st[w["s4"]["id"]]["status"] == "MISSING_INFORMATION" and "commercial offer" in st[w["s4"]["id"]]["missing"][0]
    )
    assert st[w["s5"]["id"]]["status"] == "ELIGIBLE_AFTER_QUALIFICATION" and st[w["s5"]["id"]]["rank"] is None
    assert st[w["s6"]["id"]]["missing"] == ["approval status"]
    ranked = [c["supplier_code"] for c in j["candidates"] if c["rank"]]
    assert len(ranked) == 3 and j["candidates"][0]["rank"] == 1  # ranked first, unranked after
    ev = {e["rule"]: e["outcome"] for e in st[w["s1"]["id"]]["evidence"]}
    assert ev["approval"] == "PASS" and ev["supplier_product_relationship"] == "PASS"
    elig = secured.post(f"{S}/eligibility", json=req_body(w), headers=h("viewer")).json()
    assert elig["counts"]["ELIGIBLE"] == 3 and "terms" not in elig["candidates"][0]
    rk = secured.post(f"{S}/ranking", json=req_body(w), headers=h("viewer")).json()
    assert len(rk["ranked"]) == 3 and len(rk["not_ranked"]) == 3
    assert rk["algorithm_version"] == "sc-1" and "probability" in rk["statement"]
    # request filters change eligibility without touching any record
    f = secured.post(
        f"{S}/alternatives", json=req_body(w, constraints={"max_lead_time_days": 5}), headers=h("editor")
    ).json()
    assert f["counts"] == {"EXCLUDED": 5, "MISSING_INFORMATION": 1}
    assert (
        secured.get(f"{S}/offers?approval=APPROVED&product_id={w['p']['id']}", headers=h("viewer")).json()["total"] == 3
    )


def test_optimize_end_to_end_with_traceability(client, secured, engine):
    w = world(client, secured)
    run = finished(secured, optimize(secured, w, objective="LOWEST_COST"))
    assert run["status"] == "COMPLETED" and run["result_status"] == "feasible" and run["kind"] == "OPTIMIZE"
    assert sum(alloc(run).values()) == 60.0
    assert sorted(a["quantity"] for a in run["allocations"]) == [20.0, 40.0]
    assert run["metrics"]["fulfilment_pct"] == 100.0 and run["metrics"]["unmet_quantity"] == 0.0
    assert run["approval"]["state"] == "PROPOSED" and run["approval"]["executed"] is False
    assert (
        run["reproducibility"]["algorithm_version"] == "sc-1"
        and run["reproducibility"]["snapshot_hash"] == run["snapshot_hash"]
    )
    assert run["summary"]["solver"]["optimality"] == "EXACT" and all(
        c["passed"] for c in run["summary"]["hard_constraint_checks"]
    )
    assert any("synthetic" in x for x in run["warnings"]) and any(
        "tenancy" in s.lower() or "organisation" in s for s in run["statements"]
    )
    snap = secured.get(f"{S}/runs/{run['run_uid']}/snapshot", headers=h("editor")).json()
    assert snap["snapshot_hash"] == run["snapshot_hash"] and {c["price"] for c in snap["candidates"]} == {
        10.0,
        12.0,
        15.0,
    }
    a = secured.get(f"{S}/runs/{run['run_uid']}/allocation", headers=h("viewer")).json()
    assert a["label"].startswith("PROPOSED") and a["allocations"]
    assert secured.get(f"{S}/runs/{run['run_uid']}/comparison", headers=h("viewer")).status_code == 422
    hist = secured.get(
        f"{S}/runs?product_id={w['p']['id']}&kind=OPTIMIZE&result_status=feasible", headers=h("viewer")
    ).json()
    assert hist["total"] == 1 and hist["items"][0]["run_uid"] == run["run_uid"]
    with Session(engine) as s:
        assert [o.status for o in s.scalars(select(SourcingOffer))] == [
            "ACTIVE"
        ] * 3  # the optimiser did not touch the offers


def test_infeasible_partial_and_incomplete_are_distinct(client, secured):
    w = world(client, secured, prices=((10.0, 20.0), (12.0, 30.0), (15.0, 0.0)))
    inf = finished(secured, optimize(secured, w))
    assert inf["status"] == "COMPLETED" and inf["result_status"] == "infeasible" and inf["allocations"] == []
    assert {r["code"] for r in inf["infeasibility"]["reasons"]} >= {"INSUFFICIENT_CAPACITY"}
    part = finished(secured, optimize(secured, w, fulfillment="PARTIAL"))
    assert (
        part["result_status"] == "partial"
        and part["metrics"]["unmet_quantity"] == 10.0
        and sum(a["quantity"] for a in part["allocations"]) == 50.0
    )
    assert part["infeasibility"]["unmet_quantity"] == 10.0
    empty = {"p": node(client, "products", "Other", unit_of_measure="kg"), "dest": w["dest"]}
    none = finished(secured, optimize(secured, empty))
    assert none["result_status"] == "infeasible" and none["infeasibility"]["reasons"][0]["code"] == "NO_CANDIDATES"
    w2 = world(client, secured)  # a fresh product/destination with an unverifiable supplier
    sup = node(client, "suppliers", "Unknown Approval Co")
    pr = node(client, "products", "Gum", unit_of_measure="kg")
    ok(client, "SUPPLIES_PRODUCT", sup, pr)
    secured.post(f"{S}/offers", json={"supplier_id": sup["id"], "product_id": pr["id"], "destination_facility_id": w2["dest"]["id"], "unit": "kg", "unit_price": 5.0, "currency": "USD", "capacity_amount": 500, "provenance": "SYNTHETIC_DEMO"}, headers=h("editor"))  # fmt: skip
    inc = finished(secured, optimize(secured, {"p": pr, "dest": w2["dest"]}))
    assert inc["result_status"] == "incomplete" and inc["infeasibility"]["information_that_would_help"]


def test_validation_errors_and_bounds(client, secured):
    w = world(client, secured)
    p = lambda **kw: secured.post(f"{S}/optimize", json=req_body(w, **kw), headers=h("editor"))  # noqa: E731
    assert p(quantity=0).status_code == 422 and p(quantity=-5).status_code == 422
    assert p(currency="usd").status_code == 422 and p(unit="").status_code == 422
    assert p(organisation_id=3).status_code == 422
    assert p(objective="CHEAPEST").status_code == 422
    assert p(unit="lb").status_code == 422  # the product is measured in kg: no silent conversion
    assert p(strategy="SINGLE", constraints={"min_suppliers": 2}).status_code == 422
    assert p(constraints={"max_supplier_share": 0.3, "max_suppliers": 2}).status_code == 422
    assert (
        p(weights={"price": 0, "lead_time": 0, "reliability": 0, "quality": 0, "capacity": 0, "risk": 0}).status_code
        == 422
    )
    assert p(weights={"price": -1}).status_code == 422
    assert p(constraints={"allowed_supplier_ids": [1], "excluded_supplier_ids": [1]}).status_code == 422
    assert p(constraints={"excluded_supplier_ids": [99999]}).status_code == 422
    assert p(adjustments=[{"supplier_id": 99999, "unavailable": True}]).status_code == 422
    assert p(scenario_id=99999).status_code == 404
    assert (
        secured.post(f"{S}/optimize", json=req_body(w) | {"product_id": 99999}, headers=h("editor")).status_code == 404
    )
    assert (
        secured.post(
            f"{S}/optimize", json=req_body(w) | {"destination_facility_id": 99999}, headers=h("editor")
        ).status_code
        == 404
    )
    v = secured.post(f"{S}/validate", json=req_body(w, quantity=500), headers=h("viewer")).json()
    assert v["valid"] is True and v["capacity_sufficient"] is False and v["capacity_shortfall"] == 260.0
    bad = secured.post(
        f"{S}/validate", json=req_body(w, strategy="SINGLE", constraints={"min_suppliers": 2}), headers=h("viewer")
    ).json()
    assert bad["valid"] is False and bad["problems"]


def test_idempotency_and_duplicate_protection(client, secured):
    w = world(client, secured)
    a = optimize(secured, w, key="idem-key-0001")
    b = optimize(secured, w, key="idem-key-0001")
    assert (
        a.status_code == b.status_code == 202
        and a.json()["run_uid"] == b.json()["run_uid"]
        and b.headers["Idempotent-Replayed"] == "true"
    )
    assert optimize(secured, w, key="idem-key-0001", objective="BALANCED").status_code == 409
    assert secured.get(f"{S}/runs", headers=h("viewer")).json()["total"] == 1


def _scenario(c, events):
    r = c.post(SC, json={"name": "Supplier loss", "scenario_type": "CUSTOM", "events": events}, headers=h("editor"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert c.post(f"{SC}/{sid}/validate", headers=h("editor")).json()["can_run"] is True
    return sid


def test_scenario_replans_without_touching_the_baseline(client, secured, engine):
    w = world(client, secured, prices=((10.0, 100.0), (12.0, 100.0), (15.0, 100.0)))
    sid = _scenario(
        secured,
        [
            {
                "event_type": "SUPPLIER_UNAVAILABLE",
                "target_type": "SUPPLIER",
                "target_id": w["s1"]["id"],
                "duration_days": 30,
            }
        ],
    )
    run = finished(secured, optimize(secured, w, scenario_id=sid), "editor")
    assert (
        run["status"] == "COMPLETED"
        and run["scenario_id"] == sid
        and run["scenario_hash"]
        and run["hypothetical"] is True
    )
    assert {a["supplier_code"] for a in run["baseline_allocations"]} != {a["supplier_code"] for a in run["allocations"]}
    cmp = secured.get(f"{S}/runs/{run['run_uid']}/comparison", headers=h("editor")).json()
    assert cmp["comparison"]["suppliers_lost"] and cmp["comparison"]["purchase_cost"]["change"] == pytest.approx(120.0)
    assert cmp["disruptions"]["baseline_unchanged"] is True and cmp["disruptions"]["scenario"]["scenario_id"] == sid
    assert any("timing windows" in x for x in run["warnings"])
    with Session(engine) as s:
        assert {o.status for o in s.scalars(select(SourcingOffer))} == {"ACTIVE"}
    # an event with no mappable target is reported, never silently ignored
    sid2 = _scenario(
        secured,
        [
            {
                "event_type": "FACILITY_CAPACITY_REDUCTION",
                "target_type": "FACILITY",
                "target_id": w["dest"]["id"],
                "magnitude_pct": 30,
                "duration_days": 5,
            }
        ],
    )
    r2 = finished(secured, optimize(secured, w, scenario_id=sid2), "editor")
    assert r2["disruptions"]["not_applied"][0]["event_type"] == "FACILITY_CAPACITY_REDUCTION" and any(
        "could not be mapped" in x for x in r2["warnings"]
    )
    # destination shut down: nothing can be delivered and the cause is stated
    sid3 = _scenario(
        secured,
        [
            {
                "event_type": "FACILITY_SHUTDOWN",
                "target_type": "FACILITY",
                "target_id": w["dest"]["id"],
                "duration_days": 5,
            }
        ],
    )
    r3 = finished(secured, optimize(secured, w, scenario_id=sid3), "editor")
    assert r3["result_status"] == "infeasible" and r3["allocations"] == [] and r3["baseline_allocations"]
    assert (
        secured.get(f"{S}/runs/{r3['run_uid']}/comparison", headers=h("viewer")).json()["comparison"][
            "feasibility_changed"
        ]
        is True
    )


def test_draft_scenarios_cannot_be_used(client, secured):
    w = world(client, secured)
    r = secured.post(
        SC,
        json={
            "name": "Draft",
            "scenario_type": "CUSTOM",
            "events": [
                {
                    "event_type": "SUPPLIER_UNAVAILABLE",
                    "target_type": "SUPPLIER",
                    "target_id": w["s1"]["id"],
                    "duration_days": 5,
                }
            ],
        },
        headers=h("editor"),
    )
    assert optimize(secured, w, scenario_id=r.json()["id"]).status_code == 409


def test_what_if_adjustments_change_the_copy_only(client, secured, engine):
    w = world(client, secured, prices=((10.0, 100.0), (12.0, 100.0), (15.0, 100.0)))
    run = finished(
        secured, optimize(secured, w, adjustments=[{"supplier_id": w["s1"]["id"], "price_multiplier": 3.0}]), "editor"
    )
    assert run["hypothetical"] is True and run["allocations"][0]["supplier_name"] == "Beta Polymers"
    assert run["baseline_allocations"][0]["supplier_name"] == "Alpha Resins"
    with Session(engine) as s:
        assert {o.unit_price for o in s.scalars(select(SourcingOffer))} == {10.0, 12.0, 15.0}


def test_compare_strategies_run(client, secured):
    w = world(client, secured, prices=((10.0, 100.0), (12.0, 100.0), (15.0, 100.0)))
    sid = _scenario(
        secured,
        [
            {
                "event_type": "SUPPLIER_UNAVAILABLE",
                "target_type": "SUPPLIER",
                "target_id": w["s1"]["id"],
                "duration_days": 30,
            }
        ],
    )
    r = secured.post(f"{S}/compare-strategies", json=req_body(w, scenario_id=sid), headers=h("editor"))
    run = finished(secured, r, "editor")
    s = run["comparison"]["strategies"]
    assert (
        set(s) == {"SINGLE", "DUAL", "MULTI", "AUTO"}
        and s["SINGLE"]["exposure_if_executed_unchanged"]["lost_share"] == 1.0
    )
    assert (
        s["DUAL"]["cost_of_diversification_vs_cheapest"]["amount"] > 0
        and s["SINGLE"]["reoptimised_under_scenario"]["result_status"] == "feasible"
    )
    assert run["kind"] == "COMPARE" and run["approval"] is None
    assert secured.get(f"{S}/runs/{run['run_uid']}/allocation", headers=h("viewer")).status_code == 422
    assert (
        secured.post(
            f"{S}/runs/{run['run_uid']}/decision",
            json={"decision": "APPROVED", "rationale": "ten chars at least"},
            headers=h("reviewer"),
        ).status_code
        == 409
    )
    v = finished(secured, r, "viewer")
    assert v["comparison"]["strategies"]["SINGLE"]["purchase_cost"] is None  # costs are editor-only


def test_approval_is_separate_insert_only_and_never_executes(client, secured, engine):
    w = world(client, secured)
    run = finished(secured, optimize(secured, w, role="editor"))
    uid = run["run_uid"]
    body = {"decision": "APPROVED", "rationale": "Meets demand within contract ceilings."}
    assert secured.post(f"{S}/runs/{uid}/decision", json=body, headers=h("editor")).status_code == 403
    assert secured.post(f"{S}/runs/{uid}/decision", json=body, headers=h("viewer")).status_code == 403
    assert (
        secured.post(f"{S}/runs/{uid}/decision", json=body | {"rationale": "short"}, headers=h("reviewer")).status_code
        == 422
    )
    d = secured.post(f"{S}/runs/{uid}/decision", json=body, headers=h("reviewer"))
    assert (
        d.status_code == 200
        and d.json()["approval"]["state"] == "APPROVED"
        and d.json()["approval"]["executed"] is False
    )
    assert (
        secured.post(f"{S}/runs/{uid}/decision", json=body, headers=h("reviewer")).status_code == 409
    )  # already approved
    rej = secured.post(
        f"{S}/runs/{uid}/decision",
        json={"decision": "REJECTED", "rationale": "Superseded by a new quote."},
        headers=h("reviewer"),
    )
    assert rej.json()["approval"]["state"] == "REJECTED"
    hist = secured.get(f"{S}/runs/{uid}/decisions", headers=h("viewer")).json()
    assert (
        [x["decision"] for x in hist["history"]] == ["APPROVED", "REJECTED"]
        and hist["current"]["state"] == "REJECTED"
        and "does not place orders" in hist["statement"]
    )
    with Session(engine) as s:
        row = s.scalar(select(SourcingDecision))
        row.rationale = "tampered"
        with pytest.raises(ValueError, match="insert-only"):
            s.flush()
        s.rollback()
        s.delete(s.scalar(select(SourcingDecision)))
        with pytest.raises(ValueError, match="insert-only"):
            s.flush()
        s.rollback()
        assert (
            s.scalar(select(SourcingRun).where(SourcingRun.run_uid == uid)).result_status == "feasible"
        )  # the run itself never changed


def test_only_feasible_non_hypothetical_runs_by_another_person_can_be_approved(client, secured):
    w = world(client, secured, prices=((10.0, 20.0), (12.0, 30.0), (15.0, 0.0)))
    body = {"decision": "APPROVED", "rationale": "Trying to approve a shortfall."}
    inf = finished(secured, optimize(secured, w))
    assert secured.post(f"{S}/runs/{inf['run_uid']}/decision", json=body, headers=h("reviewer")).status_code == 409
    part = finished(secured, optimize(secured, w, fulfillment="PARTIAL"))
    assert (
        secured.post(f"{S}/runs/{part['run_uid']}/decision", json=body, headers=h("reviewer")).status_code == 409
    )  # a partial plan is never approved
    assert (
        secured.post(
            f"{S}/runs/{part['run_uid']}/decision",
            json={"decision": "REJECTED", "rationale": "Rejecting the shortfall."},
            headers=h("reviewer"),
        ).status_code
        == 200
    )
    w2 = world(client, secured)
    own = finished(secured, optimize(secured, w2, role="reviewer"))
    assert (
        secured.post(f"{S}/runs/{own['run_uid']}/decision", json=body, headers=h("reviewer")).status_code == 409
    )  # no self-approval
    assert secured.post(f"{S}/runs/{own['run_uid']}/decision", json=body, headers=h("admin")).status_code == 200
    hyp = finished(
        secured,
        optimize(secured, w2, adjustments=[{"supplier_id": w2["s1"]["id"], "capacity_multiplier": 2.0}]),
        "editor",
    )
    assert secured.post(f"{S}/runs/{hyp['run_uid']}/decision", json=body, headers=h("reviewer")).status_code == 409


def test_approval_refused_when_an_offer_changed_after_the_run(client, secured):
    w = world(client, secured)
    run = finished(secured, optimize(secured, w))
    used = run["allocations"][0]["offer_uid"]
    assert secured.post(f"{S}/offers/{used}/status", json={"to": "SUSPENDED"}, headers=h("editor")).status_code == 200
    r = secured.post(
        f"{S}/runs/{run['run_uid']}/decision",
        json={"decision": "APPROVED", "rationale": "Approving stale terms."},
        headers=h("reviewer"),
    )
    assert r.status_code == 409 and "no longer active" in r.text


def test_runs_are_frozen_once_finished_and_replay_detects_changed_offers(engine, client, secured):
    w = world(client, secured)
    run = finished(secured, optimize(secured, w), "editor")
    with Session(engine) as s:
        r = s.scalar(select(SourcingRun).where(SourcingRun.run_uid == run["run_uid"]))
        r.result_status = "tampered"
        with pytest.raises(ValueError, match="immutable"):
            s.flush()
        s.rollback()
        s.delete(s.scalar(select(SourcingRun)))
        with pytest.raises(ValueError, match="cannot be deleted"):
            s.flush()
        s.rollback()
    rp = secured.post(f"{S}/runs/{run['run_uid']}/replay", headers=h("editor")).json()
    assert rp["snapshot_unchanged"] is True and rp["allocations_identical"] is True
    new = secured.post(f"{S}/offers", json=offer_body(w, "s3", 1.0, 500.0), headers=h("editor"))
    assert new.status_code == 201
    rp2 = secured.post(f"{S}/runs/{run['run_uid']}/replay", headers=h("editor")).json()
    assert (
        rp2["snapshot_unchanged"] is False and rp2["allocations_identical"] is False and "changed" in rp2["explanation"]
    )
    again = secured.get(f"{S}/runs/{run['run_uid']}", headers=h("editor")).json()
    assert (
        again["snapshot_hash"] == run["snapshot_hash"] and again["allocations"] == run["allocations"]
    )  # the stored result did not move


def test_cancel_and_active_run_limit(engine, client, secured):
    w = world(client, secured)
    req = SourcingRequest(**req_body(w))
    with Session(engine) as s, s.begin():
        run, _ = sruns.create(s, req, "editor-user", Limits())
        uid = run.run_uid
    c = secured.post(f"{S}/runs/{uid}/cancel", headers=h("editor"))
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    assert secured.post(f"{S}/runs/{uid}/cancel", headers=h("editor")).status_code == 409
    with Session(engine) as s, s.begin():
        for i in range(4):
            sruns.create(s, SourcingRequest(**req_body(w, quantity=10 + i)), "someone", Limits())
        uid2 = s.query(SourcingRun).filter_by(status=SimStatus.QUEUED).first().run_uid
    assert optimize(secured, w, quantity=77).status_code == 409
    assert secured.post(f"{S}/runs/{uid2}/cancel", headers=h("editor")).status_code == 403  # not the requester


def test_transport_route_requirement_uses_the_phase13_network(client, secured):
    w = world(client, secured)
    j = secured.post(f"{S}/alternatives", json=req_body(w, include_transport=True), headers=h("editor")).json()
    assert {c["transport"]["status"] for c in j["candidates"]} == {
        "NOT_EVALUATED"
    }  # no lanes, no supplier facilities: stated, not guessed
    req = secured.post(
        f"{S}/alternatives", json=req_body(w, constraints={"require_transport_route": True}), headers=h("editor")
    ).json()
    assert req["counts"] == {"MISSING_INFORMATION": 3}
    r = finished(secured, optimize(secured, w, constraints={"require_transport_route": True}))
    assert r["result_status"] == "incomplete" and r["allocations"] == []


def test_measured_reliability_needs_the_minimum_sample(monkeypatch):
    from app.leadtime.semantics import Delivery

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def d(i, sup, late):
        promise = base + timedelta(days=10 * i + 5)
        return Delivery(key=f"k{i}", ref=f"R{i}", product_id=1, supplier_id=sup, destination_id=1, order_at=base + timedelta(days=10 * i), dispatched_at=base + timedelta(days=10 * i + 1), original_promised_at=promise, latest_promised_at=promise, arrived_at=promise + timedelta(days=2 if late else -1), received_at=promise + timedelta(days=2 if late else -1), reported_status="DELIVERED")  # fmt: skip

    ds = [d(i, 1, late=(i % 5 == 0)) for i in range(25)] + [d(100 + i, 2, late=False) for i in range(5)]
    monkeypatch.setattr(sdata, "load_deliveries", lambda *a, **k: ds)
    r = SourcingRequest(product_id=1, destination_facility_id=1, quantity=1, unit="kg", currency="USD")
    rel, _ = sdata.measured_reliability(None, r, {1, 2, 3}, NOW, 20, 1000)
    assert rel[1].value == pytest.approx(20 / 25) and rel[1].n == 25 and rel[1].interval[0] < 0.8 < rel[1].interval[1]
    assert rel[2].value is None and rel[2].n == 5 and "not used as a measurement" in rel[2].note
    assert rel[3].value is None and rel[3].n == 0
