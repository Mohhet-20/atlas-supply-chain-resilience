"""Resilience engine: known answers, constraints, infeasibility, timeout, Pareto, normalisation, scenarios, availability, reproducibility. Synthetic."""

from __future__ import annotations

import copy
import itertools
import json

import pytest
from pydantic import ValidationError

from app.resilience import engine, model, objectives
from app.resilience import pareto as PA
from app.resilience import plan as PL
from tests.resilience_helpers import LIM, objs, outage, req, snap, stress, sup


def run(s, r, **kw):
    return engine.compute(s, r, LIM, **kw)


def rec_alloc(res):
    return {a["supplier_code"]: a["quantity"] for a in res["recommendation"]["allocation"]}


TWO = [sup(1, 10.0, 100.0), sup(2, 12.0, 100.0)]


# --- 1 known answer, independently calculated ---------------------------------------------------------------------
def test_known_minimum_cost_matches_an_independent_calculation():
    r = run(snap(TWO), req(objectives=objs(COST=1)))
    assert r["result_status"] in ("completed", "completed_with_warnings")
    assert rec_alloc(r) == {"S1": 60.0} and r["recommendation"]["values"]["COST"] == 600.0


def test_share_cap_changes_the_answer_to_the_hand_calculated_split():
    r = run(snap(TWO), req(objectives=objs(COST=1), constraints={"max_supplier_share": 0.6}))
    # 60% of 60 = 36 from the cheaper supplier, 24 from the other: 36*10 + 24*12 = 648
    assert rec_alloc(r) == {"S1": 36.0, "S2": 24.0} and r["recommendation"]["values"]["COST"] == 648.0


def test_grid_optimum_equals_brute_force_over_the_same_grid():
    s = snap([sup(1, 10.0, 40.0), sup(2, 12.0, 100.0), sup(3, 15.0, 100.0)])
    r = run(s, req(objectives=objs(COST=1)))
    best = min(
        10 * a + 12 * b + 15 * c
        for a, b, c in itertools.product(range(0, 61, 6), repeat=3)
        if a + b + c == 60 and a <= 40
    )
    assert r["recommendation"]["values"]["COST"] == pytest.approx(best)


def test_exact_sourcing_reference_is_included_and_never_beaten_by_the_grid():
    s = snap(TWO, anchors=[{"name": "SOURCING_EXACT_MIN_COST", "optimality": "EXACT", "allocation": {"OF-1": 60.0}}])
    r = run(s, req(objectives=objs(COST=1)))
    assert r["statistics"]["anchors"][0]["name"] == "SOURCING_EXACT_MIN_COST"
    assert [x["anchor"] for x in r["alternatives"] if x["recommended"]] == ["SOURCING_EXACT_MIN_COST"]
    assert r["recommendation"]["values"]["COST"] == 600.0


# --- 2/3 trade-offs ------------------------------------------------------------------------------------------------
def test_lowest_cost_plan_violates_a_service_constraint_so_a_dearer_plan_is_chosen():
    sc = [outage("A out", "OF-1")]
    base = run(snap(TWO, scenarios=sc), req(objectives=objs(COST=1), stress=[stress("A out", supplier_id=1)]))
    assert (
        base["recommendation"]["values"]["COST"] == 600.0
    )  # unconstrained cheapest is all-A, which collapses when A is out
    c = run(snap(TWO, scenarios=sc), req(objectives=objs(COST=1, WORST_CASE_SERVICE=0), stress=[stress("A out", supplier_id=1)], plan_constraints={"min_worst_case_fulfilment": 0.9}))  # fmt: skip
    # outage of A: only B delivers, so B >= 54 of 60; cheapest such grid plan is 54 B + 6 A = 54*12 + 6*10 = 708
    assert c["recommendation"]["values"]["COST"] == 708.0 and c["recommendation"]["values"]["WORST_CASE_SERVICE"] >= 0.9


def test_the_most_resilient_plan_costs_more_than_the_cheapest():
    s = snap(TWO)
    cheap = run(s, req(objectives=objs(COST=1)))
    resil = run(s, req(objectives=objs(DISRUPTION_EXPOSURE=1)))
    assert (
        resil["recommendation"]["values"]["DISRUPTION_EXPOSURE"]
        < cheap["recommendation"]["values"]["DISRUPTION_EXPOSURE"]
    )
    assert resil["recommendation"]["values"]["COST"] > cheap["recommendation"]["values"]["COST"]


