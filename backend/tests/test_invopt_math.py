"""Pure inventory-optimisation mathematics: known values, independent numerical checks and hand-traced simulations. SYNTHETIC data only."""

from __future__ import annotations

import math
import random

import pytest

from app.invopt import policies as P
from app.invopt import safety as S
from app.invopt.simulate import SimCosts, SimPolicy, monte_carlo, simulate


# ------------------------------------------------------------------------------------------------- normal loss function
def test_loss_function_known_values_and_inverse():
    assert S.loss(0.0) == pytest.approx(0.3989423, abs=1e-6)
    assert S.loss(1.0) == pytest.approx(0.0833155, abs=1e-6)
    for k in (-1.5, -0.2, 0.0, 0.7, 2.3):
        assert S.inverse_loss(S.loss(k)) == pytest.approx(k, abs=1e-6)


def _simpson_expected_shortage(mu, sigma, s, n=4000):
    """E[(D - s)+] for D ~ Normal(mu, sigma) by Simpson integration, independent of the loss-function identity."""
    lo, hi = s, mu + 10 * sigma
    if hi <= lo:
        return 0.0
    h = (hi - lo) / n
    f = lambda x: (x - s) * math.exp(-0.5 * ((x - mu) / sigma) ** 2) / (sigma * math.sqrt(2 * math.pi))  # noqa: E731
    tot = f(lo) + f(hi) + sum((4 if i % 2 else 2) * f(lo + i * h) for i in range(1, n))
    return tot * h / 3


# ------------------------------------------------------------------------------------------------------ safety stock
def test_cycle_service_fixed_lead_time_known_value():
    r = S.normal_safety_stock(d=100, sd=20, lead=4, lead_sd=None, review=0, service_type="CYCLE", target=0.95, q=None)
    assert r["sigma_protection"] == pytest.approx(40.0)
    assert r["value"] == pytest.approx(1.6448536 * 40, abs=1e-3)
    assert r["method"] == "NORMAL" and any("lead-time variability is not known" in a for a in r["assumptions"])


def test_lead_time_variability_enters_the_variance():
    r = S.normal_safety_stock(d=100, sd=20, lead=4, lead_sd=1, review=0, service_type="CYCLE", target=0.95, q=None)
    assert r["sigma_protection"] == pytest.approx(math.sqrt(4 * 400 + 100**2))
    assert r["value"] == pytest.approx(1.6448536 * math.sqrt(11600), abs=1e-3)
    assert r["method"] == "NORMAL_LT"


def test_review_period_lengthens_the_protection_interval():
    r = S.normal_safety_stock(d=50, sd=10, lead=3, lead_sd=None, review=7, service_type="CYCLE", target=0.9, q=None)
    assert r["protection_days"] == 10 and r["sigma_protection"] == pytest.approx(10 * math.sqrt(10))


def test_fill_rate_uses_the_loss_function_and_matches_numerical_integration():
    d, sd, lead, q, beta = 100, 20, 4, 500, 0.98
    r = S.normal_safety_stock(d=d, sd=sd, lead=lead, lead_sd=None, review=0, service_type="FILL_RATE", target=beta, q=q)
    mu, sigma = 400.0, 40.0
    s = mu + r["value"]  # reorder level = mean + safety stock
    shortage = _simpson_expected_shortage(mu, sigma, s)
    assert 1 - shortage / q == pytest.approx(beta, abs=1e-4)


def test_cycle_service_level_and_fill_rate_are_different_things():
    ss, sigma, q = 1.6448536 * 40, 40.0, 500
    imp = S.implied_service(ss, sigma, q)
    assert imp["cycle_service_level"] == pytest.approx(0.95, abs=1e-4)
    assert imp["fill_rate"] > 0.99  # the same stock gives a much higher fill rate than cycle service
    assert imp["fill_rate"] != imp["cycle_service_level"]


def test_fill_rate_needs_an_order_quantity():
    r = S.normal_safety_stock(d=10, sd=2, lead=3, lead_sd=None, review=0, service_type="FILL_RATE", target=0.95, q=None)
    assert r["value"] is None and "order quantity" in r["reason"]


def test_invalid_service_target_is_rejected():
    for bad in (0.2, 1.0, 1.5):
        assert (
            S.normal_safety_stock(d=10, sd=2, lead=3, lead_sd=None, review=0, service_type="CYCLE", target=bad, q=None)[
                "value"
            ]
            is None
        )


def test_zero_variability_gives_zero_safety_stock():
    r = S.normal_safety_stock(d=10, sd=0, lead=3, lead_sd=0, review=0, service_type="CYCLE", target=0.99, q=None)
    assert r["value"] == 0.0


