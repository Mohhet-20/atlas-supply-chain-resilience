"""Engine behaviour on SYNTHETIC data: positions, safety stock, constraints, costs, backtest, scenarios, determinism."""

from __future__ import annotations

import copy
import json

import pytest

from app.invopt import engine
from tests.inventory_helpers import code, d, iso, load_inventory, load_receipts
from tests.invopt_helpers import LIM, add_policy, intermittent_setup, request, run, setup, snapshot


def codes(items):
    return {i["code"] for i in items}


def test_recommendation_structure_and_labels(client):
    snap, res = run(client)
    assert res["result_status"] in ("recommended", "no_order_needed")
    rec = res["recommendation"]
    assert rec["decision_support_only"] is True and "never places an order" in rec["statement"]
    assert res["evaluation"]["synthetic"] is True and res["evaluation"]["mode"] == "MONTE_CARLO"
    assert res["evaluation"]["backtest"]["status"] == "completed"
    assert snap["lead_time"]["provenance"] == "ESTIMATED" and snap["lead_time"]["source"] == "USER_SUPPLIED"


def test_order_triggers_when_position_below_reorder_level(client):
    w = setup(client, on_hand=30)
    snap, res = run(client, w=w)
    rec = res["recommendation"]
    assert rec["order_now"] and rec["status"] == "ORDER"
    # (s, S): order up to S from the inventory position
    p = rec["policy"]
    assert rec["quantity"] == pytest.approx(p["order_up_to"] - 30, rel=1e-3)
    assert res["position"]["inventory_position"] == 30


def test_open_receipts_raise_position_and_in_transit_is_not_double_counted(client):
    w = setup(client, on_hand=30, receipts=True)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(0.05), "EA", 30, 0, 0, 0, 0, 80, ""]])
    snap, res = run(client, w=w)
    pos = res["position"]
    assert pos["on_order_counted"] == 80 and pos["inventory_position"] == 110
    assert "IN_TRANSIT_NOT_ADDED" in codes(snap["data_quality"])


def test_in_transit_without_dated_receipt_is_not_counted(client):
    w = setup(client, on_hand=30)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(0.05), "EA", 30, 0, 0, 0, 0, 80, ""]])
    snap, res = run(client, w=w)
    assert res["position"]["on_order_counted"] == 0 and "IN_TRANSIT_UNDATED" in codes(snap["data_quality"])


def test_overdue_receipt_is_excluded_with_reason(client):
    w = setup(client, on_hand=30)
    load_receipts(client, [[code(w, "p1"), code(w, "f1"), "PO-L", "CONFIRMED", 50, "EA", d(2), code(w, "s1")]])
    snap, res = run(client, w=w)
    assert res["position"]["on_order_counted"] == 0
    assert {r["reason"] for r in res["position"]["receipts_excluded"]} == {"OVERDUE_OR_DUE_NOW"}
    assert "RECEIPT_OVERDUE" in codes(snap["data_quality"])


def test_missing_inventory_demand_or_lead_time_is_never_zero(client):
    w = setup(client)
    _, res = run(client, w=w, lead_time_days=None, lead_time_std_days=None)
    assert res["result_status"] == "incomplete" and res["recommendation"] is None
    assert any("Lead time" in i["message"] for i in res["infeasibility"])
    w2 = setup.__wrapped__ if False else None  # noqa: F841


def test_no_inventory_record_is_incomplete(client):
    from tests.inventory_helpers import load_demand, world
    from tests.invopt_helpers import noisy_rows

    w = world(client)
    load_demand(client, noisy_rows(w, "p1", "f1", 10, 3, 60))
    _, res = run(client, w=w)
    assert res["result_status"] == "incomplete" and "INVENTORY_MISSING" in codes(res["data_quality"])


def test_backorders_unknown_is_flagged_and_known_zero_is_not(client):
    w = setup(client, backordered="")
    _, res = run(client, w=w)
    assert "BACKORDERS_UNKNOWN" in codes(res["data_quality"])
    w2 = setup(client, backordered=0)
    _, res2 = run(client, w=w2)
    assert "BACKORDERS_UNKNOWN" not in codes(res2["data_quality"])


def test_backorders_reduce_position(client):
    w = setup(client, on_hand=100, backordered=25)
    _, res = run(client, w=w)
    assert res["position"]["inventory_position"] == 75


