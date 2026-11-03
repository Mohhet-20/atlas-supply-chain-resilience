"""SYNTHETIC cases. The solver is checked against exhaustive enumeration of integer allocations on small random problems."""

from __future__ import annotations

import itertools
import random

import pytest

from app.sourcing.solver import Item, check_allocation, largest_fulfilment, solve


def brute(items, demand, overage=0.0, min_n=1, max_n=None):
    top = int(demand * (1 + overage / 100.0) + 1e-9)
    choices = []
    for it in items:
        opts = [0]
        step = int(it.lot) if it.lot else 1
        for q in range(1, int(it.hi) + 1):
            if q >= it.lo and q % step == 0:
                opts.append(q)
        choices.append(opts)
    best = None
    for combo in itertools.product(*choices):
        tot = sum(combo)
        used = sum(1 for c in combo if c)
        if tot < demand or tot > top or used < max(1, min_n) or (max_n is not None and used > max_n):
            continue
        cost = sum(c * it.cost for c, it in zip(combo, items, strict=True))
        if best is None or cost < best - 1e-9:
            best = cost
    return best


def brute_largest(items, demand, min_n=1, max_n=None):
    choices = []
    for it in items:
        step = int(it.lot) if it.lot else 1
        choices.append([0] + [q for q in range(1, int(it.hi) + 1) if q >= it.lo and q % step == 0])
    best = 0
    for combo in itertools.product(*choices):
        tot = sum(combo)
        used = sum(1 for c in combo if c)
        if tot <= demand and used >= max(1, min_n) and (max_n is None or used <= max_n):
            best = max(best, tot)
    return best


def rand_items(rng, n, lots):
    out = []
    for i in range(n):
        hi = rng.randint(2, 9)
        lo = rng.randint(1, hi)
        lot = rng.choice([None, 2, 3]) if lots else None
        out.append(Item(f"s{i}", float(rng.randint(1, 9)), float(lo), float(hi), float(lot) if lot else None))
    return out


def test_single_cheapest_supplier_takes_everything():
    s = solve([Item("a", 1, 0, 10), Item("b", 2, 0, 10)], 8)
    assert s.status == "OPTIMAL" and s.quantities == {"a": 8.0} and s.objective == 8.0


def test_cheapest_supplier_capacity_constrained_spills_to_next():
    s = solve([Item("a", 1, 0, 5), Item("b", 2, 0, 10)], 8)
    assert s.quantities == {"a": 5.0, "b": 3.0} and s.objective == 11.0


def test_minimum_order_quantity_can_make_the_cheapest_unusable():
    # b is cheaper but its MOQ of 9 exceeds the demand, so it cannot be used at all
    s = solve([Item("a", 5, 0, 10), Item("b", 1, 9, 20)], 6)
    assert s.quantities == {"a": 6.0}


def test_minimum_order_quantity_forces_a_costlier_split():
    s = solve([Item("a", 1, 0, 6), Item("b", 2, 3, 10)], 8)
    assert s.quantities == {"a": 5.0, "b": 3.0}


def test_order_increments_and_infeasibility():
    s = solve([Item("a", 1, 2, 6, 2.0), Item("b", 2, 3, 5, 3.0)], 8)
    assert s.status == "INFEASIBLE"  # 2k + 3m never equals 8 with m = 1 and k <= 3
    ok = solve([Item("a", 1, 2, 6, 2.0), Item("b", 2, 3, 6, 3.0)], 9)
    assert ok.status == "OPTIMAL" and sum(ok.quantities.values()) == 9


def test_overage_allows_the_next_lot():
    assert solve([Item("a", 1, 2, 10, 5.0)], 7).status == "INFEASIBLE"
    s = solve([Item("a", 1, 5, 10, 5.0)], 7, overage_pct=50)
    assert s.status == "OPTIMAL" and s.quantities == {"a": 10.0}


def test_diversification_bounds_force_a_split():
    items = [Item("a", 1, 0, 10), Item("b", 2, 0, 10), Item("c", 3, 0, 10)]
    assert len(solve(items, 8, min_n=2).quantities) == 2
    assert solve(items, 8, min_n=4).status == "INFEASIBLE"
    assert solve([Item("a", 1, 0, 5), Item("b", 2, 0, 5)], 8, max_n=1).status == "INFEASIBLE"


def test_insufficient_capacity_is_infeasible_not_relaxed():
    assert solve([Item("a", 1, 0, 3), Item("b", 2, 0, 4)], 8).status == "INFEASIBLE"


def test_invalid_demand_raises():
    with pytest.raises(ValueError):
        solve([Item("a", 1, 0, 3)], 0)


@pytest.mark.parametrize("lots", [False, True])
def test_matches_exhaustive_enumeration_on_random_problems(lots):
    rng = random.Random(14 if lots else 41)
    checked = 0
    for _ in range(120):
        items = rand_items(rng, rng.randint(2, 4), lots)
        demand = rng.randint(2, 14)
        overage = rng.choice([0, 0, 100])
        min_n = rng.choice([1, 1, 2])
        max_n = rng.choice([None, None, 2, 3])
        if max_n is not None and max_n < min_n:
            continue
        want = brute(items, demand, overage, min_n, max_n)
        got = solve(items, demand, overage_pct=overage, min_n=min_n, max_n=max_n)
        if want is None:
            assert got.status == "INFEASIBLE", (items, demand, overage, min_n, max_n, got)
        else:
            assert got.status == "OPTIMAL", (items, demand, overage, min_n, max_n, got)
            assert got.objective == pytest.approx(want), (items, demand, overage, got)
            by = {it.key: it for it in items}
            # the returned allocation is itself valid under an independent re-check
            fixed = [Item(k, by[k].cost, by[k].lo, by[k].hi, by[k].lot) for k in by]
            assert check_allocation(fixed, got.quantities, demand, overage_pct=overage, min_n=min_n, max_n=max_n) == []
            checked += 1
    assert checked > 30


@pytest.mark.parametrize("lots", [False, True])
def test_largest_fulfilment_matches_enumeration(lots):
    rng = random.Random(7 if lots else 8)
    for _ in range(80):
        items = rand_items(rng, rng.randint(1, 4), lots)
        demand = rng.randint(3, 30)
        min_n = rng.choice([1, 2])
        got, exact = largest_fulfilment(items, demand, min_n=min_n)
        assert exact
        assert got == pytest.approx(brute_largest(items, demand, min_n)), (items, demand, min_n)


def test_node_budget_is_reported_not_hidden():
    items = [Item(f"s{i}", float(i % 7 + 1), 1.0, 5.0) for i in range(25)]
    s = solve(items, 60, max_nodes=50)
    assert s.status == "BOUND_EXCEEDED" and s.exact is False


def test_dp_grid_limit_is_reported():
    s = solve([Item("a", 1, 1, 1e9, 0.001)], 1e6, max_grid_units=1000)
    assert s.status == "BOUND_EXCEEDED"


def test_check_allocation_catches_every_violation_kind():
    items = [Item("a", 1, 3, 6, 3.0), Item("b", 2, 1, 5)]
    bad = check_allocation(items, {"a": 4.0, "b": 9.0, "z": 1.0}, 12, min_n=3, max_n=2)
    text = " ".join(bad)
    for frag in ("not a multiple", "exceeds the maximum", "not a candidate", "outside the allowed range", "at least 3"):
        assert frag in text
    assert check_allocation(items, {"a": 3.0, "b": -1.0}, 2)  # negative
