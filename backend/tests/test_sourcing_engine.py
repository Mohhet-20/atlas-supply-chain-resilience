"""SYNTHETIC cases for eligibility, ranking, allocation, scenarios and strategy comparison (no database)."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.sourcing import engine as eng
from app.sourcing import overlay as ov
from app.sourcing import scoring as sc
from app.sourcing.config import SupplierAdjustment, Weights
from app.sourcing.eligibility import assess
from tests.sourcing_helpers import LIM, NOW, brute_cost, cand, ctx, req


def status(c, r=None, **kw):
    r = r or req(**kw)
    return assess(c, ctx(r))["status"]


# ------------------------------------------------------------------------------------------------------------- eligibility
def test_approved_confirmed_offer_is_eligible():
    assert status(cand(1)) == "ELIGIBLE"


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"approval": "PENDING_QUALIFICATION"}, "ELIGIBLE_AFTER_QUALIFICATION"),
        ({"approval": "UNKNOWN"}, "MISSING_INFORMATION"),
        ({"approval": "DISQUALIFIED"}, "DISQUALIFIED"),
        ({"link": "NONE"}, "MISSING_INFORMATION"),
        ({"link": "PROPOSED"}, "MISSING_INFORMATION"),
        ({"link": "REJECTED"}, "DISQUALIFIED"),
        ({"supplier_status": "ON_HOLD"}, "UNAVAILABLE"),
        ({"supplier_status": "INACTIVE"}, "DISQUALIFIED"),
        ({"merged": True}, "DISQUALIFIED"),
        ({"offer_status": "SUSPENDED"}, "UNAVAILABLE"),
        ({"contract_to": NOW - timedelta(days=1)}, "DISQUALIFIED"),
        ({"contract_from": NOW + timedelta(days=5)}, "UNAVAILABLE"),
        ({"price": None}, "MISSING_INFORMATION"),
        ({"capacity": None}, "MISSING_INFORMATION"),
        ({"capacity": 0.0}, "UNAVAILABLE"),
        ({"unit": "lb"}, "MISSING_INFORMATION"),
        ({"unit": None}, "MISSING_INFORMATION"),
        ({"currency": "EUR"}, "MISSING_INFORMATION"),
        ({"moq": 500.0}, "UNAVAILABLE"),
        ({"has_offer": False}, "MISSING_INFORMATION"),
        ({"unavailable_reason": "x"}, "UNAVAILABLE"),
    ],
)
def test_eligibility_status_matrix(over, expected):
    assert status(cand(1, **over)) == expected


def test_most_severe_outcome_wins_and_evidence_is_complete():
    a = assess(cand(1, approval="DISQUALIFIED", price=None), ctx(req()))
    assert a["status"] == "DISQUALIFIED"
    rules = {e["rule"]: e["outcome"] for e in a["evidence"]}
    assert rules["approval"] == "FAIL" and rules["price"] == "UNKNOWN" and "unit price and currency" in a["missing"]


def test_missing_data_is_never_assumed_eligible():
    a = assess(cand(1, approval="UNKNOWN"), ctx(req()))
    assert a["status"] == "MISSING_INFORMATION" and a["missing"] == ["approval status"]


def test_request_filters_exclude_without_overriding():
    assert status(cand(1), constraints={"excluded_supplier_ids": [1]}) == "EXCLUDED"
    assert status(cand(1), constraints={"allowed_supplier_ids": [2]}) == "EXCLUDED"
    assert status(cand(1), constraints={"excluded_countries": ["testland"]}) == "EXCLUDED"
    assert status(cand(1, lead_days=30.0), constraints={"max_lead_time_days": 20}) == "EXCLUDED"
    assert status(cand(1, lead_days=None), constraints={"max_lead_time_days": 20}) == "MISSING_INFORMATION"
    assert status(cand(1, defect_rate=0.2), constraints={"max_defect_rate": 0.05}) == "EXCLUDED"
    assert status(cand(1, risk=0.9), constraints={"max_risk": 0.5}) == "EXCLUDED"
    assert status(cand(1, risk=None), constraints={"max_risk": 0.5}) == "MISSING_INFORMATION"
    assert status(cand(1, rel=0.5), constraints={"min_reliability": 0.9}) == "EXCLUDED"
    assert status(cand(1, rel=None), constraints={"min_reliability": 0.9}) == "MISSING_INFORMATION"


def test_deadline_is_converted_to_a_lead_time_limit():
    r = req(constraints={"required_by": NOW + timedelta(days=7)})
    assert assess(cand(1, lead_days=10.0), ctx(r))["status"] == "EXCLUDED"
    assert assess(cand(1, lead_days=5.0), ctx(r))["status"] == "ELIGIBLE"


def test_stale_commercial_data_is_unverifiable_only_when_a_limit_is_set():
    old = cand(1, observed_at=NOW - timedelta(days=400))
    assert status(old) == "ELIGIBLE"
    assert status(old, constraints={"max_data_age_days": 90}) == "MISSING_INFORMATION"
    assert status(cand(1, observed_at=None), constraints={"max_data_age_days": 90}) == "MISSING_INFORMATION"


def test_transport_route_requirement():
    c = cand(1, transport={"status": "NOT_REACHABLE"})
    r = {"constraints": {"require_transport_route": True}}
    assert status(c, **r) == "EXCLUDED"
    assert status(cand(1, transport={"status": "NOT_EVALUATED", "note": "x"}), **r) == "MISSING_INFORMATION"
    assert status(cand(1, transport={"status": "REACHABLE"}), **r) == "ELIGIBLE"


def test_inactive_product_or_destination():
    assert assess(cand(1), ctx(req(), product_ok=False, product_note="retired"))["status"] == "DISQUALIFIED"
    assert assess(cand(1), ctx(req(), destination_ok=False, destination_note="closed"))["status"] == "UNAVAILABLE"


# ---------------------------------------------------------------------------------------------------------------- ranking
def test_known_scores_match_hand_calculation():
    cs = [cand(1, 10.0, lead_days=20.0), cand(2, 20.0, lead_days=10.0), cand(3, 30.0, lead_days=30.0)]
    w = Weights(price=0.6, lead_time=0.4, reliability=0, quality=0, capacity=0, risk=0)
    scores, _, basis = sc.score_all(cs, w, 60.0)
    assert basis == "PURCHASE_ONLY"
    assert scores["OF-T001"]["score"] == pytest.approx(80.0)  # price norm 1, lead norm 0.5
    assert scores["OF-T002"]["score"] == pytest.approx(70.0)  # price 0.5, lead 1
    assert scores["OF-T003"]["score"] == pytest.approx(0.0)
    assert sc.rank(cs, scores) == ["OF-T001", "OF-T002", "OF-T003"]
    comps = {c["metric"]: c for c in scores["OF-T001"]["components"]}
    assert comps["price"]["direction"] == "lower" and comps["price"]["contribution"] == pytest.approx(60.0)


def test_missing_metric_scores_zero_and_flags_incomplete():
    cs = [cand(1, 10.0, lead_days=None), cand(2, 10.0, lead_days=10.0)]
    w = Weights(price=0.5, lead_time=0.5, reliability=0, quality=0, capacity=0, risk=0)
    scores, _, _ = sc.score_all(cs, w, 60.0)
    assert scores["OF-T001"]["complete"] is False and scores["OF-T001"]["missing_metrics"] == ["lead_time"]
    assert scores["OF-T001"]["score"] == pytest.approx(50.0) and scores["OF-T002"]["score"] == pytest.approx(100.0)
    assert scores["OF-T001"]["weight_coverage"] == pytest.approx(0.5)


def test_weights_are_validated_and_normalised():
    with pytest.raises(ValidationError):
        Weights(price=0, lead_time=0, reliability=0, quality=0, capacity=0, risk=0)
    with pytest.raises(ValidationError):
        Weights(price=-1)
    assert sum(
        Weights(price=2, lead_time=2, reliability=0, quality=0, capacity=0, risk=0).normalised().values()
    ) == pytest.approx(1.0)


def test_equal_scores_order_deterministically_by_price_then_code():
    cs = [cand(3, 10.0), cand(1, 10.0), cand(2, 9.0)]
    scores, _, _ = sc.score_all(cs, Weights(price=0, lead_time=1, reliability=0, quality=0, capacity=0, risk=0), 60.0)
    assert sc.rank(cs, scores) == ["OF-T002", "OF-T001", "OF-T003"]


def test_correlated_indicators_raise_a_warning_and_freight_is_all_or_nothing():
    _, warns, _ = sc.score_all([cand(1)], Weights(reliability=0.2, risk=0.2), 60.0)
    assert any("twice" in w for w in warns)
    _, _, b1 = sc.score_all([cand(1, freight=1.0), cand(2)], Weights(), 60.0)
    _, _, b2 = sc.score_all([cand(1, freight=1.0), cand(2, freight=2.0)], Weights(), 60.0)
    assert (b1, b2) == ("PURCHASE_ONLY", "PURCHASE_PLUS_FREIGHT")


def test_score_does_not_modify_candidates():
    c = cand(1)
    before = c.terms()
    sc.score_all([c], Weights(), 60.0)
    assert c.terms() == before


# ------------------------------------------------------------------------------------------------------------- allocation
def run(cands, **kw):
    r = req(**kw)
    return eng.plan(cands, ctx(r), r, LIM)


def qty(p):
    return {a["supplier_code"]: a["quantity"] for a in p["allocations"]}


def test_single_cheapest_supplier_satisfies_the_demand():
    p = run([cand(1, 10.0), cand(2, 12.0)])
    assert p["result_status"] == "feasible" and qty(p) == {"S001": 60.0}
    assert p["metrics"]["purchase_cost"] == 600.0 and p["metrics"]["hhi"] == 1.0
    assert all(c["passed"] for c in p["hard_constraint_checks"])


def test_capacity_constrained_cheapest_spills_over():
    p = run([cand(1, 10.0, 40.0), cand(2, 12.0, 100.0)])
    assert qty(p) == {"S001": 40.0, "S002": 20.0} and p["metrics"]["purchase_cost"] == 640.0
    assert p["metrics"]["max_supplier_share"] == pytest.approx(40 / 60, abs=1e-5)


def test_minimum_order_quantity_and_contract_ceiling():
    p = run([cand(1, 10.0, 100.0, max_allocation=25.0), cand(2, 12.0, 100.0, moq=40.0)])
    assert qty(p) == {"S001": 20.0, "S002": 40.0}  # S002's MOQ of 40 forces S001 down to 20
    p2 = run([cand(1, 10.0, 100.0, max_allocation=25.0), cand(2, 12.0, 100.0)])
    assert qty(p2) == {"S001": 25.0, "S002": 35.0}


def test_order_increment():
    p = run([cand(1, 10.0, 100.0, lot=25.0), cand(2, 12.0, 100.0)], quantity=60.0)
    assert qty(p)["S001"] % 25 == 0 and sum(qty(p).values()) == pytest.approx(60.0)
    inf = run([cand(1, 10.0, 100.0, lot=25.0)], quantity=60.0)
    assert inf["result_status"] == "infeasible" and inf["allocations"] == []
    ok = run([cand(1, 10.0, 100.0, lot=25.0)], quantity=60.0, constraints={"overage_pct": 25})
    assert ok["result_status"] == "feasible" and qty(ok) == {"S001": 75.0}


def test_insufficient_capacity_is_infeasible_with_a_structured_reason():
    p = run([cand(1, 10.0, 20.0), cand(2, 12.0, 30.0)])
    assert p["result_status"] == "infeasible" and p["allocations"] == [] and p["metrics"] is None
    codes = {r["code"] for r in p["infeasibility"]["reasons"]}
    assert "INSUFFICIENT_CAPACITY" in codes
    fact = next(r for r in p["infeasibility"]["reasons"] if r["code"] == "INSUFFICIENT_CAPACITY")["facts"]
    assert fact["shortfall"] == pytest.approx(10.0)


def test_partial_fulfilment_only_when_requested_and_never_labelled_feasible():
    cs = [cand(1, 10.0, 20.0), cand(2, 12.0, 30.0)]
    assert run(cs)["result_status"] == "infeasible"
    p = run(cs, fulfillment="PARTIAL")
    assert p["result_status"] == "partial" and sum(qty(p).values()) == pytest.approx(50.0)
    assert p["metrics"]["unmet_quantity"] == pytest.approx(10.0)
    assert p["infeasibility"]["unmet_quantity"] == pytest.approx(10.0)


def test_partial_mode_returns_feasible_when_the_demand_can_be_met():
    assert run([cand(1, 10.0, 100.0)], fulfillment="PARTIAL")["result_status"] == "feasible"


def test_ineligible_suppliers_are_never_used_even_if_cheaper():
    p = run([cand(1, 1.0, approval="PENDING_QUALIFICATION"), cand(2, 1.0, approval="DISQUALIFIED"), cand(3, 9.0)])
    assert qty(p) == {"S003": 60.0}
    st = {c["supplier_code"]: c["status"] for c in p["candidates"]}
    assert st == {"S001": "ELIGIBLE_AFTER_QUALIFICATION", "S002": "DISQUALIFIED", "S003": "ELIGIBLE"}


def test_no_eligible_supplier_with_missing_information_is_incomplete_not_infeasible():
    p = run([cand(1, approval="UNKNOWN")])
    assert p["result_status"] == "incomplete"
    assert p["infeasibility"]["information_that_would_help"]
    assert run([])["result_status"] == "infeasible"


def test_strategies():
    cs = [cand(1, 10.0, 100.0), cand(2, 11.0, 100.0), cand(3, 12.0, 100.0)]
    single = run(cs, strategy="SINGLE")
    dual = run(cs, strategy="DUAL")
    multi = run(cs, strategy="MULTI", constraints={"min_suppliers": 3})
    assert len(single["allocations"]) == 1 and len(dual["allocations"]) == 2 and len(multi["allocations"]) == 3
    # a split costs at least as much as the cheapest single source; DUAL uses the 1% default minimum share
    assert dual["metrics"]["purchase_cost"] >= single["metrics"]["purchase_cost"]
    assert dual["bounds"]["applied_defaults"] == {"min_supplier_share": 0.01}
    assert min(a["share"] for a in dual["allocations"]) >= 0.0099
    small = run([cand(1, 10.0, 40.0), cand(2, 11.0, 40.0)], strategy="SINGLE")
    assert small["result_status"] == "infeasible" and "NO_SINGLE_SOURCE" in {
        r["code"] for r in small["infeasibility"]["reasons"]
    }


def test_share_limits_force_diversification():
    cs = [cand(1, 10.0, 100.0), cand(2, 11.0, 100.0), cand(3, 12.0, 100.0)]
    p = run(cs, constraints={"max_supplier_share": 0.5})
    assert qty(p) == {"S001": 30.0, "S002": 30.0}
    q = run(cs[:1], constraints={"max_supplier_share": 0.5})
    assert q["result_status"] == "infeasible" and "SHARE_CAP" in {r["code"] for r in q["infeasibility"]["reasons"]}
    m = run(cs, constraints={"min_supplier_share": 0.4})
    assert all(a["share"] >= 0.4 - 1e-9 for a in m["allocations"])


def test_request_conflicts_are_reported_before_solving():
    assert eng.validate_request(req(strategy="SINGLE", constraints={"min_suppliers": 2}))
    assert eng.validate_request(req(constraints={"max_supplier_share": 0.3, "max_suppliers": 2}))
    assert eng.validate_request(req(constraints={"min_supplier_share": 0.6, "min_suppliers": 2}))
    assert not eng.validate_request(req())


def test_landed_cost_objective_uses_only_stated_freight():
    cs = [cand(1, 10.0, freight=8.0), cand(2, 12.0, freight=2.0), cand(3, 5.0)]  # S003 has no freight figure
    p = run(cs, objective="LOWEST_LANDED_COST")
    assert qty(p) == {"S002": 60.0}
    assert (
        "S003" not in qty(p)
        and "no explicit freight"
        in next(c for c in p["candidates"] if c["supplier_code"] == "S003")["objective_excluded"]
    )
    assert p["metrics"]["landed_cost_stated"] == pytest.approx(60 * 14)
    only = run([cand(1, 10.0)], objective="LOWEST_LANDED_COST")
    assert only["result_status"] == "incomplete" and only["allocations"] == []


def test_reliability_and_risk_objectives_need_evidence():
    cs = [cand(1, 10.0, rel=0.7, risk=0.1), cand(2, 14.0, rel=0.95, risk=0.6), cand(3, 9.0, rel=None, risk=None)]
    assert qty(run(cs, objective="RELIABILITY")) == {"S002": 60.0}
    assert qty(run(cs, objective="LOWEST_RISK")) == {"S001": 60.0}
    assert run([cand(1, rel=None)], objective="RELIABILITY")["result_status"] == "incomplete"


def test_balanced_follows_the_suitability_score():
    cs = [cand(1, 10.0, lead_days=40.0, defect_rate=0.1), cand(2, 12.0, lead_days=5.0, defect_rate=0.01)]
    p = run(
        cs,
        objective="BALANCED",
        weights={"price": 0.2, "lead_time": 0.4, "reliability": 0, "quality": 0.4, "capacity": 0, "risk": 0},
    )
    assert qty(p) == {"S002": 60.0}


def test_resilience_objective_minimises_the_largest_share():
    cs = [cand(1, 10.0, 100.0), cand(2, 11.0, 100.0), cand(3, 12.0, 100.0)]
    p = run(cs, objective="RESILIENCE")
    assert p["metrics"]["max_supplier_share"] == pytest.approx(1 / 3, abs=1e-4)
    assert p["metrics"]["minimised_max_share"] == pytest.approx(1 / 3, abs=1e-4)
    capped = run([cand(1, 10.0, 45.0), cand(2, 11.0, 100.0)], objective="RESILIENCE")
    assert capped["metrics"]["max_supplier_share"] == pytest.approx(0.5, abs=1e-4)


def test_hhi_is_per_requirement_and_country_grouped():
    cs = [cand(1, 10.0, 30.0, country="A"), cand(2, 11.0, 30.0, country="A"), cand(3, 12.0, 30.0, country="B")]
    m = run(cs)["metrics"]
    assert m["hhi"] == pytest.approx(3 * (1 / 3) ** 2, abs=1e-4) or m["hhi"] == pytest.approx(0.5, abs=1e-4)
    assert m["country_hhi"] >= m["hhi"] - 1e-9 and set(m["country_shares"]) <= {"A", "B"}
    assert "enterprise" in m["concentration_note"]


def test_engine_cost_matches_exhaustive_search_on_random_instances():
    rng = random.Random(14)
    done = 0
    for _ in range(60):
        n = rng.randint(2, 4)
        specs = [
            (float(rng.randint(5, 20)), rng.randint(0, 6), rng.randint(3, 12), rng.choice([None, None, 2, 3]))
            for _ in range(n)
        ]
        demand = rng.randint(4, 14)
        mn = rng.choice([1, 1, 2])
        cs = [
            cand(i + 1, p, float(hi), moq=float(max(lo, 1)) if lo else None, lot=float(lot) if lot else None)
            for i, (p, lo, hi, lot) in enumerate(specs)
        ]
        items = [(p, max(lo, 1) if lo else 1, hi, lot) for (p, lo, hi, lot) in specs]
        # strategy bounds with an explicit minimum share of 1 unit keep the model identical to the brute force
        kw = {"quantity": float(demand), "constraints": {"min_suppliers": mn, "min_supplier_share": 1.0 / demand}}
        r = req(**kw)
        p = eng.plan(cs, ctx(r), r, LIM)
        want = brute_cost(items, demand, mn)
        if want is None:
            assert p["result_status"] in ("infeasible", "incomplete"), (specs, demand, mn)
        else:
            assert p["result_status"] == "feasible", (specs, demand, mn, p["infeasibility"])
            assert p["metrics"]["purchase_cost"] == pytest.approx(want)
            done += 1
    assert done > 15


# ------------------------------------------------------------------------------------------------------------------ scenarios
def test_supplier_unavailable_selects_the_alternative_and_baseline_is_untouched():
    cs = [cand(1, 10.0), cand(2, 12.0)]
    snap = [c.terms() for c in cs]
    r = req()
    base = eng.plan(cs, ctx(r), r, LIM)
    dis, applied = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, unavailable=True)])
    scen = eng.plan(dis, ctx(r), r, LIM)
    assert qty(base) == {"S001": 60.0} and qty(scen) == {"S002": 60.0}
    assert [c.terms() for c in cs] == snap  # the baseline candidates were not modified
    cmp = eng.compare_results(base, scen, applied, [])
    assert cmp["suppliers_lost"] == ["S001"] and cmp["suppliers_gained"] == ["S002"]
    assert cmp["purchase_cost"]["change"] == pytest.approx(120.0) and cmp["feasibility_changed"] is False
    ex = eng.exposure(base, dis)
    assert ex["lost_quantity"] == 60.0 and ex["lost_share"] == 1.0


def test_capacity_lead_time_price_and_risk_changes():
    cs = [cand(1, 10.0, 100.0), cand(2, 12.0, 100.0)]
    r = req(constraints={"max_lead_time_days": 15, "max_risk": 0.5})
    dis, _ = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, capacity_multiplier=0.5)])
    assert qty(eng.plan(dis, ctx(r), r, LIM)) == {"S001": 50.0, "S002": 10.0}
    slow, _ = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, add_lead_days=10)])
    assert qty(eng.plan(slow, ctx(r), r, LIM)) == {"S002": 60.0}  # S001 now exceeds the 15-day limit
    pricey, _ = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, price_multiplier=2.0)])
    assert qty(eng.plan(pricey, ctx(r), r, LIM)) == {"S002": 60.0}
    risky, _ = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, risk_override=0.9)])
    assert qty(eng.plan(risky, ctx(r), r, LIM)) == {"S002": 60.0}


def test_no_feasible_alternative_reports_unmet_demand_rather_than_using_a_disqualified_supplier():
    cs = [cand(1, 10.0, 100.0), cand(2, 5.0, 100.0, approval="DISQUALIFIED")]
    r = req(fulfillment="PARTIAL")
    dis, applied = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, capacity_multiplier=0.5)])
    p = eng.plan(dis, ctx(r), r, LIM)
    assert (
        p["result_status"] == "partial"
        and qty(p) == {"S001": 50.0}
        and p["metrics"]["unmet_quantity"] == pytest.approx(10.0)
    )
    base = eng.plan(cs, ctx(r), r, LIM)
    cmp = eng.compare_results(base, p, applied, [])
    assert cmp["feasibility_changed"] and cmp["unmet_quantity"]["change"] == pytest.approx(10.0)


def test_strategy_comparison_reports_cost_of_diversification_and_scenario_exposure():
    cs = [cand(1, 10.0, 100.0), cand(2, 11.0, 100.0), cand(3, 12.0, 100.0)]
    r = req()
    dis, _ = ov.from_adjustments(cs, [SupplierAdjustment(supplier_id=1, unavailable=True)])
    cmp = eng.strategy_comparison(cs, ctx(r), r, LIM, dis)
    s = cmp["strategies"]
    assert s["SINGLE"]["cost_of_diversification_vs_cheapest"]["amount"] == 0.0
    assert s["DUAL"]["cost_of_diversification_vs_cheapest"]["amount"] > 0
    assert s["SINGLE"]["exposure_if_executed_unchanged"]["lost_share"] == 1.0
    assert s["DUAL"]["exposure_if_executed_unchanged"]["lost_share"] < 1.0
    assert all(v["reoptimised_under_scenario"]["result_status"] == "feasible" for v in s.values())
    no_scen = eng.strategy_comparison(cs, ctx(r), r, LIM, None)
    assert "exposure_if_executed_unchanged" not in no_scen["strategies"]["SINGLE"]


def test_candidate_terms_snapshot_is_stable():
    a, b = cand(1), cand(1)
    assert a.terms() == b.terms()