# --- 4/5 normalisation and weights ---------------------------------------------------------------------------------
def test_normalisation_is_scale_free_so_currency_units_do_not_change_the_choice():
    a = run(snap(TWO), req(objectives=objs(COST=1, CONCENTRATION=1)))
    big = [sup(1, 10000.0, 100.0), sup(2, 12000.0, 100.0)]
    b = run(snap(big), req(objectives=objs(COST=1, CONCENTRATION=1)))
    assert rec_alloc(a) == rec_alloc(b)
    for row in a["objectives"]["normalisation"]["ranges"]:
        assert row["ideal"] <= row["nadir"] or row["objective"] == "WORST_CASE_SERVICE"
    assert all(0 <= x <= 1 for alt in a["alternatives"] for x in alt["normalised"])


@pytest.mark.parametrize(
    "bad",
    [
        [{"id": "COST", "weight": -1}],
        [{"id": "COST", "weight": 0}],
        [{"id": "COST", "weight": float("inf")}],
        [{"id": "COST"}, {"id": "COST"}],
        [],
    ],
)
def test_invalid_weight_configurations_are_rejected(bad):
    with pytest.raises(ValidationError):
        req(objectives=bad)


def test_weights_change_the_recommendation_and_are_recorded():
    s = snap(TWO)
    cost = run(s, req(objectives=objs(COST=10, CONCENTRATION=1)))
    spread = run(s, req(objectives=objs(COST=1, CONCENTRATION=10)))
    assert rec_alloc(cost) != rec_alloc(spread)
    assert spread["objectives"]["weights"]["CONCENTRATION"] > spread["objectives"]["weights"]["COST"]
    assert spread["objectives"]["normalisation"]["method"].startswith("min-max")


# --- 6/7 hard constraints and infeasibility ------------------------------------------------------------------------
def test_every_alternative_passes_the_independent_validator():
    s = snap([sup(1, 10, 40, moq=10, lot=5), sup(2, 12, 100), sup(3, 14, 100, moq=20)])
    r = run(
        s, req(constraints={"max_supplier_share": 0.7, "min_suppliers": 2}, objectives=objs(COST=1, CONCENTRATION=1))
    )
    P = model.build_problem(s, req(constraints={"max_supplier_share": 0.7, "min_suppliers": 2}))
    assert r["alternatives"]
    for alt in r["alternatives"]:
        plan = {a["offer_uid"]: a["quantity"] for a in alt["allocation"]}
        assert all(
            c["passed"]
            for c in PL.structural(P, req(constraints={"max_supplier_share": 0.7, "min_suppliers": 2}), plan)
        )


def test_infeasible_when_total_capacity_is_below_demand_and_nothing_is_relaxed():
    r = run(snap([sup(1, 10, 20), sup(2, 12, 20)], demand=60), req())
    assert r["result_status"] == "infeasible" and r["recommendation"] is None and r["alternatives"] == []
    assert (
        r["diagnostics"]["eligible_capacity_total"] == 40
        and "diagnostic" in r["diagnostics"]["note"].lower()
        and "not relaxed" in r["diagnostics"]["note"].lower()
    )


def test_conflicting_limits_are_infeasible_with_diagnostic_relaxations_only():
    r = run(snap(TWO), req(plan_constraints={"max_hhi": 0.3}))  # two suppliers cannot go below HHI 0.5
    assert r["result_status"] == "infeasible"
    rel = r["diagnostics"]["relaxations"]
    assert (
        rel
        and rel[0]["diagnostic_only"] is True
        and rel[0]["best_achievable_among_valid_allocations"] == pytest.approx(0.5)
    )


def test_budget_ceiling_is_a_hard_limit():
    r = run(snap(TWO), req(objectives=objs(CONCENTRATION=1), plan_constraints={"budget_ceiling": 620}))
    assert r["recommendation"]["values"]["COST"] <= 620
    assert run(snap(TWO), req(plan_constraints={"budget_ceiling": 500}))["result_status"] == "infeasible"


def test_ineligible_and_over_capacity_suppliers_are_never_used():
    s = snap([sup(1, 5.0, 100, status="EXCLUDED"), sup(2, 12.0, 50), sup(3, 14.0, 100)])
    r = run(s, req(objectives=objs(COST=1)))
    for alt in r["alternatives"]:
        for a in alt["allocation"]:
            assert a["supplier_code"] != "S1" and (a["supplier_code"] != "S2" or a["quantity"] <= 50)


