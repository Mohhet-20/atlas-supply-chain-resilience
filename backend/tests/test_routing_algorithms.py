"""Graph algorithms verified against an independent exhaustive enumeration on small random graphs. Synthetic data only."""

from __future__ import annotations

import pytest

from app.routing import algorithms as A
from app.routing.network import Network
from tests.routing_helpers import brute_paths, diamond, mk_lane, mk_net, random_lanes


def _out(lanes) -> dict:
    return mk_net(lanes).out


def _w(ln):
    return ln.duration_hours


def _total(path) -> float:
    return sum(ln.duration_hours for ln in path)


@pytest.mark.parametrize("seed", range(25))
def test_dijkstra_matches_exhaustive_minimum(seed):
    lanes = random_lanes(seed)
    out = _out(lanes)
    ref = brute_paths(lanes, 1, 6, 10)
    got = A.dijkstra(out, 1, 6, _w)
    if not ref:
        assert got is None
    else:
        assert abs(_total(got) - min(_total(p) for p in ref)) < 1e-9
        nodes = [got[0].origin, *[ln.dest for ln in got]]
        assert len(set(nodes)) == len(nodes)


@pytest.mark.parametrize("seed", range(15))
def test_yen_returns_the_k_cheapest_loopless_paths(seed):
    lanes = random_lanes(seed + 100)
    out = _out(lanes)
    ref = sorted(_total(p) for p in brute_paths(lanes, 1, 6, 10))
    got = A.yen(out, 1, 6, _w, 5)
    assert [round(_total(p), 9) for p in got] == [round(x, 9) for x in ref[: len(got)]]
    assert len(got) == min(5, len(ref))
    assert len({tuple(ln.id for ln in p) for p in got}) == len(got)
    for p in got:
        nodes = [p[0].origin, *[ln.dest for ln in p]]
        assert len(set(nodes)) == len(nodes)


@pytest.mark.parametrize("seed", range(15))
def test_enumeration_finds_exactly_the_reference_paths(seed):
    lanes = random_lanes(seed + 200)
    for hops in (2, 3, 6):
        found, stats = A.enumerate_paths(_out(lanes), 1, 6, hops)
        ref = brute_paths(lanes, 1, 6, hops)
        assert {tuple(ln.id for ln in p) for p in found} == {tuple(ln.id for ln in p) for p in ref}
        assert not stats.truncated and all(len(p) <= hops for p in found)


def test_dijkstra_ties_are_resolved_deterministically_and_parallel_lanes_are_distinct():
    lanes = [mk_lane(1, 1, 2, "ROAD", 5), mk_lane(2, 1, 2, "RAIL", 5), mk_lane(3, 2, 3, "ROAD", 5)]
    p1 = A.dijkstra(_out(lanes), 1, 3, _w)
    p2 = A.dijkstra(_out(list(reversed(lanes))), 1, 3, _w)
    assert [ln.id for ln in p1] == [ln.id for ln in p2] == [1, 3]
    assert len(A.yen(_out(lanes), 1, 3, _w, 5)) == 2  # the two parallel lanes are two different routes


def test_negative_weights_are_rejected_not_silently_wrong():
    lanes = [mk_lane(1, 1, 2, hours=-1.0), mk_lane(2, 2, 3)]
    with pytest.raises(ValueError):
        A.dijkstra(_out(lanes), 1, 3, _w)


def test_unreachable_and_blocked_targets():
    lanes = diamond()
    out = _out(lanes)
    assert A.dijkstra(out, 4, 1, _w) is None  # lanes are directed
    assert A.dijkstra(out, 1, 4, _w, blocked_nodes=frozenset({4})) is None
    p = A.dijkstra(out, 1, 4, _w, blocked_lanes=frozenset({3}))
    assert [ln.id for ln in p] == [1, 6, 4] or [ln.id for ln in p] == [1, 2]


def test_reverse_distances_are_exact_lower_bounds():
    lanes = diamond()
    rd = A.reverse_distances(_out(lanes), 4, _w)
    assert rd[4] == 0 and rd[3] == 3 and rd[2] == 8 and rd[1] == 6


def test_search_budget_truncates_and_says_so():
    lanes = random_lanes(7, n_nodes=7, n_lanes=30)
    _paths, stats = A.enumerate_paths(_out(lanes), 1, 7, 8, budget=10)
    assert stats.truncated and stats.expansions == 11


def test_prune_only_removes_what_it_is_told_to():
    lanes = diamond()
    found, stats = A.enumerate_paths(_out(lanes), 1, 4, 6, prune=lambda p: sum(ln.duration_hours for ln in p) > 20)
    assert {tuple(ln.id for ln in p) for p in found} == {(3, 4), (1, 6, 4)}
    assert stats.pruned > 0


def test_network_digest_is_order_independent_and_changes_with_content():
    a = mk_net(diamond())
    b = Network(lanes=dict(reversed(list(a.lanes.items()))), nodes=a.nodes)
    assert a.digest() == b.digest()
    changed = diamond()
    changed[0] = mk_lane(1, 1, 2, "ROAD", 11, 100, capacity=20, capacity_unit="t", reliability_prior=0.95)
    assert mk_net(changed).digest() != a.digest()
