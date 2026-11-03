"""Discrete-time inventory simulation (pure, deterministic given its inputs).

Daily order of events (day ``t`` = 1..T):
1. receipts due on day ``t`` arrive (an order placed at the start of day ``t0`` with lead time ``L`` days arrives at the start of day
   ``t0 + L``, ``L >= 1``, so it is exposed to exactly ``L`` days of demand and stock is never usable before its lead time has elapsed);
2. any backlog is filled from stock;
3. the policy reviews the inventory position (on-hand + on-order - backlog) and may place an order;
4. day ``t`` demand is served from stock; the unserved part is a shortage (backordered or lost).

Definitions: *fill rate* = 1 - units short / demand (shortage measured when demand occurs); *cycle service level* = share of completed
replenishment cycles (order placed -> order received) with no shortage day from the order day up to the day before the arrival; *holding cost* =
holding rate x sum of end-of-day on-hand; *turnover* = units served / average on-hand over the simulated period (not annualised).
Shelf life and expiry are not modelled.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.invopt.policies import round_down, round_up

EPS = 1e-9
MAX_TRAJECTORY = 366


@dataclass(frozen=True)
class SimPolicy:
    kind: str  # S_Q | S_UPTO | R_S
    s: float = 0.0
    q: float | None = None
    big_s: float | None = None
    review: int = 1
    moq: float = 0.0
    lot: float | None = None
    max_q: float | None = None


@dataclass(frozen=True)
class SimCosts:
    holding: float | None = None  # per unit per day
    ordering: float | None = None  # per order
    shortage: float | None = None  # per unit short
    unit: float | None = None  # purchase cost per unit


def _fit_order(q: float, p: SimPolicy) -> float:
    if q <= EPS:
        return 0.0
    q = max(q, p.moq)
    q = round_up(q, p.lot)
    if p.max_q is not None and q > p.max_q + EPS:
        q = round_down(p.max_q, p.lot)
        if q < p.moq - EPS or q <= EPS:
            return 0.0
    return q


def simulate(
    demand: Sequence[float], policy: SimPolicy, lead_time: Callable[[int], int], *, on_hand0: float, pipeline0: Sequence[tuple[int, float]] = (),
    backlog0: float = 0.0, backorders: bool = True, costs: SimCosts | None = None, blocked: Callable[[int], bool] | None = None,
    keep_path: bool = True,
) -> dict[str, Any]:  # fmt: skip
    if policy.kind == "S_Q" and not policy.q:
        raise ValueError("S_Q needs q")
    if policy.kind in ("S_UPTO", "R_S") and policy.big_s is None:
        raise ValueError(f"{policy.kind} needs big_s")
    T = len(demand)
    on_hand, backlog = float(on_hand0), float(backlog0)
    arrivals: dict[int, float] = defaultdict(float)
    on_order = 0.0
    for day, q in pipeline0:
        arrivals[max(1, int(day))] += q
        on_order += q
    short_flag = [0] * (T + 2)
    total_demand = units_short = hold_sum = backlog_sum = 0.0
    stockout_days = stockout_events = 0
    prev_short = False
    orders: list[dict[str, Any]] = []
    blocked_orders = 0
    path_oh: list[float] = []
    path_ip: list[float] = []
    for t in range(1, T + 1):
        rec = arrivals.pop(t, 0.0)
        on_hand += rec
        on_order -= rec
        if backlog > 0 and on_hand > 0:
            f = min(on_hand, backlog)
            on_hand -= f
            backlog -= f
        # --- review (before the day's demand)
        ip = on_hand + on_order - backlog
        want = 0.0
        if policy.kind == "S_Q":
            if ip <= policy.s + EPS:
                want = policy.q * max(1, math.ceil((policy.s - ip) / policy.q + 1e-12))  # type: ignore[operator]
        elif policy.kind == "S_UPTO":
            if ip <= policy.s + EPS:
                want = policy.big_s - ip  # type: ignore[operator]
        elif t % max(1, policy.review) == 0:
            want = policy.big_s - ip  # type: ignore[operator]
        if want > EPS:
            if blocked is not None and blocked(t):
                blocked_orders += 1
            else:
                q = _fit_order(want, policy)
                if q > EPS:
                    lt = max(1, int(lead_time(len(orders))))
                    arrivals[t + lt] += q
                    on_order += q
                    orders.append({"day": t, "quantity": round(q, 6), "arrival_day": t + lt, "lead_time_days": lt})
        # --- demand
        d = float(demand[t - 1])
        served = min(on_hand, d)
        short = d - served
        on_hand -= served
        total_demand += d
        if short > EPS:
            units_short += short
            stockout_days += 1
            short_flag[t] = 1
            if not prev_short:
                stockout_events += 1
            prev_short = True
            if backorders:
                backlog += short
        else:
            prev_short = False
        hold_sum += on_hand
        backlog_sum += backlog
        if keep_path and t <= MAX_TRAJECTORY:
            path_oh.append(round(on_hand, 6))
            path_ip.append(round(on_hand + on_order - backlog, 6))
    # cycle service level over completed cycles
    prefix = [0] * (T + 2)
    for i in range(1, T + 1):
        prefix[i] = prefix[i - 1] + short_flag[i]
    cycles = ok = 0
    for o in orders:
        a = o["arrival_day"]
        if a <= T:
            cycles += 1
            lo, hi = o["day"], a - 1
            if hi < lo or prefix[hi] - prefix[lo - 1] == 0:
                ok += 1
    qty_total = sum(o["quantity"] for o in orders)
    avg_oh = hold_sum / T if T else 0.0
    c = costs or SimCosts()
    comp: dict[str, float | None] = {
        "holding": None if c.holding is None else c.holding * hold_sum,
        "ordering": None if c.ordering is None else c.ordering * len(orders),
        "shortage": None if c.shortage is None else c.shortage * units_short,
    }
    known = {k: v for k, v in comp.items() if v is not None}
    out: dict[str, Any] = {
        "days": T, "total_demand": round(total_demand, 6), "units_short": round(units_short, 6),
        "fill_rate": None if total_demand <= EPS else round(1 - units_short / total_demand, 6),
        "stockout_days": stockout_days, "stockout_events": stockout_events,
        "cycle_service_level": None if cycles == 0 else round(ok / cycles, 6), "completed_cycles": cycles,
        "avg_on_hand": round(avg_oh, 6), "avg_backlog": round(backlog_sum / T, 6) if T else 0.0,
        "orders": len(orders), "ordered_quantity": round(qty_total, 6), "blocked_orders": blocked_orders,
        "turnover": None if avg_oh <= EPS else round((total_demand - units_short) / avg_oh, 6),
        "end_on_hand": round(on_hand, 6), "end_backlog": round(backlog, 6),
        "costs": {
            "holding": comp["holding"], "ordering": comp["ordering"], "shortage": comp["shortage"],
            "known_subtotal": round(sum(known.values()), 6) if known else None,
            "omitted_components": [k for k, v in comp.items() if v is None],
            "purchase_spend": None if c.unit is None else round(c.unit * qty_total, 6),
        },
    }  # fmt: skip
    if keep_path:
        out["order_list"] = orders[:200]
        out["path"] = {"on_hand": path_oh, "inventory_position": path_ip, "truncated": T > MAX_TRAJECTORY}
    return out


def _stats(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {"mean": None, "p05": None, "p95": None}
    v = sorted(vals)
    n = len(v)
    return {
        "mean": round(sum(v) / n, 6),
        "p05": round(v[max(0, math.ceil(0.05 * n) - 1)], 6),
        "p95": round(v[min(n - 1, math.ceil(0.95 * n) - 1)], 6),
    }


def aggregate(reps: list[dict[str, Any]]) -> dict[str, Any]:
    keys = [
        "fill_rate",
        "cycle_service_level",
        "stockout_days",
        "stockout_events",
        "units_short",
        "avg_on_hand",
        "orders",
        "turnover",
    ]
    out = {k: _stats([r[k] for r in reps if r.get(k) is not None]) for k in keys}
    out["n_without_value"] = {
        k: sum(1 for r in reps if r.get(k) is None) for k in ("fill_rate", "cycle_service_level", "turnover")
    }
    cost: dict[str, Any] = {}
    for k in ("holding", "ordering", "shortage", "known_subtotal", "purchase_spend"):
        vals = [r["costs"][k] for r in reps if r["costs"].get(k) is not None]
        cost[k] = _stats(vals) if vals else None
    cost["omitted_components"] = reps[0]["costs"]["omitted_components"] if reps else []
    out["costs"] = cost
    return out


def monte_carlo(
    policy: SimPolicy, *, days: int, reps: int, seed: int, demand_sampler: Callable[[random.Random, int], list[float]],
    lead_sampler: Callable[[random.Random], int], on_hand0: float, pipeline0: Sequence[tuple[int, float]] = (), backlog0: float = 0.0,
    backorders: bool = True, costs: SimCosts | None = None, blocked: Callable[[int], bool] | None = None, stop: Callable[[], None] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Replications share demand and lead-time draws across policies when called with the same seed (paired comparison)."""
    results = []
    for r in range(reps):
        if stop:
            stop()
        rng = random.Random(seed + r * 7919)
        demand = demand_sampler(rng, days)
        lrng = random.Random(seed + r * 104729 + 1)
        lts = [lead_sampler(lrng) for _ in range(days + 1)]
        results.append(simulate(demand, policy, lambda i: lts[i % len(lts)], on_hand0=on_hand0, pipeline0=pipeline0, backlog0=backlog0, backorders=backorders, costs=costs, blocked=blocked, keep_path=(r == 0)))  # fmt: skip
    agg = aggregate(results)
    agg["replications"] = reps
    agg["sample_path"] = results[0].get("path")
    agg["sample_orders"] = results[0].get("order_list", [])[:20]
    return agg