# --- 8 timeout is not infeasibility --------------------------------------------------------------------------------
def test_a_timeout_is_reported_as_timed_out_not_infeasible():
    def stop():
        raise PL.Budget

    r = run(snap([sup(i, 10 + i, 100) for i in range(1, 6)]), req(), stop=stop)
    assert r["result_status"] == "timed_out" and r["recommendation"] is None


# --- 9 Pareto ------------------------------------------------------------------------------------------------------
def test_dominated_points_are_removed_and_ties_kept_once():
    items = [("a", (1.0, 5.0)), ("b", (2.0, 2.0)), ("c", (3.0, 3.0)), ("d", (5.0, 1.0)), ("e", (2.0, 2.0))]
    front, ties = PA.nondominated(items)
    assert sorted(front) == ["a", "b", "d"] and ties == {"b": ["e"]}
    assert PA.dominates((1, 1), (2, 2)) and not PA.dominates((1, 2), (2, 1)) and not PA.dominates((1, 1), (1, 1))


def test_no_returned_pareto_member_is_dominated_by_another_feasible_plan():
    s = snap([sup(1, 10, 100, lead=5), sup(2, 12, 100, lead=2), sup(3, 9, 100, lead=15)])
    r = run(s, req(strategy="PARETO", objectives=objs(COST=1, DELIVERY_DELAY=1, CONCENTRATION=1)))
    assert r["recommendation"] is None and r["pareto"]["front_size"] >= 3
    vecs = {a["id"]: PA.vector(a["values"], ["COST", "DELIVERY_DELAY", "CONCENTRATION"]) for a in r["alternatives"]}
    for a, b in itertools.permutations(vecs, 2):
        assert not PA.dominates(vecs[a], vecs[b])
    assert "approximation" in r["pareto"] and "grid" in r["pareto"]["approximation"]


def test_a_maximised_objective_is_converted_correctly_for_dominance():
    assert PA.vector({"WORST_CASE_SERVICE": 0.9}, ["WORST_CASE_SERVICE"]) == (-0.9,)


# --- epsilon constraint ---------------------------------------------------------------------------------------------
def test_epsilon_constraint_minimises_cost_within_a_concentration_limit():
    r = run(
        snap(TWO),
        req(
            strategy="EPSILON_CONSTRAINT",
            primary_objective="COST",
            objectives=objs(COST=1, CONCENTRATION=1),
            epsilon_limits={"CONCENTRATION": 0.6},
        ),
    )
    # HHI = s^2 + (1-s)^2 <= 0.6 allows s <= 0.7236; the largest grid share is 0.7: 42*10 + 18*12 = 636
    assert r["recommendation"]["values"]["CONCENTRATION"] <= 0.6
    assert rec_alloc(r) == {"S1": 42.0, "S2": 18.0} and r["recommendation"]["values"]["COST"] == 636.0


def test_epsilon_limits_that_cannot_be_met_are_infeasible_and_not_weakened():
    r = run(
        snap(TWO),
        req(
            strategy="EPSILON_CONSTRAINT",
            primary_objective="COST",
            objectives=objs(COST=1, CONCENTRATION=1),
            epsilon_limits={"CONCENTRATION": 0.4},
        ),
    )
    assert r["result_status"] == "infeasible" and r["recommendation"] is None
    assert r["diagnostics"]["relaxations"][0]["diagnostic_only"] is True


def test_epsilon_request_validation():
    with pytest.raises(ValidationError):
        req(strategy="EPSILON_CONSTRAINT", objectives=objs(COST=1))  # no primary
    with pytest.raises(ValidationError):
        req(strategy="WEIGHTED_SUM", epsilon_limits={"COST": 1.0})


# --- 10 sensitivity ------------------------------------------------------------------------------------------------
def test_sensitivity_reports_which_decisions_are_stable_and_bounds_the_sweep():
    r = run(snap(TWO), req(objectives=objs(COST=3, CONCENTRATION=1), sensitivity={"enabled": True, "weights": True, "demand_multipliers": [0.9, 1.1], "capacity_multipliers": [0.5]}))  # fmt: skip
    sens = r["sensitivity"]
    assert sens["variants"] and len(sens["variants"]) <= LIM.max_sensitivity_variants
    assert "note" in sens and all(v["label"] for v in sens["variants"])
    assert any(v["parameter"] == "weight" for v in sens["variants"]) and any(
        v["parameter"] == "capacity" for v in sens["variants"]
    )