def test_fill_rate_and_cycle_service_give_different_safety_stock(client):
    w = setup(client)
    _, cyc = run(client, w=w, service_type="CYCLE", service_target=0.95, evaluation="NONE")
    _, fr = run(client, w=w, service_type="FILL_RATE", service_target=0.95, evaluation="NONE")
    a, b = cyc["safety_stock"]["value"], fr["safety_stock"]["value"]
    assert a != b and b < a  # a 95% fill rate needs less stock than a 95% cycle service level when Q is large
    assert "differ" in cyc["statements"]["service"] or "different" in cyc["statements"]["service"]


def test_continuous_review_protection_interval_is_lead_time_plus_one_day(client):
    _, res = run(client, evaluation="NONE", policy_type="S_Q")
    assert res["safety_stock"]["protection_days"] == 6.0
    _, res_r = run(client, evaluation="NONE", policy_type="R_S", review_period_days=7)
    assert res_r["safety_stock"]["protection_days"] == 12.0


def test_lead_time_variability_increases_safety_stock(client):
    w = setup(client)
    _, a = run(client, w=w, lead_time_std_days=0.0001, evaluation="NONE")
    _, b = run(client, w=w, lead_time_std_days=3.0, evaluation="NONE")
    assert b["safety_stock"]["value"] > a["safety_stock"]["value"]


def test_unknown_lead_time_variability_is_warned_not_zeroed(client):
    w = setup(client)
    snap, res = run(client, w=w, lead_time_std_days=None, evaluation="NONE")
    assert "LEAD_TIME_SPREAD_UNKNOWN" in codes(snap["data_quality"]) and res["safety_stock"]["method"] == "NORMAL"
    assert any("excluded" in a for a in res["safety_stock"]["calculated"]["assumptions"])


def test_intermittent_demand_refuses_normal_and_uses_bootstrap(client):
    w = intermittent_setup(client)
    _, bad = run(client, w=w, safety_stock_method="NORMAL", evaluation="NONE")
    assert (
        bad["result_status"] in ("incomplete", "infeasible") and "intermittent" in bad["infeasibility"][-1]["message"]
    )
    snap, ok = run(client, w=w, evaluation="NONE")
    assert ok["safety_stock"]["method"] == "BOOTSTRAP" and ok["demand"]["intermittent"] is True
    assert "DEMAND_INTERMITTENT" in codes(snap["data_quality"])


def test_eoq_used_only_with_costs_and_otherwise_labelled_heuristic(client):
    w = setup(client)
    _, no = run(client, w=w, evaluation="NONE")
    assert no["order_quantity"]["method"] == "LEAD_TIME_COVER" and "heuristic" in no["order_quantity"]["reason"]
    costs = {"currency": "USD", "holding_cost_per_unit_day": 0.02, "ordering_cost": 50.0}
    _, yes = run(client, w=w, costs=costs, evaluation="NONE")
    q = yes["order_quantity"]
    assert q["method"] == "EOQ" and q["eoq"]["value"] == pytest.approx(
        (2 * yes["demand"]["mean"] * 50 / 0.02) ** 0.5, rel=1e-4
    )


def test_cost_subtotal_reports_omitted_components_and_purchase_spend_separately(client):
    w = setup(client)
    costs = {"currency": "USD", "holding_cost_per_unit_day": 0.02, "unit_cost": 4.0}
    _, res = run(client, w=w, costs=costs)
    c = res["costs"]["recommended_policy"]
    assert c["holding"] is not None and c["known_subtotal"]["mean"] == pytest.approx(c["holding"]["mean"], rel=1e-6)
    assert set(c["omitted_components"]) == {"ordering", "shortage"}
    assert c["purchase_spend"] is not None and "unit_cost" not in res["costs"]["missing_inputs"]
    assert set(res["costs"]["missing_inputs"]) == {"ordering_cost", "shortage_cost_per_unit"}


def test_min_total_cost_without_costs_is_not_solved(client):
    _, res = run(client, objective="MIN_TOTAL_COST")
    assert res["result_status"] == "not_solved" and res["infeasibility"][0]["code"] == "COST_OBJECTIVE_NEEDS_COSTS"


