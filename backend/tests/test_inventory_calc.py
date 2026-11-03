"""Pure inventory calculation tests (synthetic numbers only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.inventory import calc as c

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def pos(**kw):
    base = dict(id=1, product_id=1, facility_id=1, unit="EA", observed_at=NOW - timedelta(hours=2), on_hand=100.0)
    base.update(kw)
    return c.Position(**base)


def q(p, **kw):
    args = dict(product_unit="EA", as_of=NOW, max_age_days=7, allocated_overlap="UNKNOWN")
    args.update(kw)
    return c.derive_quantities(p, **args)


def obs(i, start, days, qty, kind="CONSUMPTION", unit="EA"):
    s = TODAY - timedelta(days=start)
    return c.DemandObs(i, s, s + timedelta(days=days), qty, unit, kind)


def weekly(n=4, qty=70.0, **kw):
    return [obs(i, 7 * (n - i), 7, qty, **kw) for i in range(n)]


def rate(o, **kw):
    args = dict(unit="EA", as_of_date=TODAY, window_days=28, basis="AUTO", min_obs=3, min_coverage=0.5)
    args.update(kw)
    return c.demand_rate(o, **args)


# ------------------------------------------------------------------ quantities


def test_on_hand_without_reservations_is_usable_and_flags_unreported_components():
    r = q(pos())
    assert (r["usable"], r["available"]) == (100.0, 100.0)
    assert set(r["unreported"]) == {"reserved", "quarantined", "damaged"}
    assert any("overstated" in a for a in r["assumptions"])


def test_reserved_quarantined_damaged_are_each_subtracted_once():
    r = q(pos(reserved=10, quarantined=5, damaged=3))
    assert r["usable"] == 82.0 and r["unreported"] == []


def test_allocated_is_not_subtracted_when_overlap_unknown_or_included():
    for mode in ("UNKNOWN", "INCLUDED_IN_RESERVED"):
        r = q(pos(reserved=10, allocated=10), allocated_overlap=mode)
        assert r["usable"] == 90.0 and r["available"] == 90.0  # no double subtraction
        assert any("Allocated quantity is not subtracted" in a for a in r["assumptions"])


def test_allocated_is_subtracted_only_when_declared_distinct():
    r = q(pos(reserved=10, allocated=15), allocated_overlap="DISTINCT")
    assert r["usable"] == 90.0 and r["available"] == 75.0


def test_over_commitment_is_flagged_not_hidden():
    r = q(pos(allocated=130), allocated_overlap="DISTINCT")
    assert r["available"] == -30.0 and r["over_committed"]


def test_exclusions_exceeding_on_hand_are_inconsistent_and_nothing_is_subtracted():
    r = q(pos(reserved=80, quarantined=30))
    assert r["status"] == "INCONSISTENT" and r["usable"] is None


def test_in_transit_never_enters_on_hand_or_available():
    r = q(pos(in_transit=500))
    assert r["usable"] == 100.0 and r["available"] == 100.0
    assert r["source"]["in_transit"] == 500


def test_source_usable_is_preserved_next_to_the_calculated_value():
    r = q(pos(reserved=10, usable_source=95))
    assert r["usable"] == 90.0 and r["source"]["usable_source"] == 95
    assert any("Source usable" in w for w in r["warnings"])


def test_unit_mismatch_missing_negative_future_and_stale():
    assert q(pos(unit="KG"))["status"] == "INCOMPATIBLE_UNITS"
    assert q(pos(on_hand=None))["status"] == "MISSING_INVENTORY"
    assert q(pos(on_hand=-1))["status"] == "INCONSISTENT"
    assert q(pos(observed_at=NOW + timedelta(days=1)))["status"] == "FUTURE_DATED"
    old = q(pos(observed_at=NOW - timedelta(days=30)))
    assert old["stale"] and old["usable"] == 100.0


def test_zero_on_hand_is_valid_and_distinct_from_missing():
    r = q(pos(on_hand=0.0))
    assert r["status"] == "OK" and r["usable"] == 0.0


def test_backorders_reduce_position_not_available():
    r = q(pos(backordered=20))
    assert r["available"] == 100.0 and r["inventory_position_before_inbound"] == 80.0


# ------------------------------------------------------------------ demand


def test_regular_history_gives_a_documented_rate():
    r = rate(weekly())
    assert r["status"] == "calculable" and r["rate_per_day"] == 10.0
    assert r["usable_observations"] == 4 and r["missing_days"] == 0 and r["basis"] == "CONSUMPTION"


def test_missing_periods_are_not_read_as_zero_demand():
    o = [obs(0, 28, 7, 70), obs(1, 21, 7, 70), obs(2, 7, 7, 70)]  # 7 uncovered days
    r = rate(o)
    assert r["rate_per_day"] == 10.0 and r["missing_days"] == 7 and r["covered_days"] == 21


def test_zero_observed_demand_has_its_own_status():
    r = rate(weekly(qty=0.0))
    assert r["status"] == "zero_observed_demand" and r["rate_per_day"] == 0.0
    assert any("does not imply" in w for w in r["warnings"])


def test_sparse_history_is_insufficient():
    assert rate(weekly(n=2))["status"] == "insufficient_demand_history"
    assert rate([])["status"] == "insufficient_demand_history"
    assert (
        rate([obs(0, 3, 1, 5), obs(1, 2, 1, 5), obs(2, 1, 1, 5)])["status"] == "insufficient_demand_history"
    )  # 3 of 28 days


def test_incompatible_units_are_excluded_and_reported():
    r = rate(weekly(unit="KG"))
    assert r["status"] == "incompatible_units" and r["excluded"]["unit_mismatch"] == 4


def test_observations_outside_or_straddling_the_window_are_excluded():
    o = weekly() + [obs(9, 100, 7, 999), obs(10, 31, 7, 999)]
    r = rate(o)
    assert r["rate_per_day"] == 10.0
    assert r["excluded"]["outside_window"] == 1 and r["excluded"]["partial_window"] == 1


def test_window_is_configurable():
    o = weekly(n=4)
    assert rate(o, window_days=14, min_obs=2)["usable_observations"] == 2


def test_overlapping_periods_of_one_kind_are_rejected_not_double_counted():
    o = [obs(0, 14, 7, 70), obs(1, 12, 7, 70), obs(2, 5, 5, 50), obs(3, 28, 7, 70)]
    assert rate(o)["status"] == "conflicting_observations"


def test_one_kind_is_used_never_mixed():
    o = weekly(kind="CONSUMPTION") + [obs(20 + i, 7 * (4 - i), 7, 700, kind="SALES") for i in range(4)]
    r = rate(o)
    assert r["basis"] == "CONSUMPTION" and r["rate_per_day"] == 10.0
    assert rate(o, basis="SALES")["rate_per_day"] == 100.0


def test_negative_adjustments_are_netted_and_cannot_go_below_zero():
    o = weekly() + [obs(50, 20, 1, -28, kind="ADJUSTMENT")]
    assert rate(o)["rate_per_day"] == 9.0
    assert rate(weekly(qty=1.0) + [obs(51, 20, 1, -500, kind="ADJUSTMENT")])["status"] == "invalid_negative_total"


def test_intermittent_demand_is_flagged():
    o = [obs(0, 28, 7, 0), obs(1, 21, 7, 0), obs(2, 14, 7, 70), obs(3, 7, 7, 0)]
    r = rate(o)
    assert r["intermittent"] and r["status"] == "calculable"


def test_degenerate_period_is_ignored():
    bad = c.DemandObs(99, TODAY, TODAY, 5.0, "EA", "CONSUMPTION")
    assert rate(weekly() + [bad])["usable_observations"] == 4


# ------------------------------------------------------------------ days of inventory


def d(qty, dem):
    return c.days_of_inventory(qty, dem)


def test_doi_positive_inventory_and_demand():
    r = d(q(pos(on_hand=100)), rate(weekly()))
    assert r["status"] == "calculable" and r["days"] == 10.0


def test_doi_zero_inventory_with_demand_is_zero_days():
    r = d(q(pos(on_hand=0)), rate(weekly()))
    assert r["status"] == "zero_inventory" and r["days"] == 0.0


def test_doi_zero_demand_is_not_infinity():
    r = d(q(pos()), rate(weekly(qty=0)))
    assert r["status"] == "zero_observed_demand" and r["days"] is None


def test_doi_missing_stale_units_and_history():
    assert d(q(pos(on_hand=None)), rate(weekly()))["status"] == "missing_inventory"
    assert d(q(pos(observed_at=NOW - timedelta(days=30))), rate(weekly()))["status"] == "stale_inventory"
    assert d(q(pos()), rate(weekly(unit="KG")))["status"] == "incompatible_units"
    assert d(q(pos()), rate([]))["status"] == "insufficient_demand_history"
    assert d(q(pos(unit="KG")), rate(weekly()))["status"] == "incompatible_units"


def test_doi_over_committed_available_basis_is_zero():
    qq = q(pos(allocated=130), allocated_overlap="DISTINCT")
    assert c.days_of_inventory(qq, rate(weekly()), basis="available")["status"] == "zero_inventory"


# ------------------------------------------------------------------ policy


def test_lead_time_conversion():
    assert c.lead_time_to_days(2, "weeks") == 14
    assert c.lead_time_to_days(48, "hours") == 2
    with pytest.raises(ValueError):
        c.lead_time_to_days(1, "fortnights")
    with pytest.raises(ValueError):
        c.lead_time_to_days(-1, "days")


def test_policy_validation_rejects_inconsistent_thresholds():
    p = c.PolicyIn(1, 1, "EA", safety_stock=50, reorder_point=40, minimum=10, maximum=5, target=2)
    msgs = " ".join(c.policy_problems(p))
    assert "reorder_point cannot be below safety_stock" in msgs and "minimum cannot exceed maximum" in msgs
    assert c.policy_problems(c.PolicyIn(1, 1, "EA", service_level=1.0))
    assert not c.policy_problems(c.PolicyIn(1, 1, "EA", safety_stock=0, reorder_point=10))


def test_computed_reorder_point_and_missing_safety_stock_is_not_zero():
    assert c.computed_reorder_point(10, 5, 20)["value"] == 70
    r = c.computed_reorder_point(10, 5, None)
    assert r["value"] is None and "not assumed to be zero" in r["reason"]
    assert c.computed_reorder_point(10, 5, 0)["value"] == 50  # explicit zero is honoured


def test_policy_violations_and_configured_values_win_over_computed():
    pol = c.PolicyIn(1, 1, "EA", safety_stock=30, reorder_point=60, minimum=20, maximum=200, lead_time_days=5)
    assert c.evaluate_policy(pol, 25, 10)["violations"] == ["BELOW_SAFETY_STOCK", "AT_OR_BELOW_REORDER_POINT"]
    assert c.evaluate_policy(pol, 50, 10)["violations"] == ["AT_OR_BELOW_REORDER_POINT"]
    assert c.evaluate_policy(pol, 250, 10)["violations"] == ["ABOVE_MAXIMUM"]
    ev = c.evaluate_policy(pol, 100, 10)
    assert ev["violations"] == [] and ev["reorder_point_source"] == "CONFIGURED" and ev["reorder_point_used"] == 60


def test_different_policies_give_different_outcomes_for_the_same_quantity():
    a = c.PolicyIn(1, 1, "EA", safety_stock=10)
    b = c.PolicyIn(2, 1, "EA", safety_stock=90)
    assert c.evaluate_policy(a, 50, 5)["violations"] == []
    assert c.evaluate_policy(b, 50, 5)["violations"] == ["BELOW_SAFETY_STOCK"]


def test_missing_policy_is_reported():
    assert c.evaluate_policy(None, 50, 5)["violations"] == ["MISSING_POLICY"]


def test_computed_reorder_point_is_used_only_when_inputs_exist():
    pol = c.PolicyIn(1, 1, "EA", safety_stock=20, lead_time_days=5)
    ev = c.evaluate_policy(pol, 60, 10)
    assert (
        ev["reorder_point_used"] == 70
        and ev["reorder_point_source"] == "COMPUTED"
        and "AT_OR_BELOW_REORDER_POINT" in ev["violations"]
    )
    assert c.evaluate_policy(pol, 60, None)["reorder_point_used"] is None


def test_safety_stock_estimate_needs_all_inputs_and_labels_its_limits():
    assert c.estimate_safety_stock(10, None, 5, 0.95)["value"] is None
    r = c.estimate_safety_stock(10, 2.0, 4, 0.975)
    assert r["value"] == pytest.approx(1.959964 * 2.0 * 2, rel=1e-4) and r["provenance"] == "MODEL_ESTIMATE"
    assert "excluded" in r["caveat"]


# ------------------------------------------------------------------ projection


def R(i, days_ahead, qty, status="CONFIRMED", unit="EA"):
    return c.Receipt(i, qty, unit, TODAY + timedelta(days=days_ahead) if days_ahead is not None else None, status)


def cr(receipts, **kw):
    args = dict(as_of_date=TODAY, unit="EA", eligible=("CONFIRMED", "IN_TRANSIT"), horizon_days=30)
    args.update(kw)
    return c.classify_receipts(receipts, **args)


def test_constant_demand_hits_zero_on_the_expected_day():
    p = c.project(100, 10, [], 30, {})
    assert p["stockout_within_day"] == 10 and p["estimated_stockout_after_days"] == 10.0
    assert p["series"][10] == 0.0 and p["min_closing"] == 0.0 and p["unfilled_demand"] == 200.0


def test_eligible_receipt_before_crossing_prevents_stockout_in_horizon():
    use, _ = cr([R(1, 5, 300)])
    p = c.project(100, 10, use, 30, {})
    assert p["series"][5] == 350.0
    assert p["stockout_within_day"] is None


def test_receipt_after_crossing_does_not_rescue_the_stockout():
    use, _ = cr([R(1, 20, 1000)])
    p = c.project(100, 10, use, 30, {})
    assert (
        p["stockout_within_day"] == 10 and p["unfilled_demand"] == 90.0
    )  # days 11-19 unfilled; the day-20 receipt arrives too late


def test_no_replenishment_information_projects_without_receipts():
    p = c.project(60, 10, [], 30, {})
    assert p["receipts_counted"] == 0.0 and p["stockout_within_day"] == 6


def test_receipt_eligibility_rules_exclude_uncertain_supply_with_reasons():
    use, skip = cr(
        [
            R(1, 5, 10, "PLANNED"),
            R(2, 5, 10, "PROPOSED"),
            R(3, 5, 10, "DELAYED"),
            R(4, 5, 10, "UNKNOWN"),
            R(5, None, 10),
            R(6, -2, 10),
            R(7, 5, 10, unit="KG"),
            R(8, 99, 10),
            R(9, 3, 10),
            R(10, 4, 10, "IN_TRANSIT"),
        ]
    )
    assert {u["id"] for u in use} == {9, 10}
    why = {s["id"]: s["reason"] for s in skip}
    assert why[1] == "STATUS_PLANNED_NOT_ELIGIBLE" and why[5] == "NO_EXPECTED_DATE" and why[6] == "OVERDUE_OR_DUE_NOW"
    assert why[7] == "UNIT_MISMATCH" and why[8] == "BEYOND_HORIZON"


def test_planned_receipts_count_only_when_opted_in():
    use, _ = cr([R(1, 5, 10, "PLANNED")], eligible=("CONFIRMED", "IN_TRANSIT", "PLANNED"))
    assert len(use) == 1


def test_threshold_crossings_are_reported_per_threshold():
    p = c.project(100, 10, [], 30, {"safety_stock": 40, "reorder_point": 60})
    assert p["crossings"] == {"safety_stock": 6, "reorder_point": 4}
    assert c.project(30, 10, [], 5, {"safety_stock": 40})["crossings"]["safety_stock"] == 0  # already below


def test_projection_is_bounded_by_the_requested_horizon():
    p = c.project(10**9, 1, [], 7, {})
    assert len(p["series"]) == 8


def test_zero_opening_inventory_stocks_out_on_day_one():
    p = c.project(0, 5, [], 10, {})
    assert p["stockout_within_day"] == 0 and p["estimated_stockout_after_days"] == 0.0


def test_projection_is_deterministic():
    use, _ = cr([R(1, 5, 30), R(2, 12, 30)])
    assert c.project(100, 7, use, 30, {"safety_stock": 20}) == c.project(100, 7, list(use), 30, {"safety_stock": 20})


# ------------------------------------------------------------------ classification


def cls(position, dem, pol=None, proj=True, backordered=None, low=7):
    qq = q(position)
    qq["basis_value"] = qq["available"]
    dd = c.days_of_inventory(qq, dem)
    pe = c.evaluate_policy(pol, qq["available"], dem.get("rate_per_day"))
    pj = None
    if proj and dem.get("rate_per_day") is not None and qq["available"] is not None:
        pj = c.project(
            qq["available"], dem["rate_per_day"], [], 60, {"safety_stock": pol.safety_stock if pol else None}
        )
    return c.classify(qq, dem, dd, pe, pj, low_coverage_days=low, backordered=backordered)


def test_current_shortage_requires_a_policy_or_obligation():
    pol = c.PolicyIn(1, 1, "EA", safety_stock=150)
    r = cls(pos(on_hand=100), rate(weekly()), pol)
    assert r["status"] == "current_shortage" and r["basis"] == "POLICY"
    # low inventory alone, with no policy and no demand obligation, is not a shortage
    assert cls(pos(on_hand=2), rate([]), None)["status"] != "current_shortage"
    assert cls(pos(on_hand=0), rate(weekly()), None)["status"] == "current_shortage"
    assert cls(pos(on_hand=0), rate([]), None, backordered=5)["status"] == "current_shortage"


def test_projected_stockout_low_coverage_within_policy_and_unknown():
    pol = c.PolicyIn(1, 1, "EA", safety_stock=10, reorder_point=20)
    assert cls(pos(on_hand=300), rate(weekly()), pol)["status"] == "projected_stockout"  # 30 days < 60 day horizon
    assert cls(pos(on_hand=5000), rate(weekly()), pol)["status"] == "within_policy"
    assert cls(pos(on_hand=50), rate(weekly()), c.PolicyIn(1, 1, "EA", safety_stock=10, reorder_point=60))[
        "status"
    ] in ("low_coverage", "projected_stockout")
    assert cls(pos(on_hand=500), rate([]), pol)["status"] == "unknown"
    assert cls(pos(on_hand=500), rate([]), None)["status"] == "insufficient_data"


def test_unusable_inventory_is_insufficient_data_never_a_shortage():
    for p in (pos(on_hand=None), pos(unit="KG"), pos(observed_at=NOW - timedelta(days=30)), pos(reserved=500)):
        assert cls(p, rate(weekly()), c.PolicyIn(1, 1, "EA", safety_stock=50))["status"] == "insufficient_data"


# ------------------------------------------------------------------ scenario


def test_scenario_effect_does_not_quantify_qualitative_exposure():
    e = c.scenario_effect({"loss_pct": None, "impact_state": "POTENTIAL_EXPOSURE"}, [10], 60, 0)
    assert e["affected"] and not e["quantified"] and e["window"] == [0, 10]
    assert c.scenario_effect(None, [], 60, 0) == {"affected": False}
    assert (
        c.scenario_effect({"loss_pct": 50}, [None], 60, 0)["duration_known"] is False
    )  # unknown duration -> whole horizon


def test_apply_effect_modes():
    r = [{"id": 1, "day": 5, "quantity": 100.0}, {"id": 2, "day": 40, "quantity": 100.0}]
    eff = {"affected": True, "loss_pct": 40.0, "delay_days": 3, "window": [0, 10]}
    point = c.apply_effect(r, eff, "point")
    assert point[0]["quantity"] == 60.0 and point[0]["day"] == 8 and point[1]["quantity"] == 100.0
    assert [x["id"] for x in c.apply_effect(r, eff, "worst")] == [2]
    assert c.apply_effect(r, eff, "best") == r
    assert c.apply_effect(r, {"affected": False}, "point") == r