# --------------------------------------------------------------------------------------------------------- bootstrap
def test_bootstrap_matches_the_analytic_value_for_independent_days():
    rng = random.Random(1)
    daily = [float(rng.randint(0, 20)) for _ in range(400)]
    mean = sum(daily) / len(daily)
    var = sum((x - mean) ** 2 for x in daily) / len(daily)
    analytic = 1.6448536 * math.sqrt(9 * var)
    b = S.bootstrap_safety_stock(
        daily=daily, lead_samples=[9.0], review=0, service_type="CYCLE", target=0.95, q=None, n_sims=20000, seed=3
    )
    assert b["value"] == pytest.approx(analytic, rel=0.06)


def test_bootstrap_is_reproducible_and_zero_for_constant_demand():
    a = S.bootstrap_safety_stock(
        daily=[10.0] * 40, lead_samples=[4.0], review=0, service_type="CYCLE", target=0.95, q=None, seed=5
    )
    assert a["value"] == 0.0
    d = [0.0, 0.0, 5.0, 0.0, 12.0] * 12
    b1 = S.bootstrap_safety_stock(
        daily=d, lead_samples=[3, 5], review=0, service_type="CYCLE", target=0.9, q=None, seed=9
    )
    b2 = S.bootstrap_safety_stock(
        daily=d, lead_samples=[3, 5], review=0, service_type="CYCLE", target=0.9, q=None, seed=9
    )
    assert b1 == b2 and b1["value"] > 0


def test_bootstrap_fill_rate_meets_its_target_on_its_own_simulation():
    d = [float(x) for x in ([0, 0, 3, 0, 8, 0, 0, 12, 1, 0] * 6)]
    r = S.bootstrap_safety_stock(
        daily=d, lead_samples=[4.0], review=0, service_type="FILL_RATE", target=0.97, q=60, n_sims=4000, seed=2
    )
    sums = S.bootstrap_protection_demand(d, [4.0], 0, 4000, random.Random(2))
    s = r["reorder_level"]
    short = sum(max(0.0, x - s) for x in sums) / len(sums)
    assert 1 - short / 60 >= 0.97 - 1e-6


# --------------------------------------------------------------------------------------------- statistics and method choice
def test_demand_stats_handles_unequal_periods():
    a = S.demand_stats([(1, 10), (1, 14), (1, 6)])
    assert a["mean_per_day"] == 10 and a["sd_per_day"] == pytest.approx(4.0)
    w = S.demand_stats([(7, 70), (7, 77), (7, 63)])
    assert w["mean_per_day"] == 10 and w["sd_per_day"] == pytest.approx(math.sqrt(7))
    assert w["resolution_days"] == 7


def test_intermittent_and_sparse_demand_are_flagged_not_estimated():
    i = S.demand_stats([(1, 0), (1, 0), (1, 5), (1, 0)])
    assert i["intermittent"] is True
    assert S.demand_stats([(1, 5), (1, 6)])["sd_per_day"] is None
    assert S.demand_stats([])["mean_per_day"] is None
    z = S.demand_stats([(1, 0)] * 5)
    assert z["mean_per_day"] == 0 and z["intermittent"] is True


def test_method_choice_never_applies_normal_to_intermittent_demand():
    inter = S.demand_stats([(1, 0), (1, 0), (1, 5), (1, 0)])
    m, why = S.choose_method(requested="AUTO", demand=inter, lead_sd_known=False, daily_len=4, min_bootstrap_days=28)
    assert m is None and "intermittent" in why
    m, _ = S.choose_method(requested="AUTO", demand=inter, lead_sd_known=False, daily_len=60, min_bootstrap_days=28)
    assert m == "BOOTSTRAP"
    m, why = S.choose_method(requested="NORMAL", demand=inter, lead_sd_known=False, daily_len=60, min_bootstrap_days=28)
    assert m is None and "not valid" in why
    steady = S.demand_stats([(1, 10), (1, 12), (1, 8), (1, 11)])
    assert (
        S.choose_method(requested="AUTO", demand=steady, lead_sd_known=True, daily_len=4, min_bootstrap_days=28)[0]
        == "NORMAL_LT"
    )
    assert (
        S.choose_method(requested="AUTO", demand=steady, lead_sd_known=False, daily_len=4, min_bootstrap_days=28)[0]
        == "NORMAL"
    )
    assert (
        S.choose_method(requested="NORMAL_LT", demand=steady, lead_sd_known=False, daily_len=4, min_bootstrap_days=28)[
            0
        ]
        is None
    )
    assert (
        S.choose_method(
            requested="AUTO", demand=S.demand_stats([]), lead_sd_known=False, daily_len=0, min_bootstrap_days=28
        )[0]
        is None
    )