def test_a_capacity_collapse_shows_up_as_an_infeasible_variant():
    r = run(
        snap(TWO),
        req(sensitivity={"enabled": True, "weights": False, "demand_multipliers": [], "capacity_multipliers": [0.2]}),
    )
    assert "all supplier capacities x0.2" in r["sensitivity"]["first_infeasible"]


# --- 12/13 scenarios, fixed vs adaptive ------------------------------------------------------------------------
def test_supplier_outage_scenario_degrades_a_single_source_plan_and_spares_a_split_plan():
    P = model.build_problem(
        snap(TWO, scenarios=[outage("A out", "OF-1")]), req(stress=[stress("A out", supplier_id=1)])
    )
    single = model.scenario_results(P, {"OF-1": 60.0})[0]
    split = model.scenario_results(P, {"OF-1": 30.0, "OF-2": 30.0})[0]
    assert single["unmet"] == 60.0 and single["fulfilment"] == 0.0 and split["unmet"] == 30.0


def test_fixed_and_adaptive_modes_are_labelled_and_adaptive_can_only_help():
    sc = [outage("A out", "OF-1")]
    fixed = run(snap(TWO, scenarios=sc), req(objectives=objs(COST=1, WORST_CASE_SERVICE=1), stress=[stress("A out", supplier_id=1)], plan_constraints={"min_baseline_fulfilment": 1.0}))  # fmt: skip
    adapt = run(snap(TWO, scenarios=sc), req(objectives=objs(COST=1, WORST_CASE_SERVICE=1), stress=[stress("A out", supplier_id=1)], scenario_mode="ADAPTIVE", response_days=5))  # fmt: skip
    assert "Fixed plan" in fixed["scenarios"]["label"] and "Adaptive plan" in adapt["scenarios"]["label"]
    plan = {"OF-1": 60.0}
    Pf = model.build_problem(snap(TWO, scenarios=sc), req(stress=[stress("A out", supplier_id=1)]))
    Pa = model.build_problem(
        snap(TWO, scenarios=sc), req(stress=[stress("A out", supplier_id=1)], scenario_mode="ADAPTIVE", response_days=5)
    )
    assert model.scenario_results(Pa, plan)[0]["unmet"] < model.scenario_results(Pf, plan)[0]["unmet"]


def test_adaptive_response_is_limited_by_lead_time_plus_response_time():
    slow = [sup(1, 10, 100, lead=5), sup(2, 12, 100, lead=28)]
    Pa = model.build_problem(snap(slow, scenarios=[outage("A out", "OF-1")]), req(stress=[stress("A out", supplier_id=1)], scenario_mode="ADAPTIVE", response_days=5, horizon_days=30))  # fmt: skip
    assert (
        model.scenario_results(Pa, {"OF-1": 60.0})[0]["recovered"] == 0.0
    )  # 28 + 5 > 30 days: replacement cannot arrive in time


def test_adaptive_needs_a_response_time_and_fixed_rejects_one():
    with pytest.raises(ValidationError):
        req(scenario_mode="ADAPTIVE")
    with pytest.raises(ValidationError):
        req(response_days=3)


def test_opening_stock_covers_a_shortfall_before_any_recovery():
    P = model.build_problem(
        snap(TWO, scenarios=[outage("A out", "OF-1")], stock=25.0), req(stress=[stress("A out", supplier_id=1)])
    )
    r = model.scenario_results(P, {"OF-1": 60.0})[0]
    assert r["from_stock"] == 25.0 and r["unmet"] == 35.0


def test_demand_surge_and_capacity_drop_are_evaluated():
    sc = [{"name": "surge", "kind": "DEMAND_SURGE", "probability": None, "demand_multiplier": 1.5, "effects": {}, "outage": None, "applied": [], "not_applied": []},
          {"name": "cap", "kind": "CAPACITY_DROP", "probability": None, "demand_multiplier": 1.0, "outage": None, "applied": [], "not_applied": [], "effects": {"OF-1": {"available": True, "capacity": 20.0, "lead_days": 10.0}}}]  # fmt: skip
    P = model.build_problem(snap(TWO, scenarios=sc), req(stress=[stress("surge", "DEMAND_SURGE", demand_multiplier=1.5), stress("cap", "CAPACITY_DROP", capacity_multiplier=0.2)]))  # fmt: skip
    res = {r["name"]: r for r in model.scenario_results(P, {"OF-1": 30.0, "OF-2": 30.0})}
    assert res["surge"]["unmet"] == 30.0 and res["cap"]["unmet"] == 10.0


