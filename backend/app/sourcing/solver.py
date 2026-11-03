"""Exact allocation solver (pure Python, no database, no third-party solver).

Problem: choose a quantity q_i for each item i so that
  * every item is either unused (q_i = 0) or used with lo_i <= q_i <= hi_i (minimum order quantity / minimum share, and
    capacity / contractual maximum / share cap), and q_i is a multiple of lot_i when the item has an order increment;
  * the total equals the demand D (or lies in [D, D*(1+overage)] on the lot grid);
  * the number of used items lies in [min_n, max_n];
  * the linear objective sum(cost_i * q_i) is minimal.

Two exact methods are used, because the structure differs:
  * No order increments: branch and bound over "used / not used" decisions with a relaxation bound (fill the remaining demand
    from the cheapest remaining capacity, ignoring unmet minimums). For a fixed used set the optimum is the greedy fill, so the
    search is exact. If the node budget is exhausted the status is BOUND_EXCEEDED - never a silently weaker answer.
  * Order increments: dynamic programming over items x total units on the grid of the finest increment (integer quantities).
    If the grid or the operation count is too large the status is BOUND_EXCEEDED.
A simplex/MILP library is deliberately not used: sizes are small (<= a few dozen suppliers) and these methods are exact,
dependency-free and verifiable against brute-force enumeration.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from fractions import Fraction

EPS = 1e-9
INF = float("inf")
MIN_USED = 1e-6


@dataclass(frozen=True)
class Item:
    key: str
    cost: float  # objective cost per unit, >= 0
    lo: float  # smallest non-zero quantity
    hi: float  # largest quantity
    lot: float | None = None


@dataclass
class Solution:
    status: str  # OPTIMAL | INFEASIBLE | BOUND_EXCEEDED
    quantities: dict[str, float] = field(default_factory=dict)
    objective: float | None = None
    method: str = ""
    nodes: int = 0
    exact: bool = True
    grid_step: float | None = None
    note: str = ""


def _usable(items: list[Item]) -> list[Item]:
    # a used item must receive a strictly positive quantity, so a zero minimum is lifted to a negligible positive one
    out = [
        replace(it, lo=max(it.lo, MIN_USED)) for it in items if it.hi > EPS and it.lo <= it.hi + EPS and it.cost >= 0
    ]
    out = [it for it in out if it.lo <= it.hi + EPS]
    return sorted(out, key=lambda it: (it.cost, it.key))


def check_allocation(
    items: list[Item],
    q: dict[str, float],
    demand: float,
    *,
    overage_pct: float = 0.0,
    min_n: int = 1,
    max_n: int | None = None,
) -> list[str]:
    """Independent re-check of every hard rule on a finished allocation. Returns the violations (empty = valid)."""
    bad: list[str] = []
    by = {it.key: it for it in items}
    used = 0
    total = 0.0
    for k, v in q.items():
        it = by.get(k)
        if it is None:
            bad.append(f"{k}: not a candidate")
            continue
        if v < -EPS:
            bad.append(f"{k}: negative quantity")
        if v <= EPS:
            continue
        used += 1
        total += v
        if v > it.hi + 1e-7 * max(1.0, it.hi):
            bad.append(f"{k}: {v:g} exceeds the maximum {it.hi:g}")
        if v < it.lo - 1e-7 * max(1.0, it.lo):
            bad.append(f"{k}: {v:g} is below the minimum {it.lo:g}")
        if it.lot:
            r = v / it.lot
            if abs(r - round(r)) > 1e-6:
                bad.append(f"{k}: {v:g} is not a multiple of the order increment {it.lot:g}")
    top = demand * (1 + overage_pct / 100.0)
    if total < demand - 1e-7 * max(1.0, demand) or total > top + 1e-7 * max(1.0, top):
        bad.append(f"total {total:g} is outside the allowed range [{demand:g}, {top:g}]")
    if used < min_n:
        bad.append(f"{used} supplier(s) used, at least {min_n} required")
    if max_n is not None and used > max_n:
        bad.append(f"{used} supplier(s) used, at most {max_n} allowed")
    return bad


# ----------------------------------------------------------------------------------------------- continuous: branch and bound
def _bb(
    items: list[Item],
    demand: float,
    top: float,
    min_n: int,
    max_n: int | None,
    max_nodes: int,
    stop: Callable[[], None] | None,
) -> Solution:
    n = len(items)
    suffix_hi = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_hi[i] = suffix_hi[i + 1] + items[i].hi
    best = [INF, None]
    nodes = [0]
    over = [False]
    chosen: list[int] = []

    def relaxed(i: int, lo_sum: float, lo_cost: float) -> float:
        rem = demand - lo_sum
        cost = lo_cost
        if rem <= EPS:
            return cost
        for j in chosen:
            take = min(rem, items[j].hi - items[j].lo)
            cost += take * items[j].cost
            rem -= take
            if rem <= EPS:
                return cost
        for j in range(i, n):
            take = min(rem, items[j].hi)
            cost += take * items[j].cost
            rem -= take
            if rem <= EPS:
                return cost
        return INF if rem > 1e-7 * max(1.0, demand) else cost

    def dfs(i: int, lo_sum: float, lo_cost: float, hi_sum: float) -> None:
        if over[0]:
            return
        nodes[0] += 1
        if nodes[0] > max_nodes:
            over[0] = True
            return
        if stop is not None and nodes[0] % 512 == 0:
            stop()
        k = len(chosen)
        if (max_n is not None and k > max_n) or k + (n - i) < min_n:
            return
        if lo_sum > top + EPS or hi_sum + suffix_hi[i] < demand - 1e-7 * max(1.0, demand):
            return
        b = relaxed(i, lo_sum, lo_cost)
        if b == INF or b >= best[0] - 1e-12 * max(1.0, abs(best[0]) if best[0] < INF else 1.0):
            return
        if i == n:
            if k >= max(1, min_n) and hi_sum >= demand - 1e-7 * max(1.0, demand):
                best[0], best[1] = b, list(chosen)
            return
        it = items[i]
        chosen.append(i)
        dfs(i + 1, lo_sum + it.lo, lo_cost + it.lo * it.cost, hi_sum + it.hi)
        chosen.pop()
        dfs(i + 1, lo_sum, lo_cost, hi_sum)

    dfs(0, 0.0, 0.0, 0.0)
    if over[0]:
        return Solution(
            "BOUND_EXCEEDED",
            method="branch_and_bound",
            nodes=nodes[0],
            exact=False,
            note=f"node budget {max_nodes} exhausted",
        )
    if best[1] is None:
        return Solution("INFEASIBLE", method="branch_and_bound", nodes=nodes[0])
    q = {items[j].key: items[j].lo for j in best[1]}
    rem = demand - sum(q.values())
    for j in best[1]:
        take = min(max(rem, 0.0), items[j].hi - items[j].lo)
        q[items[j].key] += take
        rem -= take
    return Solution("OPTIMAL", q, best[0], "branch_and_bound", nodes[0])


def _max_total(items: list[Item], cap: float, min_n: int, max_n: int | None, max_nodes: int) -> tuple[float, bool]:
    """Largest total <= cap that some used set can reach (continuous items): max over sets S with sum(lo) <= cap of min(cap, sum(hi))."""
    n = len(items)
    suffix_hi = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_hi[i] = suffix_hi[i + 1] + items[i].hi
    best = [0.0]
    nodes = [0]
    over = [False]

    def dfs(i: int, k: int, lo_sum: float, hi_sum: float) -> None:
        if over[0] or best[0] >= cap - EPS:
            return
        nodes[0] += 1
        if nodes[0] > max_nodes:
            over[0] = True
            return
        if (max_n is not None and k > max_n) or k + (n - i) < min_n or lo_sum > cap + EPS:
            return
        if min(cap, hi_sum + suffix_hi[i]) <= best[0] + EPS:
            return
        if i == n:
            if k >= max(1, min_n):
                best[0] = max(best[0], min(cap, hi_sum))
            return
        it = items[i]
        dfs(i + 1, k + 1, lo_sum + it.lo, hi_sum + it.hi)
        dfs(i + 1, k, lo_sum, hi_sum)

    dfs(0, 0, 0.0, 0.0)
    return best[0], over[0]


# --------------------------------------------------------------------------------------------- order increments: dynamic programming
def _frac(x: float) -> Fraction:
    return Fraction(x).limit_denominator(10**6)


def _fgcd(a: Fraction, b: Fraction) -> Fraction:
    return Fraction(math.gcd(a.numerator * b.denominator, b.numerator * a.denominator), a.denominator * b.denominator)


def _grid(items: list[Item], demand: float, top: float) -> Fraction:
    """Finest quantity step needed. Items with an increment contribute it; items without one are continuous, so their bounds and the
    demand range are added to the lattice (a vertex of the continuous part is always a combination of those values)."""
    g: Fraction | None = None

    def add(x: Fraction) -> None:
        nonlocal g
        if x > 0:
            g = x if g is None else _fgcd(g, x)

    mixed = any(not it.lot for it in items)
    for it in items:
        if it.lot:
            add(_frac(it.lot))
        else:
            if it.lo > 10 * MIN_USED:
                add(_frac(it.lo))
            add(_frac(it.hi))
    if mixed:
        add(_frac(demand))
        add(_frac(top))
    assert g is not None
    return g


def _dp(
    items: list[Item], demand: float, overage_pct: float, min_n: int, max_n: int | None, limits: tuple[int, int], reach_only: bool = False,
    stop: Callable[[], None] | None = None,
) -> Solution:  # fmt: skip
    max_units, max_ops = limits
    top = demand * (1 + overage_pct / 100.0)
    g = _grid(items, demand, top)
    gf = float(g)
    spec: list[tuple[int, int, int]] = []  # (units per step, jmin, jmax)
    for it in items:
        s = int(round(_frac(it.lot) / g)) if it.lot else 1
        step_q = it.lot if it.lot else gf
        jmin = max(1, math.ceil(it.lo / step_q - 1e-9))
        jmax = math.floor(it.hi / step_q + 1e-9)
        spec.append((s, jmin, jmax))
    umin = math.ceil(demand / gf - 1e-9)
    umax = math.floor(top / gf + 1e-9)
    if reach_only:
        umin, umax = 0, math.floor(demand / gf + 1e-9)
    if umin > umax:
        return Solution(
            "INFEASIBLE",
            method="dynamic_programming",
            grid_step=gf,
            note="no multiple of the order increments lies within the demand range",
        )
    if umax > max_units:
        return Solution(
            "BOUND_EXCEEDED",
            method="dynamic_programming",
            exact=False,
            grid_step=gf,
            note=f"grid of {umax} units exceeds the limit {max_units}",
        )
    cap_c = max_n if max_n is not None else min_n
    cd = cap_c + 1
    ops = sum((umax + 1) * cd * max(0, jm - jn + 2) for (_, jn, jm) in spec)
    if ops > max_ops:
        return Solution(
            "BOUND_EXCEEDED",
            method="dynamic_programming",
            exact=False,
            grid_step=gf,
            note=f"{ops} operations exceed the limit {max_ops}",
        )
    cur = [[INF] * (umax + 1) for _ in range(cd)]
    cur[0][0] = 0.0
    choices: list[dict[tuple[int, int], tuple[int, int]]] = []
    for it, (s, jn, jm) in zip(items, spec, strict=True):
        if stop is not None:
            stop()
        new = [row[:] for row in cur]
        ch: dict[tuple[int, int], tuple[int, int]] = {}
        unit_cost = it.cost * gf
        for c in range(cd):
            c2 = c + 1 if max_n is not None else min(c + 1, cap_c)
            if max_n is not None and c2 > max_n:
                continue
            row = cur[c]
            for t in range(umax + 1):
                base = row[t]
                if base == INF:
                    continue
                for j in range(jn, jm + 1):
                    t2 = t + j * s
                    if t2 > umax:
                        break
                    val = base + unit_cost * j * s
                    if val < new[c2][t2] - 1e-12:
                        new[c2][t2] = val
                        ch[(c2, t2)] = (j, c)
        cur = new
        choices.append(ch)
    best = (INF, -1, -1)
    valid_c = range(min_n, max_n + 1) if max_n is not None else [cap_c]
    for c in valid_c:
        for t in range(max(umin, 1), umax + 1):
            v = cur[c][t]
            if reach_only and v < INF and t > best[2]:
                best = (0.0, c, t)
            elif not reach_only and v < best[0] - 1e-12:
                best = (v, c, t)
    if best[1] < 0:
        return Solution("INFEASIBLE", method="dynamic_programming", grid_step=gf, nodes=ops)
    if reach_only:
        return Solution("OPTIMAL", {}, 0.0, "dynamic_programming", ops, True, gf, note=str(best[2]))
    q: dict[str, float] = {}
    c, t = best[1], best[2]
    for i in range(len(items) - 1, -1, -1):
        rec = choices[i].get((c, t))
        if rec is None:
            continue
        j, pc = rec
        q[items[i].key] = j * spec[i][0] * gf
        t -= j * spec[i][0]
        c = pc
    return Solution("OPTIMAL", q, best[0], "dynamic_programming", ops, True, gf)


# ------------------------------------------------------------------------------------------------------------------------ public
def solve(
    items: list[Item], demand: float, *, overage_pct: float = 0.0, min_n: int = 1, max_n: int | None = None, max_nodes: int = 200_000,
    max_grid_units: int = 20_000, max_dp_ops: int = 3_000_000, stop: Callable[[], None] | None = None,
) -> Solution:  # fmt: skip
    """Minimum-cost allocation of exactly ``demand`` (see module docstring)."""
    if demand <= 0 or not math.isfinite(demand):
        raise ValueError("demand must be positive and finite")
    usable = _usable(items)
    if not usable or (max_n is not None and max_n < max(1, min_n)):
        return Solution("INFEASIBLE", method="none")
    if any(it.lot for it in usable):
        return _dp(usable, demand, overage_pct, max(1, min_n), max_n, (max_grid_units, max_dp_ops), stop=stop)
    return _bb(usable, demand, demand * (1 + overage_pct / 100.0), max(1, min_n), max_n, max_nodes, stop)


def largest_fulfilment(
    items: list[Item], demand: float, *, min_n: int = 1, max_n: int | None = None, max_nodes: int = 200_000, max_grid_units: int = 20_000,
    max_dp_ops: int = 3_000_000, stop: Callable[[], None] | None = None,
) -> tuple[float, bool]:  # fmt: skip
    """Largest total <= demand that satisfies every hard rule, and whether the answer is exact. 0.0 when nothing is possible."""
    usable = _usable(items)
    if not usable:
        return 0.0, True
    if any(it.lot for it in usable):
        s = _dp(usable, demand, 0.0, max(1, min_n), max_n, (max_grid_units, max_dp_ops), reach_only=True, stop=stop)
        if s.status == "BOUND_EXCEEDED":
            return 0.0, False
        return (int(s.note) * (s.grid_step or 0.0), True) if s.status == "OPTIMAL" else (0.0, True)
    t, over = _max_total(usable, demand, max(1, min_n), max_n, max_nodes)
    return t, not over
