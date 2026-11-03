"""Order-quantity and replenishment-rule mathematics (pure). Hard constraints are applied here and never traded for score.

EOQ: ``Q* = sqrt(2 D K / h)`` with ``D`` demand per day, ``K`` cost per order and ``h`` holding cost per unit per day. It assumes
constant demand, a fixed order cost, linear holding cost, no quantity discounts and no shortages; it is refused when any input is
missing or demand is intermittent, rather than applied mechanically.
"""

from __future__ import annotations

import math
from typing import Any

EPS = 1e-9


def r6(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)


def eoq(
    d_per_day: float | None, order_cost: float | None, holding_per_unit_day: float | None, *, intermittent: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {"value": None, "formula": "sqrt(2 x demand per day x order cost / holding cost per unit per day)", "reason": None, "assumptions": ["constant demand", "fixed cost per order", "linear holding cost", "no quantity discounts", "no planned shortages"]}  # fmt: skip
    if intermittent:
        out["reason"] = "EOQ assumes steady demand and is not applied to intermittent demand."
    elif d_per_day is None or d_per_day <= 0:
        out["reason"] = "A positive demand rate is required."
    elif order_cost is None or order_cost <= 0:
        out["reason"] = "The cost per order is unknown or zero, so EOQ is undefined."
    elif holding_per_unit_day is None or holding_per_unit_day <= 0:
        out["reason"] = "The holding cost is unknown or zero, so EOQ is undefined."
    else:
        out["value"] = r6(math.sqrt(2 * d_per_day * order_cost / holding_per_unit_day))
    return out


def round_up(q: float, lot: float | None) -> float:
    if not lot:
        return q
    return math.ceil(q / lot - 1e-9) * lot


def round_down(q: float, lot: float | None) -> float:
    if not lot:
        return q
    return math.floor(q / lot + 1e-9) * lot


def bounds(
    *, moq: float | None, lot: float | None, max_q: float | None, storage_headroom: float | None, budget: float | None, unit_cost: float | None,
) -> dict[str, Any]:  # fmt: skip
    """Smallest and largest order allowed by the hard constraints, the constraints that bind, and those that could not be verified."""
    lo = max(moq or 0.0, lot or 0.0)
    lo = round_up(lo, lot) if lo > 0 else 0.0
    caps: list[tuple[str, float]] = []
    unverified: list[str] = []
    if max_q is not None:
        caps.append(("MAX_ORDER_QUANTITY", max_q))
    if storage_headroom is not None:
        caps.append(("STORAGE_CAPACITY", max(0.0, storage_headroom)))
    if budget is not None:
        if unit_cost is not None and unit_cost > 0:
            caps.append(("BUDGET", budget / unit_cost))
        else:
            unverified.append("budget_per_order (the unit cost is unknown, so the budget cannot be checked)")
    hi = min((c for _, c in caps), default=None)
    hi_eff = None if hi is None else round_down(hi, lot)
    binding = [name for name, c in caps if hi is not None and abs(c - hi) < 1e-9]
    infeasible = []
    if hi_eff is not None and hi_eff + EPS < lo:
        infeasible = [
            {"code": name, "limit": r6(c), "minimum_order": r6(lo)} for name, c in caps if round_down(c, lot) + EPS < lo
        ]
        if not infeasible:
            infeasible = [{"code": "LOT_OR_MINIMUM_CONFLICT", "limit": r6(hi_eff), "minimum_order": r6(lo)}]
    return {
        "min": r6(lo),
        "max": r6(hi_eff),
        "binding": binding,
        "unverified": unverified,
        "infeasible": infeasible,
        "lot": lot,
    }


def fit_quantity(target: float, b: dict[str, Any]) -> dict[str, Any]:
    """Quantity to order for a wanted ``target`` inside the bounds ``b``. Flags every adjustment."""
    lo, hi, lot = b["min"] or 0.0, b["max"], b.get("lot")
    flags: list[str] = []
    if b["infeasible"]:
        return {"quantity": 0.0, "status": "INFEASIBLE", "flags": ["NO_FEASIBLE_QUANTITY"], "reasons": b["infeasible"]}
    if target <= EPS:
        return {"quantity": 0.0, "status": "NO_ORDER", "flags": [], "reasons": []}
    q = round_up(target, lot)
    if lot and q > target + EPS:
        flags.append("ROUNDED_UP_TO_LOT")
    if q < lo - EPS:
        q = lo
        flags.append("RAISED_TO_MINIMUM_ORDER")
    status = "ORDER"
    if hi is not None and q > hi + EPS:
        q = hi
        flags.append("CAPPED_BY_" + "_".join(b["binding"]) if b["binding"] else "CAPPED")
        status = "ORDER_CAPPED"
    return {"quantity": r6(q), "status": status, "flags": flags, "reasons": [], "shortfall_vs_target": r6(max(0.0, target - q)) if status == "ORDER_CAPPED" else 0.0}  # fmt: skip


def order_target(policy_type: str, ip: float, s: float, q: float | None, big_s: float | None) -> dict[str, Any]:
    """Quantity the policy rule asks for (before constraints) and whether it triggers."""
    if policy_type == "S_Q":
        if q is None or q <= 0:
            return {"trigger": ip <= s + EPS, "target": None, "reason": "Q is not defined"}
        trig = ip <= s + EPS
        return {"trigger": trig, "target": q * max(1, math.ceil((s - ip) / q + 1e-12)) if trig else 0.0, "rule": "inventory position <= s: order Q (or a multiple of Q that lifts the position above s)"}  # fmt: skip
    if big_s is None:
        return {"trigger": False, "target": None, "reason": "S is not defined"}
    if policy_type == "S_UPTO":
        trig = ip <= s + EPS
        return {
            "trigger": trig,
            "target": max(0.0, big_s - ip) if trig else 0.0,
            "rule": "inventory position <= s: order up to S",
        }
    need = big_s - ip
    return {"trigger": need > EPS, "target": max(0.0, need), "rule": "at each review: order up to S"}