def test_largest_supplier_outage_is_resolved_against_each_plan():
    sc = [
        {
            "name": "largest",
            "kind": "SUPPLIER_OUTAGE",
            "probability": None,
            "demand_multiplier": 1.0,
            "effects": {},
            "outage": "LARGEST",
            "applied": [],
            "not_applied": [],
        }
    ]
    P = model.build_problem(snap(TWO, scenarios=sc), req(stress=[stress("largest")]))
    assert model.scenario_results(P, {"OF-1": 50.0, "OF-2": 10.0})[0]["failed_supplier"] == "OF-1"
    assert model.scenario_results(P, {"OF-1": 10.0, "OF-2": 50.0})[0]["failed_supplier"] == "OF-2"


# --- 14/15 missing inputs, unavailable objectives, probabilities ---------------------------------------------------
def test_missing_inputs_make_objectives_unavailable_instead_of_zero():
    s = snap([sup(1, None, 100, lead=None), sup(2, 12, 100, lead=None)])
    av = objectives.availability({"eligible": [x for x in s["suppliers"]], "has_stress": False})
    assert (
        not av["COST"]["available"]
        and not av["DELIVERY_DELAY"]["available"]
        and not av["WORST_CASE_SERVICE"]["available"]
        and av["CONCENTRATION"]["available"]
    )
    r = run(s, req(objectives=objs(COST=1, DELIVERY_DELAY=1, CONCENTRATION=1)))
    ids = {u["id"] for u in r["objectives"]["unavailable"]}
    assert ids == {"COST", "DELIVERY_DELAY"} and r["objectives"]["enabled"] == ["CONCENTRATION"]
    assert all(
        alt["values"]["COST"] is None or alt["values"]["COST"] == alt["values"]["COST"] for alt in r["alternatives"]
    )
    assert r["result_status"] == "completed_with_warnings"


def test_a_limit_that_needs_an_unavailable_input_is_invalid_input_not_ignored():
    r = run(
        snap([sup(1, 10, 100, lead=None), sup(2, 12, 100, lead=None)]), req(plan_constraints={"max_delay_days": 12})
    )
    assert r["result_status"] == "invalid_input"


def test_no_eligible_offer_is_insufficient_data():
    r = run(snap([sup(1, status="MISSING_INFORMATION")]), req())
    assert r["result_status"] == "insufficient_data" and r["recommendation"] is None


def test_scenario_probabilities_are_never_invented():
    sc = [outage("A out", "OF-1")]
    r = run(
        snap(TWO, scenarios=sc),
        req(objectives=objs(COST=1, WORST_CASE_SERVICE=1), stress=[stress("A out", supplier_id=1)]),
    )
    assert r["scenarios"]["weighted"] is False and r["scenarios"]["expected_unmet"] is None
    with pytest.raises(ValidationError):
        req(
            stress=[
                stress("a", supplier_id=1, probability=0.5),
                stress("b", kind="DEMAND_SURGE", demand_multiplier=1.5),
            ]
        )
    with pytest.raises(ValidationError):
        req(
            stress=[
                stress("a", supplier_id=1, probability=0.5),
                stress("b", kind="DEMAND_SURGE", demand_multiplier=1.5, probability=0.3),
            ]
        )
    ok = run(snap(TWO, scenarios=[outage("a", "OF-1", prob=0.4), outage("b", "OF-2", prob=0.6)]), req(objectives=objs(COST=1, WORST_CASE_SERVICE=1), stress=[stress("a", supplier_id=1, probability=0.4), stress("b", supplier_id=2, probability=0.6)]))  # fmt: skip
    assert ok["scenarios"]["weighted"] is True


