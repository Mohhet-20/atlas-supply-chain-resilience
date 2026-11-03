"""Optimizer, constraints, objectives and baseline comparison on the pure engine. Synthetic data only."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.routing import engine as eng
from app.routing import overlay
from app.routing.config import Constraints, Limits, RouteRequest
from tests.routing_helpers import NOW, brute_paths, diamond, mk_ctx, mk_lane, mk_net, random_lanes

LIM = Limits()


def solve(lanes, ctx=None, **kw):
    kw.setdefault("origin_facility_id", 1)
    kw.setdefault("destination_facility_id", 4)
    kw.setdefault("alternatives", 10)
    req = RouteRequest(**kw)
    return eng.solve(mk_net(lanes), req, ctx or mk_ctx(lanes), LIM), req


def ids(res):
    return [r["metrics"]["lane_uids"] for r in res["routes"]]


def uids(*nums):
    return [f"LN-T{n:04d}" for n in nums]


def test_single_criterion_objectives_pick_the_documented_route():
    lanes = diamond()
    assert ids(solve(lanes, objective="LOWEST_COST")[0])[0] == uids(5)
    assert ids(solve(lanes, objective="FASTEST")[0])[0] == uids(3, 4)
    assert ids(solve(lanes, objective="MOST_RELIABLE")[0])[0] == uids(3, 4)
    res, _ = solve(lanes, objective="LOWEST_COST")
    assert res["search"]["optimality"] == "EXACT_WITHIN_HOP_LIMIT" and res["route_count"]["feasible"] == 4
    assert res["routes"][0]["metrics"]["cost"]["total"] == 150 and res["routes"][0]["rank"] == 1


def test_balanced_normalises_each_criterion_and_excludes_risk():
    res, req = solve(diamond(), objective="BALANCED")
    w = req.effective_weights()
    assert set(w) == {"cost", "duration", "reliability"} and abs(sum(w.values()) - 1) < 1e-12
    top = res["routes"][0]
    for p in top["score"]["parts"]:
        assert 0 <= p["normalised"] <= 1
    assert [p["criterion"] for p in top["score"]["parts"]] == ["cost", "duration", "reliability"]
    comps = [r["score"]["composite"] for r in res["routes"]]
    assert comps == sorted(comps)


def test_custom_weights_are_validated_and_normalised_without_double_counting():
    with pytest.raises(ValidationError):
        RouteRequest(origin_facility_id=1, destination_facility_id=4, objective="CUSTOM")
    with pytest.raises(ValidationError):
        RouteRequest(origin_facility_id=1, destination_facility_id=4, objective="CUSTOM", weights={"speed": 1})
    with pytest.raises(ValidationError):
        RouteRequest(origin_facility_id=1, destination_facility_id=4, objective="CUSTOM", weights={"cost": -1})
    with pytest.raises(ValidationError):
        RouteRequest(origin_facility_id=1, destination_facility_id=4, objective="CUSTOM", weights={"cost": 0})
    with pytest.raises(ValidationError):
        RouteRequest(origin_facility_id=1, destination_facility_id=4, objective="FASTEST", weights={"cost": 1})
    req = RouteRequest(
        origin_facility_id=1, destination_facility_id=4, objective="CUSTOM", weights={"cost": 3, "duration": 1}
    )
    assert req.effective_weights() == {"cost": 0.75, "duration": 0.25}
    only_cost = solve(diamond(), objective="CUSTOM", weights={"cost": 1})[0]
    assert ids(only_cost)[0] == uids(5)


def test_hard_constraints_remove_routes_and_are_never_relaxed():
    lanes = diamond()
    res, _ = solve(lanes, objective="LOWEST_COST", constraints={"max_transit_hours": 30})
    assert uids(5) not in ids(res)
    assert (
        res["rejections"]["cut_before_completion"] == {"max_transit_hours": 1} and res["search"]["pruned_branches"] == 1
    )
    res, _ = solve(lanes, objective="FASTEST", constraints={"max_cost": 1000, "currency": "USD"})
    assert uids(3, 4) not in ids(res) and ids(res)[0] == uids(1, 6, 4)
    none, _ = solve(lanes, constraints={"max_transit_hours": 5})
    assert none["status"] == "no_feasible_route" and none["routes"] == []
    inf = none["infeasibility"]
    assert (
        inf["network_connected"]
        and "No constraint was relaxed" in inf["relaxation"]
        and any("CONSTRAINTS" in r or "BOUNDS" in r for r in inf["reasons"])
    )


def test_mode_transfer_capacity_and_structure_constraints():
    lanes = diamond()
    assert all(
        "AIR" not in r["metrics"]["modes"] for r in solve(lanes, constraints={"prohibited_modes": ["AIR"]})[0]["routes"]
    )
    res, _ = solve(lanes, constraints={"required_modes": ["RAIL"]})
    assert ids(res) == [uids(1, 6, 4)]
    res, _ = solve(lanes, constraints={"allowed_modes": ["ROAD"]})
    assert ids(res) == [uids(1, 2)]
    res, _ = solve(lanes, constraints={"max_transfers": 0})
    assert ids(res) == [uids(5)]
    res, _ = solve(lanes, constraints={"min_capacity": 10, "capacity_unit": "t"})
    assert set(map(tuple, ids(res))) == {tuple(uids(5)), tuple(uids(1, 2))}
    res, _ = solve(lanes, constraints={"excluded_lanes": uids(5, 3)})
    assert set(map(tuple, ids(res))) == {tuple(uids(1, 2)), tuple(uids(1, 6, 4))}
    res, _ = solve(lanes, constraints={"excluded_facilities": [2]})
    assert set(map(tuple, ids(res))) == {tuple(uids(5)), tuple(uids(3, 4))}
    res, _ = solve(lanes, constraints={"required_facilities": [3]})
    assert set(map(tuple, ids(res))) == {tuple(uids(3, 4)), tuple(uids(1, 6, 4))}
    assert solve(lanes, constraints={"required_modes": ["PIPELINE"]})[0]["status"] == "no_feasible_route"


def test_deadline_window_and_transfer_wait_are_part_of_the_time_limit():
    from datetime import timedelta

    lanes = diamond()
    c = {"depart_at": NOW.isoformat(), "deadline_at": (NOW + timedelta(hours=20)).isoformat()}
    res, _ = solve(lanes, constraints=c)
    assert set(map(tuple, ids(res))) == {tuple(uids(3, 4)), tuple(uids(1, 6, 4))}
    res, _ = solve(lanes, constraints=c, transfer_hours=10)  # A-B-C-D now needs 18 + 20 hours
    assert ids(res) == [uids(3, 4)] and res["routes"][0]["metrics"]["duration"]["total_hours"] == 16
    with pytest.raises(ValidationError):
        Constraints(depart_at=NOW, deadline_at=NOW - timedelta(hours=1))
    with pytest.raises(ValidationError):
        Constraints(deadline_at=NOW)
    assert (
        Constraints(max_transit_hours=10, depart_at=NOW, deadline_at=NOW + timedelta(hours=5)).transit_limit_hours()
        == 5
    )


def test_missing_data_is_unverifiable_not_feasible_and_not_zero():
    lanes = diamond()
    lanes[0] = mk_lane(1, 1, 2, "ROAD", None, 100, reliability_prior=0.95)  # A->B has no duration
    res, _ = solve(lanes, objective="FASTEST", constraints={"max_transit_hours": 30})
    assert ids(res) == [uids(3, 4)]  # only the fully verified route is presented as feasible
    un = {tuple(u["metrics"]["lane_uids"]): u for u in res["unverifiable"]}
    assert tuple(uids(1, 2)) in un and any("without a duration" in r for r in un[tuple(uids(1, 2))]["reasons"])
    # known time already above the limit -> VIOLATED even though one lane is unknown
    res, _ = solve(lanes, constraints={"max_transit_hours": 11})
    assert tuple(uids(1, 2)) not in {tuple(u["metrics"]["lane_uids"]) for u in res["unverifiable"]} | {
        tuple(r) for r in ids(res)
    }
    assert res["rejections"]["cut_before_completion"]["max_transit_hours"] >= 1


def test_objective_needing_missing_data_makes_the_route_unverifiable():
    lanes = diamond()
    lanes[4] = mk_lane(5, 1, 4, "SEA", 70, None)  # the direct lane has no price
    res, _ = solve(lanes, objective="LOWEST_COST")
    assert uids(5) not in ids(res)
    assert any(u["metrics"]["lane_uids"] == uids(5) and "cost" in u["reasons"][0] for u in res["unverifiable"])
    assert ids(res)[0] == uids(1, 2)


def test_currencies_are_never_mixed_or_converted():
    lanes = diamond()
    lanes[1] = mk_lane(2, 2, 4, "ROAD", 12, 120, "EUR", reliability_prior=0.95)
    res, _ = solve(lanes, objective="LOWEST_COST", constraints={"max_cost": 1000, "currency": "USD"})
    m = {tuple(u["metrics"]["lane_uids"]): u for u in res["unverifiable"]}
    assert tuple(uids(1, 2)) in m  # USD + EUR cannot be totalled or compared with a USD budget
    assert any("several currencies" in r for r in m[tuple(uids(1, 2))]["reasons"])
    plain, _ = solve(lanes, objective="LOWEST_COST")
    assert plain["ranking_currency"] == "USD" and uids(1, 2) not in ids(plain)


def test_reliability_is_a_product_and_constraint_uses_it():
    lanes = diamond()
    res, _ = solve(lanes, objective="MOST_RELIABLE")
    by = {tuple(r["metrics"]["lane_uids"]): r["metrics"]["reliability"]["value"] for r in res["routes"]}
    assert by[tuple(uids(1, 2))] == pytest.approx(0.9025) and by[tuple(uids(1, 6, 4))] == pytest.approx(
        0.95 * 0.9 * 0.99, rel=1e-5
    )
    res, _ = solve(lanes, constraints={"min_reliability": 0.9})
    assert set(map(tuple, ids(res))) == {tuple(uids(3, 4)), tuple(uids(1, 2))}


def test_risk_constraint_has_three_states_from_coverage():
    lanes = diamond()
    ctx = mk_ctx(lanes, risk={1: 0.1, 2: 0.2, 3: 0.9, 4: 0.1})
    res, _ = solve(lanes, ctx, constraints={"max_risk": 0.5})
    assert set(map(tuple, ids(res))) == {tuple(uids(5)), tuple(uids(1, 2))}
    ctx2 = mk_ctx(lanes, risk={1: 0.1, 4: 0.1})  # B and C unscored
    res, _ = solve(lanes, ctx2, constraints={"max_risk": 0.5})
    assert ids(res) == [uids(5)] and {tuple(u["metrics"]["lane_uids"]) for u in res["unverifiable"]} == {
        tuple(uids(1, 2)),
        tuple(uids(3, 4)),
        tuple(uids(1, 6, 4)),
    }


def test_cargo_restrictions_and_stale_data():
    lanes = diamond()
    lanes[2] = mk_lane(
        3,
        1,
        3,
        "AIR",
        3,
        900,
        restrictions=("HAZMAT_PROHIBITED",),
        reliability_prior=0.99,
        capacity=5,
        capacity_unit="t",
    )
    res, _ = solve(lanes, constraints={"cargo_tags": ["HAZMAT"]})
    assert uids(3, 4) not in ids(res)
    from datetime import timedelta

    lanes = diamond()
    lanes[4] = mk_lane(5, 1, 4, "SEA", 70, 150, observed_at=NOW - timedelta(days=400), reliability_prior=0.8)
    lanes[0] = mk_lane(1, 1, 2, "ROAD", 10, 100, observed_at=None, reliability_prior=0.95)
    res, _ = solve(lanes, constraints={"max_data_age_days": 365})
    assert uids(5) not in ids(res) and uids(1, 2) not in ids(res) and ids(res) == [uids(3, 4)]
    assert {tuple(u["metrics"]["lane_uids"]) for u in res["unverifiable"]} == {tuple(uids(1, 2)), tuple(uids(1, 6, 4))}


def test_alternatives_are_distinct_ranked_and_expose_trade_offs():
    res, _ = solve(diamond(), objective="BALANCED", alternatives=3)
    r = res["routes"]
    assert len(r) == 3 and len({tuple(x["metrics"]["lane_uids"]) for x in r}) == 3
    assert [x["rank"] for x in r] == [1, 2, 3]
    assert r[0]["trade_offs"] is None and set(r[1]["trade_offs"]) == {"cost", "duration", "reliability", "risk"}
    assert all(0 <= x["overlap_with_best"] <= 1 for x in r)
    assert set(res["references"]) >= {"cost", "duration", "reliability"} and res["pareto_front"]
    assert "explanation" in r[0] and r[0]["explanation"]["summary"].startswith("Ranked first")


def test_pareto_flags_dominated_routes():
    lanes = [mk_lane(1, 1, 4, "ROAD", 10, 100, reliability_prior=0.9), mk_lane(2, 1, 4, "RAIL", 20, 200, reliability_prior=0.8), mk_lane(3, 1, 4, "AIR", 5, 500, reliability_prior=0.95)]  # fmt: skip
    res, _ = solve(lanes, objective="BALANCED")
    flags = {tuple(r["metrics"]["lane_uids"]): r["pareto_optimal"] for r in res["routes"]}
    assert flags[tuple(uids(1))] and flags[tuple(uids(3))] and not flags[tuple(uids(2))]


def test_disconnected_and_unknown_endpoints_explain_themselves():
    lanes = diamond()
    res, _ = solve(lanes, origin_facility_id=4, destination_facility_id=1)
    assert res["status"] == "no_feasible_route" and any("NOT_CONNECTED" in r for r in res["infeasibility"]["reasons"])
    res, _ = solve(lanes, origin_facility_id=1, destination_facility_id=99)
    assert res["status"] == "no_feasible_route" and "no transport lane" in res["infeasibility"]["reasons"][0]
    res, _ = solve(lanes, max_hops=1, constraints={"prohibited_modes": ["SEA"]})
    assert any("HOP_LIMIT" in r for r in res["infeasibility"]["reasons"])


def test_budget_truncation_is_reported_and_yen_adds_the_best_additive_route():
    lanes = random_lanes(3, n_nodes=7, n_lanes=40)
    req = RouteRequest(origin_facility_id=1, destination_facility_id=7, objective="FASTEST", max_hops=8)
    res = eng.solve(mk_net(lanes), req, mk_ctx(lanes), Limits(max_expansions=30))
    assert (
        res["search"]["truncated"]
        and res["search"]["optimality"] == "BEST_FOUND"
        and any("search budget" in w for w in res["warnings"])
    )
    ref = brute_paths(lanes, 1, 7, 8)
    best = min(sum(ln.duration_hours for ln in p) for p in ref)
    assert res["search"]["yen_fallback_used"] and res["routes"][0]["metrics"]["duration"]["total_hours"] == best


@pytest.mark.parametrize("seed", range(30))
def test_engine_agrees_with_exhaustive_search_under_constraints(seed):
    lanes = random_lanes(seed + 500, n_nodes=6, n_lanes=16)
    limit_h, budget = 45.0, 600.0
    req = RouteRequest(origin_facility_id=1, destination_facility_id=6, objective="LOWEST_COST", alternatives=10, max_hops=6, constraints={"max_transit_hours": limit_h, "max_cost": budget, "currency": "USD"})  # fmt: skip
    res = eng.solve(mk_net(lanes), req, mk_ctx(lanes), LIM)
    ref = []
    for p in brute_paths(lanes, 1, 6, 6):
        hours = sum(ln.duration_hours for ln in p)
        ccys = {ln.currency for ln in p}
        if hours > limit_h or (ccys == {"USD"} and sum(ln.cost for ln in p) > budget):
            continue
        ref.append(p)
    comparable = [p for p in ref if {ln.currency for ln in p} == {"USD"}]
    mixed = [p for p in ref if {ln.currency for ln in p} != {"USD"}]
    got = {tuple(r["metrics"]["lane_uids"]) for r in res["routes"]} | {
        tuple(u["metrics"]["lane_uids"]) for u in res["unverifiable"]
    }
    assert res["route_count"]["feasible"] + res["route_count"]["unverifiable"] == len(ref)
    assert res["route_count"]["feasible"] == len(comparable) and len(mixed) <= res["route_count"]["unverifiable"]
    if comparable:
        want = sorted(
            comparable,
            key=lambda p: (
                sum(ln.cost for ln in p),
                sum(ln.duration_hours for ln in p),
                len(p),
                tuple(ln.id for ln in p),
            ),
        )
        assert [r["metrics"]["lane_uids"] for r in res["routes"]] == [[ln.uid for ln in p] for p in want[:10]]
    assert got >= {tuple(ln.uid for ln in p) for p in comparable[:10]}


def test_scenario_overlay_never_mutates_the_baseline_and_comparison_explains_the_change():
    lanes = diamond()
    net = mk_net(lanes)
    before = net.digest()
    req = RouteRequest(origin_facility_id=1, destination_facility_id=4, objective="FASTEST", alternatives=4)
    ctx = mk_ctx(lanes)
    base = eng.solve(net, req, ctx, LIM)
    dis = [overlay.Disruption("CLOSE_NODE", "scenario:S:E1", facility_ids=(3,)), overlay.Disruption("ADD_DURATION", "scenario:S:E2", facility_ids=(2,), add_hours=24.0), overlay.Disruption("REDUCE_CAPACITY", "scenario:S:E3", facility_ids=(2,), pct=50.0)]  # fmt: skip
    net2, applied = overlay.apply(net, dis)
    assert net.digest() == before and net.closed_nodes == {} and net.lanes["LN-T0001"].duration_hours == 10  # fmt: skip
    scen = eng.solve(net2, req, ctx, LIM)
    assert base["routes"][0]["metrics"]["lane_uids"] == uids(3, 4)
    assert scen["routes"][0]["metrics"]["lane_uids"] != uids(3, 4) and all(
        uids(3, 4) != r["metrics"]["lane_uids"] for r in scen["routes"]
    )
    cmp = eng.compare(base, scen, net2, ctx, req.constraints)
    assert cmp["best_route_changed"] is True and cmp["best_route_deltas"]["duration"] > 0
    states = {tuple(r["lane_uids"]): r for r in cmp["baseline_routes_in_scenario"]}
    assert (
        states[tuple(uids(3, 4))]["state"] == "UNAVAILABLE"
        and states[tuple(uids(3, 4))]["causes"][0]["state"] == "FACILITY_CLOSED"
    )
    assert states[tuple(uids(1, 2))]["state"] == "CHANGED" and states[tuple(uids(1, 2))]["deltas"]["duration"] == 48
    assert net2.lanes["LN-T0001"].capacity == 10 and any(
        "capacity reduced 50%" in n for n in net2.lanes["LN-T0001"].degraded
    )
    assert len(applied) == 3 and net2.digest() != before


def test_overlay_flags_but_does_not_invent_when_the_lane_has_no_figure():
    lanes = diamond()
    lanes[0] = mk_lane(1, 1, 2, "ROAD", None, 100)
    net2, applied = overlay.apply(
        mk_net(lanes), [overlay.Disruption("ADD_DURATION", "s", facility_ids=(2,), add_hours=24.0)]
    )
    assert net2.lanes["LN-T0001"].duration_hours is None and "LN-T0001" in applied[0]["flag_only"]


def test_check_route_reports_structure_problems_and_constraint_states():
    lanes = diamond()
    net, ctx = mk_net(lanes), mk_ctx(lanes)
    ok = eng.check_route(net, uids(1, 2), Constraints(max_transit_hours=30), ctx)
    assert ok["classification"] == "FEASIBLE" and ok["metrics"]["duration"]["total_hours"] == 22
    bad = eng.check_route(net, uids(1, 2), Constraints(max_transit_hours=20), ctx)
    assert bad["classification"] == "INFEASIBLE" and bad["constraints"][0]["state"] == "VIOLATED"
    gap = eng.check_route(net, uids(1, 4), Constraints(), ctx)
    assert gap["valid_structure"] is False and gap["problems"][0]["problem"] == "NOT_CONTIGUOUS"
    missing = eng.check_route(net, ["LN-NOPE"], Constraints(), ctx)
    assert missing["problems"][0]["problem"] == "LANE_NOT_AVAILABLE"