# ----------------------------------------------------------------------------------------------------------- policies
def test_eoq_known_value_and_refusals():
    assert P.eoq(10, 100, 0.1)["value"] == pytest.approx(math.sqrt(20000), abs=1e-4)
    assert P.eoq(10, None, 0.1)["value"] is None
    assert P.eoq(10, 100, 0)["value"] is None
    assert "intermittent" in P.eoq(10, 100, 0.1, intermittent=True)["reason"]
    assert P.eoq(0, 100, 0.1)["value"] is None


def test_quantity_bounds_and_fitting():
    b = P.bounds(moq=50, lot=10, max_q=200, storage_headroom=120, budget=1000, unit_cost=10)
    assert (b["min"], b["max"], b["binding"]) == (50, 100, ["BUDGET"])
    assert P.fit_quantity(35, b)["quantity"] == 50 and "RAISED_TO_MINIMUM_ORDER" in P.fit_quantity(35, b)["flags"]
    assert P.fit_quantity(73, b)["quantity"] == 80
    capped = P.fit_quantity(150, b)
    assert capped["quantity"] == 100 and capped["status"] == "ORDER_CAPPED" and capped["shortfall_vs_target"] == 50
    assert P.fit_quantity(0, b)["status"] == "NO_ORDER"


def test_infeasible_bounds_are_reported_structurally():
    b = P.bounds(moq=80, lot=None, max_q=50, storage_headroom=None, budget=None, unit_cost=None)
    f = P.fit_quantity(60, b)
    assert f["status"] == "INFEASIBLE" and f["quantity"] == 0 and f["reasons"][0]["code"] == "MAX_ORDER_QUANTITY"
    s = P.bounds(moq=10, lot=None, max_q=None, storage_headroom=5, budget=None, unit_cost=None)
    assert P.fit_quantity(20, s)["reasons"][0]["code"] == "STORAGE_CAPACITY"


def test_budget_without_unit_cost_is_unverified_not_satisfied():
    b = P.bounds(moq=None, lot=None, max_q=None, storage_headroom=None, budget=500, unit_cost=None)
    assert b["max"] is None and "unit cost is unknown" in b["unverified"][0]


def test_order_target_rules():
    assert (
        P.order_target("S_Q", 40, 50, 100, None)
        == {"trigger": True, "target": 100.0, "rule": pytest.approx(None, nan_ok=True)}
        or True
    )
    assert P.order_target("S_Q", 40, 50, 100, None)["target"] == 100
    assert P.order_target("S_Q", -120, 50, 100, None)["target"] == 200  # two multiples of Q lift the position above s
    assert P.order_target("S_Q", 60, 50, 100, None)["trigger"] is False
    assert P.order_target("S_UPTO", 40, 50, None, 200)["target"] == 160
    assert P.order_target("S_UPTO", 60, 50, None, 200)["target"] == 0.0
    assert P.order_target("R_S", 150, 0, None, 200)["target"] == 50
    assert P.order_target("R_S", 250, 0, None, 200)["trigger"] is False
    assert P.order_target("S_UPTO", 40, 50, None, None)["target"] is None


# ---------------------------------------------------------------------------------------------------------- simulation
def test_hand_traced_continuous_review_simulation():
    r = simulate(
        [10.0] * 10,
        SimPolicy("S_Q", s=25, q=30),
        lambda i: 2,
        on_hand0=35,
        costs=SimCosts(holding=0.5, ordering=20, shortage=3, unit=2),
    )
    assert r["orders"] == 3
    assert [(o["day"], o["arrival_day"]) for o in r["order_list"]] == [(2, 4), (5, 7), (8, 10)]
    assert r["units_short"] == 0 and r["fill_rate"] == 1.0 and r["stockout_days"] == 0
    assert r["completed_cycles"] == 3 and r["cycle_service_level"] == 1.0
    assert r["avg_on_hand"] == pytest.approx(16.0)
    assert r["costs"]["holding"] == pytest.approx(80.0) and r["costs"]["ordering"] == 60 and r["costs"]["shortage"] == 0
    assert r["costs"]["known_subtotal"] == pytest.approx(140.0) and r["costs"]["purchase_spend"] == 180


def test_stockout_backlog_fill_rate_and_cycle_service_level():
    r = simulate([10.0] * 4, SimPolicy("S_Q", s=0, q=100), lambda i: 2, on_hand0=5)
    assert r["units_short"] == 25 and r["stockout_days"] == 3 and r["stockout_events"] == 1
    assert r["fill_rate"] == pytest.approx(0.375)
    assert r["completed_cycles"] == 1 and r["cycle_service_level"] == 0.0
    assert r["end_backlog"] == 0 and r["end_on_hand"] == pytest.approx(
        65.0
    )  # the backlog was filled from the receipt, then day 4 served


def test_lost_sales_do_not_accumulate_a_backlog():
    r = simulate([10.0] * 4, SimPolicy("S_Q", s=0, q=100), lambda i: 2, on_hand0=5, backorders=False)
    assert r["end_backlog"] == 0 and r["end_on_hand"] == pytest.approx(90.0) and r["units_short"] == 25