# --- 16 baseline comparison edge cases -----------------------------------------------------------------------------
def test_comparison_handles_zero_baselines_and_unavailable_metrics():
    base = {"values": {"COST": 100.0, "UNMET_DEMAND": 0.0, "CONCENTRATION": None, "DISRUPTION_EXPOSURE": 10.0}}
    cand = {"values": {"COST": 90.0, "UNMET_DEMAND": 5.0, "CONCENTRATION": 0.5, "DISRUPTION_EXPOSURE": 10.0}}
    rows = {
        x["objective"]: x
        for x in engine._compare(base, cand, ["COST", "UNMET_DEMAND", "CONCENTRATION", "DISRUPTION_EXPOSURE"])
    }
    assert rows["COST"]["verdict"] == "better" and rows["COST"]["relative_pct"] == -10.0
    assert (
        rows["UNMET_DEMAND"]["verdict"] == "worse"
        and rows["UNMET_DEMAND"]["relative_pct"] is None
        and "zero" in rows["UNMET_DEMAND"]["caveat"]
    )
    assert rows["CONCENTRATION"]["verdict"] == "n/a" and rows["DISRUPTION_EXPOSURE"]["verdict"] == "unchanged"


def test_supplied_baseline_is_compared_and_a_missing_one_is_labelled_as_a_reference():
    s = snap(TWO)
    withb = run(
        s,
        req(
            objectives=objs(COST=1, CONCENTRATION=1),
            baseline=[{"offer_uid": "OF-1", "quantity": 30}, {"offer_uid": "OF-2", "quantity": 30}],
        ),
    )
    assert withb["comparison"]["reference"] == "SUPPLIED_BASELINE" and withb["comparison"]["rows"]
    without = run(s, req(objectives=objs(COST=1, CONCENTRATION=1)))
    assert (
        without["comparison"]["reference"] == "REFERENCE_COST_MINIMAL"
        and "not the current operation" in without["comparison"]["note"]
    )
    assert run(s, req(baseline=[{"offer_uid": "OF-9", "quantity": 1}]))["result_status"] == "invalid_input"


# --- 17/18 reproducibility, no mutation ----------------------------------------------------------------------------
def test_results_are_deterministic_and_the_snapshot_is_not_mutated():
    s = snap([sup(1, 10, 100), sup(2, 12, 100), sup(3, 11, 60)], scenarios=[outage("A out", "OF-1")])
    before = copy.deepcopy(s)
    r1 = run(
        s,
        req(
            objectives=objs(COST=2, CONCENTRATION=1, WORST_CASE_SERVICE=1),
            stress=[stress("A out", supplier_id=1)],
            sensitivity={"enabled": True},
        ),
    )
    r2 = run(
        s,
        req(
            objectives=objs(COST=2, CONCENTRATION=1, WORST_CASE_SERVICE=1),
            stress=[stress("A out", supplier_id=1)],
            sensitivity={"enabled": True},
        ),
    )
    assert s == before
    assert r1["recommendation_hash"] == r2["recommendation_hash"]
    assert json.dumps({k: v for k, v in r1.items() if k != "statistics"}, sort_keys=True, default=str) == json.dumps({k: v for k, v in r2.items() if k != "statistics"}, sort_keys=True, default=str)  # fmt: skip
    assert r1["solver"]["deterministic"] is True and r1["solver"]["random_seed"] is None


# --- 24 limits ----------------------------------------------------------------------------------------------------
def test_larger_inputs_are_bounded_and_the_truncation_is_disclosed():
    many = snap([sup(i, 10 + i * 0.1, 100) for i in range(1, 13)], demand=120)
    r = run(many, req(120, objectives=objs(COST=1, CONCENTRATION=1)))
    assert any(w["code"] == "SUPPLIERS_TRUNCATED" for w in r["warnings"]) and r["inexact"] is True
    assert r["statistics"]["generated"] <= LIM.max_candidates
    from dataclasses import replace

    small = engine.compute(
        snap([sup(i, 10 + i, 100) for i in range(1, 6)]), req(grid_steps=20), replace(LIM, max_candidates=500)
    )
    assert (
        any(w["code"] == "ENUMERATION_TRUNCATED" for w in small["warnings"]) and small["statistics"]["generated"] <= 500
    )


def test_request_limits_are_validated():
    with pytest.raises(ValidationError):
        req(grid_steps=500)
    with pytest.raises(ValidationError):
        req(organisation_id=3)  # no tenancy model: unknown fields are rejected
    with pytest.raises(ValidationError):
        req(sensitivity={"enabled": True, "demand_multipliers": [100]})