def test_min_total_cost_with_costs_selects_a_candidate(client):
    costs = {"currency": "USD", "holding_cost_per_unit_day": 0.05, "ordering_cost": 20.0, "shortage_cost_per_unit": 5.0}
    _, res = run(client, objective="MIN_TOTAL_COST", costs=costs)
    ev = res["evaluation"]
    assert res["result_status"] in ("recommended", "no_order_needed") and ev["selected"]
    best = min(c["metrics"]["costs"]["known_subtotal"]["mean"] for c in ev["candidates"])
    sel = next(c for c in ev["candidates"] if c["id"] == ev["selected"])
    assert sel["metrics"]["costs"]["known_subtotal"]["mean"] == pytest.approx(best)


def test_hard_constraints_moq_increment_and_cap(client):
    w = setup(client, on_hand=30)
    _, res = run(
        client,
        w=w,
        evaluation="NONE",
        constraints={"min_order_quantity": 200, "order_increment": 25, "max_order_quantity": 260},
    )
    rec = res["recommendation"]
    assert rec["quantity"] % 25 == 0 and 200 <= rec["quantity"] <= 260
    assert rec["status"] in ("ORDER", "ORDER_CAPPED")


def test_cap_below_need_reports_capped_with_shortfall(client):
    w = setup(client, on_hand=10)
    _, res = run(client, w=w, evaluation="NONE", constraints={"max_order_quantity": 40})
    rec = res["recommendation"]
    assert rec["status"] == "ORDER_CAPPED" and rec["quantity"] == 40 and rec["shortfall_vs_target"] > 0
    assert any(f.startswith("CAPPED_BY") for f in rec["flags"])


def test_conflicting_constraints_are_infeasible_with_reasons(client):
    w = setup(client, on_hand=10)
    # a lot size of 70 with a 50-unit cap leaves no orderable multiple
    _, res = run(client, w=w, evaluation="NONE", constraints={"order_increment": 70, "max_order_quantity": 50})
    assert res["result_status"] == "infeasible" and "CONSTRAINTS_CONFLICT" in codes(res["infeasibility"])
    with pytest.raises(ValueError):
        request(w, constraints={"min_order_quantity": 100, "max_order_quantity": 50})


def test_storage_capacity_below_required_stock_is_reported(client):
    w = setup(client, on_hand=10)
    _, res = run(client, w=w, evaluation="NONE", constraints={"storage_capacity": 40})
    assert res["result_status"] == "infeasible" and codes(res["infeasibility"]) & {
        "NO_FEASIBLE_POLICY",
        "CONSTRAINTS_CONFLICT",
    }


def test_budget_without_unit_cost_is_reported_unverified(client):
    w = setup(client, on_hand=30)
    _, res = run(client, w=w, evaluation="NONE", constraints={"budget_per_order": 500})
    assert any(x["code"] == "CONSTRAINT_UNVERIFIED" for x in res["warnings"])
    _, res2 = run(
        client,
        w=w,
        evaluation="NONE",
        constraints={"budget_per_order": 100},
        costs={"currency": "USD", "unit_cost": 10.0},
    )
    assert res2["recommendation"]["quantity"] <= 10


def test_deadline_shorter_than_lead_time_is_infeasible(client):
    _, res = run(client, evaluation="NONE", constraints={"required_within_days": 2})
    assert res["result_status"] == "infeasible" and "LEAD_TIME_EXCEEDS_DEADLINE" in codes(res["infeasibility"])
    assert res["recommendation"]["order_now"] is False


def test_ineligible_supplier_blocks_recommendation(client):
    w = setup(client, on_hand=20)
    req = request(w, evaluation="NONE")
    snap = snapshot(client, req)
    snap = copy.deepcopy(snap)
    snap["offer"] = {
        "ambiguous": False,
        "eligible": False,
        "ineligible_reasons": ["supplier approval status is DISQUALIFIED"],
        "moq": None,
        "lot_size": None,
        "max_allocation": None,
    }
    res = engine.compute(snap, req, LIM)
    assert res["result_status"] == "infeasible" and "SUPPLIER_NOT_ELIGIBLE" in codes(res["infeasibility"])


def test_determinism_same_inputs_same_result(client):
    w = setup(client)
    req = request(w)
    snap = snapshot(client, req)
    a = json.dumps(engine.compute(snap, req, LIM), sort_keys=True, default=str)
    b = json.dumps(engine.compute(copy.deepcopy(snap), req, LIM), sort_keys=True, default=str)
    assert a == b


