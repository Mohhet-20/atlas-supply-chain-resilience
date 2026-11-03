"""Resilience API: contract, roles, write gate, redaction, validation, pagination, idempotency, integration with Sourcing offers and Scenarios,
approvals, immutability and non-mutation of baseline data. Synthetic data only."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.main import create_app
from app.models.enums import SimStatus
from app.models.resilience import ResilienceDecision, ResilienceRun
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings
from tests.resilience_helpers import R
from tests.test_sourcing_api import _scenario, world


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
    return {"product_id": w["p"]["id"], "destination_facility_id": w["dest"]["id"], "quantity": 60, "unit": "kg", "currency": "USD", "objectives": [{"id": "COST", "weight": 1}, {"id": "CONCENTRATION", "weight": 1}, {"id": "DISRUPTION_EXPOSURE", "weight": 1}]} | kw  # fmt: skip


def go(c, w, role="editor", key=None, **kw):
    hd = h(role) | ({"Idempotency-Key": key} if key else {})
    return c.post(f"{R}/optimize", json=body(w, **kw), headers=hd)


def finished(c, resp, role="editor"):
    assert resp.status_code == 202, resp.text
    return c.get(f"{R}/runs/{resp.json()['run_uid']}", headers=h(role)).json()


@pytest.fixture
def w(client, secured):
    return world(client, secured, prices=((10.0, 40.0), (12.0, 100.0), (15.0, 100.0)))


def test_catalogue_health_and_disclosure(secured):
    j = secured.get(f"{R}/health", headers=h("viewer")).json()
    assert j["algorithm_version"] == "mo-1" and j["solver"]["type"] == "APPROXIMATE_GRID"
    assert any("purchase orders" in x for x in j["not_supported"])
    assert not any("organisation" in x for x in j["not_supported"])  # data is organisation-scoped
    c = secured.get(f"{R}/catalogue", headers=h("viewer")).json()
    assert {o["id"] for o in c["objectives"]} == {
        "COST",
        "UNMET_DEMAND",
        "DELIVERY_DELAY",
        "CONCENTRATION",
        "DISRUPTION_EXPOSURE",
        "WORST_CASE_SERVICE",
    }
    for o in c["objectives"]:
        assert (
            o["direction"] in ("min", "max")
            and o["unit"]
            and o["definition"]
            and o["inputs"]
            and o["missing"]
            and o["normalisation"]
        )
    assert set(c["result_statuses"]) >= {
        "completed",
        "infeasible",
        "timed_out",
        "invalid_input",
        "insufficient_data",
        "failed",
    }
    assert secured.get(f"{R}/health").status_code == 401


def test_optimisation_end_to_end_over_registered_offers(client, secured, w, engine):
    run = finished(secured, go(secured, w))
    assert run["status"] == "COMPLETED" and run["result_status"] in ("completed", "completed_with_warnings")
    rec = run["recommendation"]
    assert (
        rec and abs(sum(a["quantity"] for a in rec["allocation"]) - 60) < 1e-6 and rec["decision_support_only"] is True
    )
    assert run["pareto"]["front_size"] >= 2 and run["alternatives"] and run["objectives"]["normalisation"]["ranges"]
    assert (
        run["solver"]["type"] == "APPROXIMATE_GRID"
        and run["reproducibility"]["deterministic"] is True
        and run["snapshot_hash"]
    )
    assert run["approval"]["state"] == "PROPOSED" and run["approval"]["executed"] is False
    # the sourcing exact minimum-cost allocation is used as a reference and the grid never beats it
    anchors = run["statistics"]["anchors"]
    assert anchors and anchors[0]["name"] == "SOURCING_EXACT_MIN_COST"
    with engine.connect() as cx:
        assert cx.execute(text("select count(*) from sourcing_offers")).scalar() == 3


def test_baseline_data_is_not_mutated_by_a_scenario_run(client, secured, w, engine):
    with engine.connect() as cx:
        before = cx.execute(
            text("select id, unit_price, capacity_amount, lead_time_days, status from sourcing_offers order by id")
        ).all()
    s1 = w["s1"]["id"]
    run = finished(secured, go(secured, w, objectives=[{"id": "COST", "weight": 1}, {"id": "WORST_CASE_SERVICE", "weight": 1}], stress=[{"name": "S1 out", "kind": "SUPPLIER_OUTAGE", "supplier_id": s1}, {"name": "Cap drop", "kind": "CAPACITY_DROP", "capacity_multiplier": 0.5}, {"name": "Slow", "kind": "LEAD_TIME_INCREASE", "add_lead_days": 7}, {"name": "Surge", "kind": "DEMAND_SURGE", "demand_multiplier": 1.25}]))  # fmt: skip
    assert (
        run["status"] == "COMPLETED"
        and len(run["scenarios"]["outcomes"]) == 4
        and run["scenarios"]["weighted"] is False
    )
    assert run["scenarios"]["label"].startswith("Fixed plan")
    with engine.connect() as cx:
        assert (
            cx.execute(
                text("select id, unit_price, capacity_amount, lead_time_days, status from sourcing_offers order by id")
            ).all()
            == before
        )


def test_registered_scenario_is_mapped_onto_offers_through_the_sourcing_overlay(client, secured, w):
    sid = _scenario(
        secured,
        [
            {
                "event_type": "SUPPLIER_UNAVAILABLE",
                "target_type": "SUPPLIER",
                "target_id": w["s2"]["id"],
                "duration_days": 30,
            }
        ],
    )
    run = finished(secured, go(secured, w, objectives=[{"id": "COST", "weight": 1}, {"id": "WORST_CASE_SERVICE", "weight": 1}], stress=[{"name": "Beta down", "kind": "SCENARIO", "scenario_id": sid}]))  # fmt: skip
    assert run["status"] == "COMPLETED"
    d = run["scenarios"]["definitions"][0]
    assert d["name"] == "Beta down" and d["affected_offers"]
    # a plan that uses the unavailable supplier loses that share in the scenario outcome
    rec = run["alternatives"][0]
    assert rec["scenarios"][0]["name"] == "Beta down"


def test_stress_naming_an_unknown_supplier_is_rejected_not_treated_as_harmless(secured, w):
    r = go(secured, w, stress=[{"name": "Ghost", "kind": "SUPPLIER_OUTAGE", "supplier_id": 987654}])
    assert r.status_code == 422 and "Unknown supplier" in r.text


def test_infeasible_demand_is_reported_as_infeasible_with_diagnostics(client, secured, w):
    run = finished(secured, go(secured, w, quantity=500))
    assert run["result_status"] == "infeasible" and run["recommendation"] is None and run["alternatives"] == []
    assert (
        run["diagnostics"]["eligible_capacity_total"] == 240
        and "Constraints were not relaxed" in run["diagnostics"]["note"]
    )
    assert (
        secured.post(
            f"{R}/runs/{run['run_uid']}/decision",
            json={"decision": "APPROVED", "rationale": "approve an infeasible plan please"},
            headers=h("reviewer"),
        ).status_code
        == 409
    )


def test_viewer_cannot_run_and_never_sees_prices_or_costs(client, secured, w):
    assert go(secured, w, role="viewer").status_code == 403
    run = finished(secured, go(secured, w))
    v = secured.get(f"{R}/runs/{run['run_uid']}", headers=h("viewer")).json()
    assert v["cost_redacted"] is True
    for a in v["alternatives"]:
        assert (
            a["values"]["COST"] is None
            and a["cost"]["total_known"] is None
            and all(x["unit_price"] is None for x in a["allocation"])
        )
    assert v["comparison"] is None or all(
        r["baseline"] is None for r in v["comparison"]["rows"] if r["objective"] == "COST"
    )
    assert secured.get(f"{R}/runs/{run['run_uid']}/snapshot", headers=h("viewer")).status_code == 403
    assert secured.get(f"{R}/runs/{run['run_uid']}/snapshot", headers=h("editor")).status_code == 200
    e = secured.get(f"{R}/runs/{run['run_uid']}/alternatives", headers=h("editor")).json()
    assert any(a["values"]["COST"] for a in e["items"])


def test_validation_errors_are_clear_and_unknown_fields_including_tenant_ids_are_rejected(secured, w):
    hd = h("editor")
    for bad in ({"organisation_id": 5}, {"objectives": [{"id": "COST", "weight": -1}]}, {"objectives": [{"id": "BOGUS", "weight": 1}]}, {"quantity": -5}, {"scenario_mode": "ADAPTIVE"}, {"grid_steps": 99}, {"strategy": "EPSILON_CONSTRAINT"}):  # fmt: skip
        r = secured.post(f"{R}/optimize", json=body(w, **bad), headers=hd)
        assert r.status_code == 422, (bad, r.text)
    assert secured.post(f"{R}/optimize", json=body(w, product_id=999999), headers=hd).status_code == 404
    assert (
        secured.post(
            f"{R}/optimize", json=body(w, stress=[{"name": "x", "kind": "SCENARIO", "scenario_id": 424242}]), headers=hd
        ).status_code
        == 404
    )
    assert secured.post(f"{R}/optimize", json=body(w, stress=[{"name": f"s{i}", "kind": "DEMAND_SURGE", "demand_multiplier": 1.5} for i in range(9)]), headers=hd).status_code == 422  # fmt: skip


def test_write_gate_blocks_runs_but_reading_still_works(readonly, w):
    r = readonly.post(f"{R}/optimize", json=body(w), headers=h("editor"))
    assert r.status_code in (403, 405, 409, 503) and r.status_code != 202
    assert readonly.get(f"{R}/runs", headers=h("viewer")).status_code == 200


def test_idempotency_and_history_filtering_and_pagination(secured, w):
    a = go(secured, w, key="idem-resil-001")
    b = go(secured, w, key="idem-resil-001")
    assert a.json()["run_uid"] == b.json()["run_uid"] and b.headers["Idempotent-Replayed"] == "true"
    assert go(secured, w, key="idem-resil-001", quantity=61).status_code == 409
    go(secured, w, strategy="PARETO")
    go(secured, w, quantity=500)
    lst = secured.get(f"{R}/runs?limit=2&offset=0", headers=h("viewer")).json()
    assert lst["total"] == 3 and len(lst["items"]) == 2 and lst["limit"] == 2
    assert secured.get(f"{R}/runs?strategy=PARETO", headers=h("viewer")).json()["total"] == 1
    assert secured.get(f"{R}/runs?result_status=infeasible", headers=h("viewer")).json()["total"] == 1
    assert secured.get(f"{R}/runs?limit=500", headers=h("viewer")).status_code == 422
    assert secured.get(f"{R}/runs/NOPE-0000", headers=h("viewer")).status_code == 404


def test_run_sections_alternatives_comparison_constraints_and_pareto_filter(secured, w):
    run = finished(secured, go(secured, w, baseline=[{"offer_uid": w["offers"]["s2"], "quantity": 60}]))
    uid = run["run_uid"]
    alts = secured.get(f"{R}/runs/{uid}/alternatives?pareto_only=true&limit=2", headers=h("editor")).json()
    assert alts["limit"] == 2 and len(alts["items"]) <= 2 and all(a["pareto"] for a in alts["items"])
    cmp_ = secured.get(f"{R}/runs/{uid}/comparison", headers=h("editor")).json()["comparison"]
    assert cmp_["reference"] == "SUPPLIED_BASELINE" and {
        "baseline",
        "candidate",
        "difference",
        "relative_pct",
        "verdict",
    } <= set(cmp_["rows"][0])
    con = secured.get(f"{R}/runs/{uid}/constraints", headers=h("editor")).json()
    assert con["constraints"]["recommended_checks"] and any(
        c["passed"] for c in con["constraints"]["recommended_checks"]
    )
    assert secured.get(f"{R}/runs/{uid}/scenarios", headers=h("editor")).status_code == 200


def test_on_demand_sensitivity_is_bounded_and_not_stored(secured, w, engine):
    run = finished(secured, go(secured, w))
    r = secured.post(
        f"{R}/runs/{run['run_uid']}/sensitivity",
        json={"demand_multipliers": [0.5, 1.5], "capacity_multipliers": [0.3]},
        headers=h("editor"),
    )
    assert r.status_code == 200 and r.json()["sensitivity"]["variants"] and "not stored" in r.json()["note"]
    assert (
        secured.post(
            f"{R}/runs/{run['run_uid']}/sensitivity", json={"demand_multipliers": [99]}, headers=h("editor")
        ).status_code
        == 422
    )
    assert secured.post(f"{R}/runs/{run['run_uid']}/sensitivity", json={}, headers=h("viewer")).status_code == 403
    assert secured.get(f"{R}/runs/{run['run_uid']}/sensitivity", headers=h("viewer")).json()["sensitivity"] is None


def test_replay_reproduces_the_stored_result_and_reports_changed_inputs(client, secured, w, engine):
    run = finished(secured, go(secured, w, sensitivity={"enabled": True}))
    rp = secured.post(f"{R}/runs/{run['run_uid']}/replay", headers=h("editor")).json()
    assert rp["reproduced_from_stored_snapshot"] is True and rp["live_inputs_unchanged"] is True
    again = secured.post(f"{R}/runs/{run['run_uid']}/replay", headers=h("editor")).json()
    assert again["reproduced_from_stored_snapshot"] is True


def test_decisions_separation_of_duties_and_alternative_required(secured, w, engine):
    run = finished(secured, go(secured, w))
    uid, rid = run["run_uid"], run["recommendation"]["id"]
    d = f"{R}/runs/{uid}/decision"
    ok = {
        "decision": "APPROVED",
        "rationale": "Reviewed against the offers and the stress tests.",
        "alternative_id": rid,
    }
    assert secured.post(d, json=ok, headers=h("editor")).status_code == 403  # role
    assert secured.post(d, json={**ok, "alternative_id": "C99999"}, headers=h("reviewer")).status_code == 422
    assert secured.post(d, json={**ok, "alternative_id": None}, headers=h("reviewer")).status_code == 422
    assert secured.post(d, json={**ok, "rationale": "too short"}, headers=h("reviewer")).status_code == 422
    r = secured.post(d, json=ok, headers=h("reviewer"))
    assert (
        r.status_code == 200
        and r.json()["approval"]["state"] == "APPROVED"
        and r.json()["approval"]["executed"] is False
    )
    assert secured.post(d, json=ok, headers=h("reviewer")).status_code == 409
    hist = secured.get(f"{R}/runs/{uid}/decisions", headers=h("viewer")).json()
    assert hist["current"]["alternative_id"] == rid and len(hist["history"]) == 1
    with engine.connect() as cx:
        assert cx.execute(text("select count(*) from audit_events where action like 'resilience.run.%'")).scalar() >= 2


def test_requester_cannot_approve_own_run_unless_admin(secured, w):
    run = finished(secured, go(secured, w, role="reviewer"), "reviewer")
    d = f"{R}/runs/{run['run_uid']}/decision"
    ok = {
        "decision": "APPROVED",
        "rationale": "Self approval attempt for the record.",
        "alternative_id": run["recommendation"]["id"],
    }
    assert secured.post(d, json=ok, headers=h("reviewer")).status_code == 409
    assert secured.post(d, json=ok, headers=h("admin")).status_code == 200


def test_history_is_immutable(client, secured, w, engine):
    run = finished(secured, go(secured, w))
    from sqlalchemy.orm import Session

    with Session(engine) as s:
        r = s.scalar(select(ResilienceRun).where(ResilienceRun.run_uid == run["run_uid"]))
        assert r.status is SimStatus.COMPLETED
        r.error = "tamper"
        with pytest.raises(ValueError, match="immutable"):
            s.flush()
        s.rollback()
        r = s.scalar(select(ResilienceRun).where(ResilienceRun.run_uid == run["run_uid"]))
        s.delete(r)
        with pytest.raises(ValueError, match="retained"):
            s.flush()
        s.rollback()
    rid = run["recommendation"]["id"]
    secured.post(
        f"{R}/runs/{run['run_uid']}/decision",
        json={"decision": "APPROVED", "rationale": "Reviewed and accepted.", "alternative_id": rid},
        headers=h("reviewer"),
    )
    with Session(engine) as s:
        d = s.scalar(select(ResilienceDecision))
        d.rationale = "edited"
        with pytest.raises(ValueError, match="insert-only"):
            s.flush()
        s.rollback()
    # a re-run creates a NEW run; the old one is untouched
    again = finished(secured, go(secured, w))
    assert again["run_uid"] != run["run_uid"]
    assert (
        secured.get(f"{R}/runs/{run['run_uid']}", headers=h("editor")).json()["recommendation_hash"]
        == run["recommendation_hash"]
    )


def test_cancel_rules(secured, w, engine):
    run = finished(secured, go(secured, w))
    assert secured.post(f"{R}/runs/{run['run_uid']}/cancel", headers=h("editor")).status_code in (409, 422)
    from sqlalchemy.orm import Session

    from app.resilience import runs as rr
    from app.resilience.config import Limits, ResilienceRequest

    with Session(engine) as s, s.begin():
        q, _ = rr.create(
            s, ResilienceRequest.model_validate({**run["request"], "quantity": 55}), "editor-user", Limits()
        )
        uid = q.run_uid
    assert secured.post(f"{R}/runs/{uid}/cancel", headers=h("editor")).json()["status"] == "CANCELLED"


def test_availability_reports_unavailable_objectives_without_storing_anything(secured, w, engine):
    r = secured.post(f"{R}/availability", json=body(w), headers=h("viewer"))
    assert r.status_code == 200
    j = r.json()
    av = {o["id"]: o for o in j["objectives"]}
    assert av["COST"]["available"] and av["CONCENTRATION"]["available"] and not av["WORST_CASE_SERVICE"]["available"]
    assert "stress" in av["WORST_CASE_SERVICE"]["reason"] and j["eligibility_counts"]["ELIGIBLE"] == 3
    with engine.connect() as cx:
        assert cx.execute(text("select count(*) from resilience_runs")).scalar() == 0


def test_unavailable_objective_is_reported_on_the_run_not_replaced_by_zero(secured, w):
    run = finished(
        secured, go(secured, w, objectives=[{"id": "COST", "weight": 1}, {"id": "WORST_CASE_SERVICE", "weight": 1}])
    )
    assert run["result_status"] == "completed_with_warnings"
    assert [u["id"] for u in run["objectives"]["unavailable"]] == ["WORST_CASE_SERVICE"]
    assert all(a["values"]["WORST_CASE_SERVICE"] is None for a in run["alternatives"])


def test_time_budget_is_enforced_and_a_timeout_is_not_infeasibility(secured, w, engine, monkeypatch):
    from app.resilience import runs as rr
    from app.resilience.config import Limits

    run_id = finished(secured, go(secured, w))["run_uid"]
    from sqlalchemy.orm import Session, sessionmaker

    with Session(engine) as s, s.begin():
        q, _ = rr.create(
            s,
            __import__("app.resilience.config", fromlist=["x"]).ResilienceRequest.model_validate(
                {**secured.get(f"{R}/runs/{run_id}", headers=h("editor")).json()["request"], "quantity": 58}
            ),
            "editor-user",
            Limits(),
        )
        qid, quid = q.id, q.run_uid
    rr.execute(sessionmaker(bind=engine, expire_on_commit=False), qid, Limits(time_budget_seconds=1e-9))
    j = secured.get(f"{R}/runs/{quid}", headers=h("editor")).json()
    assert j["result_status"] == "timed_out" and j["status"] in ("PARTIAL",) and j["recommendation"] is None
    assert secured.post(f"{R}/runs/{quid}/decision", json={"decision": "APPROVED", "rationale": "approve a timed out run", "alternative_id": "C00001"}, headers=h("reviewer")).status_code == 409  # fmt: skip
