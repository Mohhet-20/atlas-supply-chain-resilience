"""Hard constraints, an independent plan validator and the bounded candidate generator.

The generator enumerates supplier shares on a discrete grid (a documented approximation of the continuous problem). The validator re-checks
every constraint from the problem data on its own, so a generated plan is never trusted because the generator produced it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from app.resilience.model import EPS

CATALOGUE = [
    (
        "eligibility",
        "hard",
        "Only suppliers assessed ELIGIBLE under the sourcing rules take part (approval, contract window, unit and currency, thresholds).",
    ),
    (
        "demand_balance",
        "hard",
        "Allocated quantity equals the demand, or lies within the stated overage; with partial fulfilment allowed it may be lower.",
    ),
    ("capacity", "hard", "No supplier exceeds its stated capacity or contractual maximum allocation."),
    ("minimum_order_quantity", "hard", "A used supplier receives at least its minimum order quantity."),
    ("lot_size", "hard", "A used supplier receives a multiple of its order increment."),
    (
        "supplier_count_and_share",
        "hard",
        "Minimum/maximum number of suppliers and minimum/maximum supplier share, when configured.",
    ),
    ("budget_ceiling", "hard", "Total known cost does not exceed the ceiling (only when configured; needs prices)."),
    (
        "min_baseline_fulfilment",
        "hard",
        "Fulfilment under normal conditions is at least the target (only when configured).",
    ),
    (
        "min_worst_case_fulfilment",
        "hard",
        "Fulfilment in the worst evaluated scenario is at least the target (only when configured and scenarios exist).",
    ),
    ("max_hhi / max_delay_days", "hard", "Concentration and weighted lead-time ceilings (only when configured)."),
]


def structural(P: dict[str, Any], req: Any, plan: dict[str, float]) -> list[dict[str, Any]]:
    """Independent check of the constraints that define a valid allocation. Returns check rows (passed true/false)."""
    k = req.constraints
    rows: list[dict[str, Any]] = []
    used = {u: q for u, q in plan.items() if q > EPS}
    tot = sum(used.values())
    D = P["D"]
    top = D * (1 + k.overage_pct / 100.0)

    def add(cid: str, ok: bool, detail: str, slack: float | None = None) -> None:
        rows.append({"id": cid, "passed": ok, "detail": detail, "slack": slack, "binding": ok and slack is not None and abs(slack) <= 1e-6 * max(1.0, abs(slack) + 1.0)})  # fmt: skip

    bad_uid = [u for u in used if u not in P["sup"]]
    add(
        "eligibility",
        not bad_uid,
        "all used offers are eligible" if not bad_uid else f"not eligible: {', '.join(bad_uid)}",
    )
    lo = 0.0 if P["allow_partial"] else D
    add(
        "demand_balance",
        lo - 1e-7 * max(1, D) <= tot <= top + 1e-7 * max(1, top),
        f"total {tot:g} must be within [{lo:g}, {top:g}]",
        min(tot - lo, top - tot),
    )
    for u, q in used.items():
        s = P["sup"].get(u)
        if s is None:
            continue
        cap = float(s["capacity"])
        add("capacity", q <= cap + 1e-7 * max(1, cap), f"{s['code']}: {q:g} of capacity {cap:g}", cap - q)
        if s.get("moq"):
            add(
                "minimum_order_quantity",
                q >= s["moq"] - 1e-7 * max(1, s["moq"]),
                f"{s['code']}: {q:g} vs minimum {s['moq']:g}",
                q - s["moq"],
            )
        if s.get("lot"):
            r = q / s["lot"]
            add("lot_size", abs(r - round(r)) <= 1e-6, f"{s['code']}: {q:g} vs increment {s['lot']:g}")
    n = len(used)
    if k.min_suppliers:
        add("supplier_count_and_share", n >= k.min_suppliers, f"{n} suppliers used, at least {k.min_suppliers}")
    if k.max_suppliers:
        add(
            "supplier_count_and_share",
            n <= k.max_suppliers,
            f"{n} suppliers used, at most {k.max_suppliers}",
            k.max_suppliers - n,
        )
    if tot > EPS:
        for u, q in used.items():
            sh = q / tot
            if k.max_supplier_share is not None:
                add(
                    "supplier_count_and_share",
                    sh <= k.max_supplier_share + 1e-9,
                    f"{P['sup'][u]['code']}: share {sh:.3f} vs max {k.max_supplier_share:g}",
                    k.max_supplier_share - sh,
                )
            if k.min_supplier_share is not None:
                add(
                    "supplier_count_and_share",
                    sh >= k.min_supplier_share - 1e-9,
                    f"{P['sup'][u]['code']}: share {sh:.3f} vs min {k.min_supplier_share:g}",
                    sh - k.min_supplier_share,
                )
    return rows


def metric_constraints(P: dict[str, Any], req: Any, m: dict[str, Any]) -> list[dict[str, Any]]:
    pc = req.plan_constraints
    rows: list[dict[str, Any]] = []

    def add(cid: str, ok: bool, detail: str, slack: float | None) -> None:
        rows.append({"id": cid, "passed": ok, "detail": detail, "slack": slack, "binding": ok and slack is not None and abs(slack) <= 1e-6 * max(1.0, abs(slack) + 1.0)})  # fmt: skip

    if pc.budget_ceiling is not None:
        c = m["cost"]["total_known"]
        add("budget_ceiling", c is not None and c <= pc.budget_ceiling + 1e-7, f"known cost {c if c is None else round(c, 2)} vs ceiling {pc.budget_ceiling:g}", None if c is None else pc.budget_ceiling - c)  # fmt: skip
    if pc.min_baseline_fulfilment is not None:
        f = m["baseline_fulfilment"]
        add(
            "min_baseline_fulfilment",
            f >= pc.min_baseline_fulfilment - 1e-9,
            f"fulfilment {f:.4f} vs target {pc.min_baseline_fulfilment:g}",
            f - pc.min_baseline_fulfilment,
        )
    if pc.min_worst_case_fulfilment is not None:
        w = m["values"]["WORST_CASE_SERVICE"]
        add("min_worst_case_fulfilment", w is not None and w >= pc.min_worst_case_fulfilment - 1e-9, f"worst case {w if w is None else round(w, 4)} vs target {pc.min_worst_case_fulfilment:g}", None if w is None else w - pc.min_worst_case_fulfilment)  # fmt: skip
    if pc.max_hhi is not None:
        h = m["values"]["CONCENTRATION"]
        add(
            "max_hhi / max_delay_days",
            h is not None and h <= pc.max_hhi + 1e-9,
            f"HHI {h if h is None else round(h, 4)} vs ceiling {pc.max_hhi:g}",
            None if h is None else pc.max_hhi - h,
        )
    if pc.max_delay_days is not None:
        d = m["values"]["DELIVERY_DELAY"]
        add("max_hhi / max_delay_days", d is not None and d <= pc.max_delay_days + 1e-9, f"lead time {d if d is None else round(d, 2)} d vs ceiling {pc.max_delay_days:g}", None if d is None else pc.max_delay_days - d)  # fmt: skip
    return rows


class Budget(Exception):
    pass


def _levels(P: dict[str, Any], req: Any, uid: str, steps: int) -> list[tuple[int, float]]:
    s = P["sup"][uid]
    D, k = P["D"], req.constraints
    out: list[tuple[int, float]] = [(0, 0.0)]
    seen: set[float] = set()
    cap = float(s["capacity"])
    for c in range(1, steps + 1):
        q = c / steps * D
        if s.get("lot"):
            q = math.floor(q / s["lot"] + 0.5) * s["lot"]
        q = round(q, 9)
        if q <= EPS or q in seen:
            continue
        if q > cap + 1e-7 * max(1, cap) or (s.get("moq") and q < s["moq"] - 1e-7):
            continue
        if k.max_supplier_share is not None and q > k.max_supplier_share * D * (1 + k.overage_pct / 100.0) + 1e-7:
            continue
        seen.add(q)
        out.append((c, q))
    return out


def generate(
    P: dict[str, Any], req: Any, steps: int, max_candidates: int, stop: Callable[[], None] | None
) -> tuple[list[dict[str, float]], dict[str, Any]]:
    """Enumerate allocations on the share grid (units of demand/steps). Deterministic order. Returns plans and a statistics block."""
    order = list(P["order"])
    levels = [_levels(P, req, u, steps) for u in order]
    k = req.constraints
    max_n = k.max_suppliers
    min_units = 0 if P["allow_partial"] else steps
    plans: list[dict[str, float]] = []
    stats = {"steps": steps, "leaves_visited": 0, "structural_rejected": 0, "truncated": False, "levels_per_supplier": {u: len(lv) - 1 for u, lv in zip(order, levels)}}  # fmt: skip
    leaf_cap = max_candidates * 4
    top = P["D"] * (1 + k.overage_pct / 100.0)

    def rec(i: int, units: int, cur: dict[str, float], used: int) -> None:
        if stats["truncated"]:
            return
        if i == len(order):
            stats["leaves_visited"] += 1
            if stats["leaves_visited"] % 500 == 0 and stop:
                stop()
            if stats["leaves_visited"] > leaf_cap:
                stats["truncated"] = True
                return
            if units < min_units or not cur:
                return
            tot = sum(cur.values())
            if tot > top + 1e-7 * max(1, top):
                stats["structural_rejected"] += 1
                return
            plan = dict(cur)
            if any(not r["passed"] for r in structural(P, req, plan)):
                stats["structural_rejected"] += 1
                return
            plans.append(plan)
            if len(plans) >= max_candidates:
                stats["truncated"] = True
            return
        for c, q in levels[i]:
            if units + c > steps:
                break
            if c > 0 and max_n is not None and used + 1 > max_n:
                continue
            if c > 0:
                cur[order[i]] = q
            rec(i + 1, units + c, cur, used + (1 if c > 0 else 0))
            if c > 0:
                del cur[order[i]]

    rec(0, 0, {}, 0)
    return plans, stats