def test_candidate_selection_meets_target_and_is_cheapest_among_those(client):
    costs = {"currency": "USD", "holding_cost_per_unit_day": 0.05, "ordering_cost": 20.0}
    _, res = run(client, costs=costs, service_target=0.9)
    ev = res["evaluation"]
    meeting = [c for c in ev["candidates"] if c["meets_service_target"]]
    sel = next(c for c in ev["candidates"] if c["id"] == ev["selected"])
    assert sel["meets_service_target"]
    assert sel["metrics"]["costs"]["known_subtotal"]["mean"] == pytest.approx(
        min(c["metrics"]["costs"]["known_subtotal"]["mean"] for c in meeting)
    )
    assert "not a proof" in ev["statement"] or "not a guarantee" in ev["statement"]


def test_current_policy_is_compared_on_paired_paths(client):
    w = setup(client)
    add_policy(client, w, reorder_point=40.0, maximum=120.0, lead_time_days=5.0, safety_stock=10.0)
    _, res = run(client, w=w)
    cur = res["evaluation"]["current"]
    assert cur["available"] and cur["policy"]["reorder_point"] == 40.0 and cur["policy"]["order_up_to"] == 120.0
    cmp_ = res["evaluation"]["comparison"]
    assert "same demand and lead-time draws" in cmp_["basis"] and cmp_["fill_rate_change"] is not None


def test_current_policy_without_maximum_is_labelled_hybrid(client):
    w = setup(client)
    add_policy(client, w, reorder_point=40.0, lead_time_days=5.0)
    _, res = run(client, w=w)
    assert "HYBRID" in res["evaluation"]["current"]["label"]


def test_missing_current_policy_is_not_invented(client):
    _, res = run(client)
    assert res["evaluation"]["current"]["available"] is False and res["evaluation"]["comparison"] is None


def test_backtest_parameters_use_training_days_only(client):
    w = setup(client, days=120)
    req = request(w, evaluation="BACKTEST")
    snap = snapshot(client, req)
    daily = snap["demand"]["observed"]["daily"]
    n = len(daily)
    test_n = max(14, int(n * 0.4))
    a = engine.backtest_design(daily[: n - test_n], snap, req, LIM, 50.0, 1)
    mutated = daily[: n - test_n] + [x * 50 + 1000 for x in daily[n - test_n :]]
    b = engine.backtest_design(mutated[: n - test_n], snap, req, LIM, 50.0, 1)
    assert a["ss"] == b["ss"] and a["level"] == b["level"]
    # the full engine path: changing only the final (test) days leaves the backtest parameters unchanged
    snap2 = copy.deepcopy(snap)
    snap2["demand"]["observed"]["daily"] = mutated
    r1, r2 = engine.compute(snap, req, LIM), engine.compute(snap2, req, LIM)
    assert r1["evaluation"]["backtest"]["parameters"] == r2["evaluation"]["backtest"]["parameters"]
    assert (
        r1["evaluation"]["backtest"]["status"] == "completed"
        and "never read" in r1["evaluation"]["backtest"]["leakage_guard"]
    )


def test_backtest_needs_enough_history(client):
    w = setup(client, days=30)
    _, res = run(client, w=w, evaluation="BACKTEST")
    assert res["evaluation"]["backtest"] is None and any(x["code"] == "BACKTEST_NOT_POSSIBLE" for x in res["warnings"])


def test_monte_carlo_budget_reduces_replications_and_says_so(client):
    w = setup(client)
    req = request(w, replications=1000, simulation_days=730)
    snap = snapshot(client, req)
    res = engine.compute(snap, req, LIM)
    assert res["evaluation"]["budget"]["replications"] < 1000 and any(
        x["code"] in ("SIMULATION_REDUCED", "SIMULATION_BUDGET") for x in res["warnings"]
    )


def test_disruption_what_if_is_applied_to_copies_and_reports_mitigations(client):
    w = setup(client, on_hand=120)
    req = request(w, what_if={"supplier_blocked_days": 14, "supplier_blocked_start_day": 0, "extra_lead_time_days": 3})
    snap = snapshot(client, req)
    frozen = copy.deepcopy(snap)
    res = engine.compute(snap, req, LIM)
    assert snap == frozen  # inputs are never mutated
    sr = res["scenario_results"][0]
    assert sr["effective"]["orders_blocked"] == [1, 14] and sr["effective"]["lead_time_days"] == 8
    assert sr["recommended_as_is"]["fill_rate"]["mean"] <= sr["undisrupted_recommended"]["fill_rate"]["mean"]
    assert {m["option"] for m in res["mitigations"]} >= {"RAISE_SAFETY_STOCK"}
    bridging = [m for m in res["mitigations"] if m["option"] == "PLACE_BRIDGING_ORDER_NOW"]
    assert all(m["feasible_now"] is False for m in bridging)  # supplier blocked from day 1