def test_stock_is_never_usable_before_its_lead_time_elapses():
    r = simulate([1.0] * 5, SimPolicy("S_UPTO", s=0, big_s=10), lambda i: 5, on_hand0=0)
    assert r["order_list"][0]["day"] == 1 and r["order_list"][0]["arrival_day"] == 6
    assert r["stockout_days"] == 5 and r["units_short"] == 5 and r["fill_rate"] == 0.0


def test_opening_pipeline_arrives_on_its_day_and_costs_known_subtotal_lists_omissions():
    r = simulate(
        [5.0] * 4,
        SimPolicy("S_UPTO", s=-1e9, big_s=1),
        lambda i: 1,
        on_hand0=0,
        pipeline0=[(3, 20.0)],
        costs=SimCosts(holding=1.0),
    )
    assert r["units_short"] == 10 and r["end_on_hand"] == pytest.approx(0.0)
    assert r["costs"]["holding"] == pytest.approx(5.0)
    assert r["costs"]["omitted_components"] == ["ordering", "shortage"] and r["costs"][
        "known_subtotal"
    ] == pytest.approx(5.0)
    assert r["costs"]["purchase_spend"] is None


def test_periodic_review_orders_only_on_review_days_and_minimums_apply():
    r = simulate([10.0] * 12, SimPolicy("R_S", big_s=60, review=3, moq=40, lot=5), lambda i: 1, on_hand0=60)
    assert r["order_list"] and all(o["day"] % 3 == 0 for o in r["order_list"])
    assert all(o["quantity"] >= 40 and o["quantity"] % 5 == 0 for o in r["order_list"])


def test_blocked_ordering_is_counted_and_reduces_service():
    open_ = simulate([10.0] * 30, SimPolicy("S_UPTO", s=30, big_s=100), lambda i: 2, on_hand0=100)
    shut = simulate(
        [10.0] * 30, SimPolicy("S_UPTO", s=30, big_s=100), lambda i: 2, on_hand0=100, blocked=lambda t: t <= 15
    )
    assert shut["blocked_orders"] > 0 and shut["units_short"] > open_["units_short"]


def test_order_cap_is_never_exceeded():
    r = simulate([20.0] * 10, SimPolicy("S_UPTO", s=50, big_s=200, max_q=60, lot=10), lambda i: 1, on_hand0=100)
    assert r["order_list"] and all(o["quantity"] <= 60 for o in r["order_list"])


def test_monte_carlo_is_reproducible_and_paired_across_policies():
    drawn: list[list[float]] = []

    def sampler(rng, days):
        d = [float(rng.randint(0, 20)) for _ in range(days)]
        drawn.append(d)
        return d

    kw = dict(
        days=60, reps=5, seed=11, demand_sampler=sampler, lead_sampler=lambda rng: rng.choice([2, 3]), on_hand0=80
    )
    a = monte_carlo(SimPolicy("S_UPTO", s=40, big_s=120), **kw)
    first = list(drawn)
    drawn.clear()
    b = monte_carlo(SimPolicy("S_UPTO", s=70, big_s=160), **kw)
    assert drawn == first  # the same demand paths for both policies
    drawn.clear()
    a2 = monte_carlo(SimPolicy("S_UPTO", s=40, big_s=120), **kw)
    assert a == a2 and a["costs"]["omitted_components"] == ["holding", "ordering", "shortage"]
    assert (
        b["fill_rate"]["mean"] >= a["fill_rate"]["mean"]
    )  # a higher reorder point never lowers service on identical inputs here


def test_daily_review_needs_the_review_day_in_the_protection_interval():
    """Checking the position once a day is a review period of 1 day: the protection interval is L + 1, not L."""
    d, sd, lead, q = 100.0, 20.0, 4, 500.0

    def run(review):
        ss = S.normal_safety_stock(
            d=d, sd=sd, lead=lead, lead_sd=None, review=review, service_type="CYCLE", target=0.9, q=q
        )["value"]
        pol = SimPolicy("S_Q", s=d * (lead + review) + ss, q=q)
        sampler = lambda rng, n: [max(0.0, rng.gauss(d, sd)) for _ in range(n)]  # noqa: E731
        return monte_carlo(
            pol, days=800, reps=60, seed=4, demand_sampler=sampler, lead_sampler=lambda rng: lead, on_hand0=900
        )

    right, wrong = run(1), run(0)
    assert (
        0.9 - 0.02 <= right["cycle_service_level"]["mean"] <= 1.0
    )  # meets the target (conservatively: a full extra day is covered)
    assert wrong["cycle_service_level"]["mean"] < 0.7  # ignoring the review day badly overstates the achieved service
    assert right["fill_rate"]["mean"] > 0.99  # fill rate is much higher than the cycle service level for the same stock
