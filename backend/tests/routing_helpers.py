"""SYNTHETIC fixtures for routing tests. Every facility, lane and number is invented; results verify the software only."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.routing.evaluate import Context, Rel
from app.routing.network import Lane, Network, Node

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
R = "/api/v1/routing"


def mk_lane(
    i: int,
    o: int,
    d: int,
    mode: str = "ROAD",
    hours: float | None = 10.0,
    cost: float | None = 100.0,
    ccy: str | None = "USD",
    **kw: Any,
) -> Lane:
    base = dict(
        id=i, uid=f"LN-T{i:04d}", origin=o, dest=d, mode=mode, duration_hours=hours, duration_quality="VERIFIED" if hours is not None else "UNKNOWN",
        cost=cost, currency=ccy if cost is not None else None, cost_quality="VERIFIED" if cost is not None else "UNKNOWN", provenance="USER_PROVIDED", observed_at=NOW,
    )  # fmt: skip
    return Lane(**(base | kw))


def mk_net(lanes: list[Lane]) -> Network:
    ids = {x for ln in lanes for x in (ln.origin, ln.dest)}
    nodes = {i: Node(i, f"F{i}", f"Facility {i}", "Testland", None, None, "UNKNOWN", None, "ACTIVE") for i in ids}
    return Network(lanes={ln.uid: ln for ln in lanes}, nodes=nodes, as_of=NOW)


def mk_ctx(lanes: list[Lane], rel: dict[int, float] | None = None, risk: dict[int, float] | None = None) -> Context:
    r = {
        ln.uid: Rel(
            (rel or {}).get(ln.id, ln.reliability_prior),
            "PRIOR" if (rel or {}).get(ln.id, ln.reliability_prior) is not None else "UNKNOWN",
        )
        for ln in lanes
    }
    return Context(rel=r, risk=dict(risk or {}), now=NOW)


def diamond() -> list[Lane]:
    """A=1 B=2 C=3 D=4. Four A->D routes with known totals (see tests)."""
    return [
        mk_lane(1, 1, 2, "ROAD", 10, 100, capacity=20, capacity_unit="t", reliability_prior=0.95),
        mk_lane(2, 2, 4, "ROAD", 12, 120, capacity=20, capacity_unit="t", reliability_prior=0.95),
        mk_lane(3, 1, 3, "AIR", 3, 900, capacity=5, capacity_unit="t", reliability_prior=0.99),
        mk_lane(4, 3, 4, "AIR", 3, 800, capacity=5, capacity_unit="t", reliability_prior=0.99),
        mk_lane(5, 1, 4, "SEA", 70, 150, capacity=100, capacity_unit="t", reliability_prior=0.80),
        mk_lane(6, 2, 3, "RAIL", 5, 60, capacity=50, capacity_unit="t", reliability_prior=0.90),
    ]


def random_lanes(seed: int, n_nodes: int = 6, n_lanes: int = 14) -> list[Lane]:
    rng = random.Random(seed)
    lanes = []
    for i in range(1, n_lanes + 1):
        o, d = rng.sample(range(1, n_nodes + 1), 2)
        lanes.append(mk_lane(i, o, d, rng.choice(["ROAD", "RAIL", "SEA", "AIR"]), float(rng.randint(1, 40)), float(rng.randint(10, 500)), rng.choice(["USD", "USD", "USD", "EUR"]) if rng.random() < 0.1 else "USD", reliability_prior=round(rng.uniform(0.6, 0.99), 2)))  # fmt: skip
    return lanes


def brute_paths(lanes: list[Lane], src: int, dst: int, max_hops: int) -> list[list[Lane]]:
    """Independent reference: plain recursive enumeration of every loopless lane sequence."""
    out: list[list[Lane]] = []

    def rec(node: int, seen: set[int], path: list[Lane]) -> None:
        if len(path) >= max_hops:
            return
        for ln in lanes:
            if ln.origin == node and ln.dest not in seen:
                if ln.dest == dst:
                    out.append([*path, ln])
                else:
                    rec(ln.dest, seen | {ln.dest}, [*path, ln])

    rec(src, {src}, [])
    return out