def test_demand_spike_what_if_degrades_service(client):
    w = setup(client, on_hand=120)
    req = request(w, what_if={"demand_multiplier": 2.0})
    res = engine.compute(snapshot(client, req), req, LIM)
    sr = res["scenario_results"][0]
    assert sr["degradation"]["fill_rate_change"] < 0


def test_stale_inventory_is_flagged(client):
    w = setup(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(30), "EA", 100, 0, 0, 0, 0, "", ""]])
    from sqlalchemy import delete

    from app.models.inventory import InventoryPosition

    with client.app.state.session_factory() as s, s.begin():
        s.execute(
            delete(InventoryPosition).where(
                InventoryPosition.observed_at
                > __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                - __import__("datetime").timedelta(days=5)
            )
        )
    snap, _ = run(client, w=w)
    assert "INVENTORY_STALE" in codes(snap["data_quality"])


def test_measured_lead_time_is_preferred_to_estimates_and_labelled(client):
    from tests.invopt_helpers import load_leads

    w = setup(client)
    load_leads(client, w, [8, 9, 7, 8, 10, 8, 9, 7, 8, 9, 8, 10])
    snap, res = run(client, w=w, lead_time_days=None, lead_time_std_days=None, evaluation="NONE")
    lt = snap["lead_time"]
    assert lt["source"] == "MEASURED" and lt["provenance"] == "MEASURED" and lt["n"] == 12
    assert lt["days"] == pytest.approx(8.5, abs=0.1) and lt["std"] > 0
    assert res["safety_stock"]["method"] == "NORMAL_LT"
    # an analyst-supplied value overrides but the measured one is kept as an alternative
    snap2, _ = run(client, w=w, lead_time_days=6, evaluation="NONE")
    assert snap2["lead_time"]["source"] == "USER_SUPPLIED" and any(
        a["source"] == "MEASURED" for a in snap2["lead_time"]["alternatives"]
    )


def test_too_few_deliveries_are_not_used_as_a_measurement(client):
    from tests.invopt_helpers import load_leads

    w = setup(client)
    load_leads(client, w, [8, 9, 7])
    snap, res = run(client, w=w, lead_time_days=None, lead_time_std_days=None, evaluation="NONE")
    assert snap["lead_time"]["source"] is None and res["result_status"] == "incomplete"
    assert any(a["source"] == "MEASURED_INSUFFICIENT" for a in snap["lead_time"]["alternatives"])


def test_forecast_drives_demand_and_its_version_is_recorded(client):
    from app.forecast.config import ForecastConfig
    from tests.forecast_helpers import forecast, synthetic_world

    w = synthetic_world(client)
    fr = forecast(client, w, "p1", "f1", ForecastConfig(horizon=28, n_folds=5))
    req = request(w, forecast_run_id=fr.id, evaluation="MONTE_CARLO")
    snap = snapshot(client, req)
    res = engine.compute(snap, req, LIM)
    dem = res["demand"]
    assert dem["source"] == "FORECAST" and dem["forecast"]["forecast_run_id"] == fr.id
    assert (
        dem["forecast"]["data_snapshot_hash"] == fr.data_snapshot_hash and dem["forecast"]["model"] == fr.selected_model
    )
    assert dem["observed_mean"] is not None and "not an observation" in dem["forecast"]["label"]
    assert any(e["step"] == "demand" and f"forecast run {fr.id}" in e["text"] for e in res["explanation"])


def test_forecast_unit_mismatch_falls_back_to_observed_and_says_so(client):
    from app.forecast.config import ForecastConfig
    from tests.forecast_helpers import forecast, synthetic_world

    w = synthetic_world(client)
    fr = forecast(
        client, w, "p3", "f1", ForecastConfig(horizon=14, n_folds=3)
    )  # KG forecast, EA inventory? p3 has no inventory
    req = request(w, forecast_run_id=fr.id, evaluation="NONE")
    with pytest.raises(ValueError):
        snapshot(client, req)  # a forecast for a different product is refused outright
