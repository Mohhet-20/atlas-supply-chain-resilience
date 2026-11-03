"""Pure inventory calculations: quantities, demand rate, days of inventory, policy, projection, classification.

No database access. Every function returns plain dicts that name the formula, the inputs, the assumptions and any
warnings; an unavailable result is ``None`` with a status, never a fabricated number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import NormalDist, pstdev
from typing import Any

from app.inventory.config import DEMAND_PRIORITY

EPS = 1e-9
LEAD_TIME_DAYS = {"hour": 1 / 24, "hours": 1 / 24, "day": 1.0, "days": 1.0, "week": 7.0, "weeks": 7.0}


def norm_unit(unit: str | None) -> str | None:
    u = (unit or "").strip().lower()
    return u or None


def r6(x: float | None) -> float | None:
    return None if x is None else round(float(x), 6)


# ----------------------------------------------------------------------------------------------------- quantities


@dataclass(frozen=True)
class Position:
    id: int
    product_id: int
    facility_id: int
    unit: str
    observed_at: datetime
    on_hand: float | None
    reserved: float | None = None
    allocated: float | None = None
    quarantined: float | None = None
    damaged: float | None = None
    usable_source: float | None = None  # a trusted source field, kept apart from the calculated value
    atp_source: float | None = None  # available-to-promise as supplied by the source
    in_transit: float | None = None
    backordered: float | None = None


def _utc(d: datetime) -> datetime:
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def derive_quantities(
    p: Position, *, product_unit: str | None, as_of: datetime, max_age_days: int, allocated_overlap: str
) -> dict[str, Any]:
    """Usable and available inventory with their formulas. Raw source fields are returned unchanged under ``source``."""
    as_of = _utc(as_of)
    obs = _utc(p.observed_at)
    age = (as_of - obs).total_seconds() / 86400
    out: dict[str, Any] = {
        "status": "OK", "unit": p.unit, "observed_at": obs.isoformat(), "age_days": r6(age), "stale": False,
        "usable": None, "available": None, "inventory_position_before_inbound": None,
        "source": {k: getattr(p, k) for k in ("on_hand", "reserved", "allocated", "quarantined", "damaged", "usable_source", "atp_source", "in_transit", "backordered")},
        "formulas": {}, "assumptions": [], "warnings": [], "unreported": [],
    }  # fmt: skip
    if p.on_hand is None:
        out["status"] = "MISSING_INVENTORY"
        out["warnings"].append("On-hand quantity is not reported, so no derived quantity is calculated.")
        return out
    if product_unit and norm_unit(product_unit) != norm_unit(p.unit):
        out["status"] = "INCOMPATIBLE_UNITS"
        out["warnings"].append(
            f"Unit '{p.unit}' differs from the product's registered unit '{product_unit}'; no conversion is applied."
        )
        return out
    if obs > as_of:
        out["status"] = "FUTURE_DATED"
        out["warnings"].append("The observation is dated after the analysis time and is not used.")
        return out
    if p.on_hand < 0:
        out["status"] = "INCONSISTENT"
        out["warnings"].append("On-hand quantity is negative; negative balances are not valid positions.")
        return out
    out["stale"] = age > max_age_days
    if out["stale"]:
        out["warnings"].append(f"Observation is {age:.1f} days old (limit {max_age_days}).")
    comps = {"reserved": p.reserved, "quarantined": p.quarantined, "damaged": p.damaged}
    out["unreported"] = [k for k, v in comps.items() if v is None]
    excl = sum(v for v in comps.values() if v is not None)
    if any(v is not None and v < 0 for v in comps.values()):
        out["status"] = "INCONSISTENT"
        out["warnings"].append("A reserved, quarantined or damaged quantity is negative.")
        return out
    if excl > p.on_hand + EPS:
        out["status"] = "INCONSISTENT"
        out["warnings"].append(
            f"Reserved + quarantined + damaged ({excl:g}) exceed on-hand ({p.on_hand:g}); the categories may overlap or the data is wrong, so nothing is subtracted."
        )
        return out
    usable = p.on_hand - excl
    out["usable"] = r6(usable)
    out["formulas"]["usable"] = "on_hand - reserved - quarantined - damaged (reported components only)"
    if out["unreported"]:
        out["assumptions"].append(
            f"Not reported, so not subtracted: {', '.join(out['unreported'])}. Usable inventory may be overstated."
        )
    avail = usable
    if allocated_overlap == "DISTINCT" and p.allocated is not None:
        avail = usable - p.allocated
        out["formulas"]["available"] = "usable - allocated (allocations declared distinct from reservations)"
    else:
        out["formulas"]["available"] = "usable (allocated quantity not subtracted)"
        if p.allocated is not None:
            out["assumptions"].append(
                "Allocated quantity is not subtracted because it is "
                + (
                    "declared to be included in reserved."
                    if allocated_overlap == "INCLUDED_IN_RESERVED"
                    else "of unknown overlap with reserved; subtracting could count units twice."
                )
            )
    if avail < -EPS:
        out["warnings"].append(f"Allocations exceed usable inventory by {-avail:g}: over-committed.")
        out["over_committed"] = True
    out["available"] = r6(avail)
    if p.in_transit is not None:
        out["assumptions"].append(
            "In-transit quantity is tracked separately and is never added to on-hand or available."
        )
    if p.backordered is not None:
        out["inventory_position_before_inbound"] = r6(avail - p.backordered)
        out["formulas"]["inventory_position_before_inbound"] = (
            "available - backordered (eligible inbound is added only in a projection)"
        )
    if p.usable_source is not None and out["usable"] is not None and abs(p.usable_source - usable) > 1e-6:
        out["warnings"].append(
            f"Source usable ({p.usable_source:g}) differs from calculated usable ({usable:g}); both are kept."
        )
    return out


# --------------------------------------------------------------------------------------------------------- demand


@dataclass(frozen=True)
class DemandObs:
    id: int
    period_start: date
    period_end: date  # exclusive
    quantity: float
    unit: str
    kind: str


def demand_rate(
    obs: list[DemandObs], *, unit: str | None, as_of_date: date, window_days: int, basis: str,
    min_obs: int, min_coverage: float,
) -> dict[str, Any]:  # fmt: skip
    """Average daily demand over covered days only. Missing periods are never read as zero demand."""
    w0, w1 = as_of_date - timedelta(days=window_days), as_of_date
    res: dict[str, Any] = {
        "status": "insufficient_demand_history", "rate_per_day": None, "unit": unit, "basis": None,
        "window": {"start": w0.isoformat(), "end": w1.isoformat(), "days": window_days},
        "usable_observations": 0, "covered_days": 0, "missing_days": window_days, "coverage_ratio": 0.0,
        "excluded": {"outside_window": 0, "partial_window": 0, "unit_mismatch": 0, "other_kind": 0},
        "intermittent": False, "daily_rate_stddev": None, "latest_period_end": None, "observation_ids": [],
        "warnings": [], "formula": "sum(quantity of one demand kind in fully-contained periods) / covered days",
    }  # fmt: skip
    pool = []
    for o in obs:
        if o.period_end <= o.period_start:
            continue
        if o.period_end <= w0 or o.period_start >= w1:
            res["excluded"]["outside_window"] += 1
        elif o.period_start < w0 or o.period_end > w1:
            res["excluded"]["partial_window"] += 1
        elif unit and norm_unit(o.unit) != norm_unit(unit):
            res["excluded"]["unit_mismatch"] += 1
        else:
            pool.append(o)
    kinds = [k for k in DEMAND_PRIORITY if any(o.kind == k for o in pool)]
    if basis != "AUTO":
        kinds = [k for k in kinds if k == basis]
    if not kinds:
        if res["excluded"]["unit_mismatch"] and not pool:
            res["status"] = "incompatible_units"
            res["warnings"].append(
                "Every in-window observation uses a different unit from the inventory; no conversion is applied."
            )
        else:
            res["warnings"].append("No usable demand observations in the window.")
        return res
    kind = kinds[0]
    res["basis"] = kind
    res["excluded"]["other_kind"] = sum(1 for o in pool if o.kind not in (kind, "ADJUSTMENT"))
    base = sorted((o for o in pool if o.kind == kind), key=lambda o: (o.period_start, o.period_end))
    adj = [o for o in pool if o.kind == "ADJUSTMENT"]
    for a, b in zip(base, base[1:], strict=False):
        if b.period_start < a.period_end:
            res["status"] = "conflicting_observations"
            res["warnings"].append(
                "Demand periods of the same kind overlap, so quantities would be counted twice; no rate is calculated."
            )
            return res
    covered = sum((o.period_end - o.period_start).days for o in base)
    net = sum(o.quantity for o in base) + sum(o.quantity for o in adj)
    res.update(
        usable_observations=len(base), covered_days=covered, missing_days=max(0, window_days - covered),
        coverage_ratio=r6(min(1.0, covered / window_days)), observation_ids=[o.id for o in base + adj],
        latest_period_end=max(o.period_end for o in base).isoformat(),
    )  # fmt: skip
    if adj:
        res["warnings"].append(f"{len(adj)} adjustment observation(s) were netted into the total.")
    daily = [o.quantity / (o.period_end - o.period_start).days for o in base]
    if len(daily) >= 3:
        res["daily_rate_stddev"] = r6(pstdev(daily))
    res["intermittent"] = sum(1 for o in base if o.quantity == 0) / len(base) >= 0.5
    if res["intermittent"]:
        res["warnings"].append(
            "Intermittent demand: half or more of the observed periods are zero; an average rate is a weak guide."
        )
    if (w1 - max(o.period_end for o in base)).days > window_days / 2:
        res["warnings"].append("The most recent demand observation is old relative to the window.")
    if len(base) < min_obs or res["coverage_ratio"] < min_coverage:
        res["warnings"].append(
            f"Need at least {min_obs} observations covering {min_coverage:.0%} of the window; have {len(base)} covering {res['coverage_ratio']:.0%}."
        )
        return res
    if net < -EPS:
        res["status"] = "invalid_negative_total"
        res["warnings"].append("Adjustments make the net demand negative; no rate is calculated.")
        return res
    res["rate_per_day"] = r6(net / covered)
    res["status"] = "zero_observed_demand" if abs(net) <= EPS else "calculable"
    if res["status"] == "zero_observed_demand":
        res["warnings"].append(
            "Observed demand was zero in this window; this does not imply future demand will be zero."
        )
    return res


# ----------------------------------------------------------------------------------------------- days of inventory


def days_of_inventory(qty: dict[str, Any], dem: dict[str, Any], *, basis: str = "usable") -> dict[str, Any]:
    """Eligible usable inventory / average daily demand, with an explicit status instead of infinity."""
    out = {"status": "unknown", "days": None, "formula": "eligible usable inventory / average daily demand", "unit": qty.get("unit"), "reason": None}  # fmt: skip
    st = qty.get("status")
    if st == "MISSING_INVENTORY":
        out.update(status="missing_inventory", reason="No on-hand quantity was reported.")
    elif st == "INCOMPATIBLE_UNITS" or dem.get("status") == "incompatible_units":
        out.update(
            status="incompatible_units", reason="Inventory and demand units are not compatible; nothing is converted."
        )
    elif st in ("INCONSISTENT", "FUTURE_DATED"):
        out.update(status="unknown", reason="The inventory observation is inconsistent or future-dated.")
    elif qty.get("stale"):
        out.update(status="stale_inventory", reason="The inventory observation is older than the allowed age.")
    else:
        inv = qty.get("usable") if basis == "usable" else qty.get("available")
        if inv is None:
            out.update(status="unknown", reason="Usable inventory could not be calculated.")
        else:
            inv = max(0.0, inv)
            if dem["status"] == "calculable":
                if inv <= EPS:
                    out.update(
                        status="zero_inventory", days=0.0, reason="No usable inventory with positive observed demand."
                    )
                else:
                    out.update(status="calculable", days=r6(inv / dem["rate_per_day"]), reason=None)
            elif inv <= EPS:
                out.update(
                    status="zero_inventory",
                    days=0.0 if dem["status"] == "zero_observed_demand" else None,
                    reason="No usable inventory.",
                )
            elif dem["status"] == "zero_observed_demand":
                out.update(
                    status="zero_observed_demand",
                    reason="Observed demand was zero in the selected window; coverage is not computed and zero is not assumed for the future.",
                )
            else:
                out.update(
                    status="insufficient_demand_history",
                    reason="; ".join(dem.get("warnings") or ["Demand history is insufficient."]),
                )
    return out


# ---------------------------------------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class PolicyIn:
    id: int
    version: int
    unit: str
    safety_stock: float | None = None
    reorder_point: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    target: float | None = None
    lead_time_days: float | None = None
    review_period_days: float | None = None
    service_level: float | None = None


def lead_time_to_days(value: float, unit: str) -> float:
    f = LEAD_TIME_DAYS.get((unit or "").strip().lower())
    if f is None:
        raise ValueError(f"Unsupported lead-time unit '{unit}'. Use hours, days or weeks.")
    if value < 0:
        raise ValueError("Lead time cannot be negative.")
    return value * f


def policy_problems(p: PolicyIn) -> list[str]:
    bad: list[str] = []
    for name in (
        "safety_stock",
        "reorder_point",
        "minimum",
        "maximum",
        "target",
        "lead_time_days",
        "review_period_days",
    ):
        v = getattr(p, name)
        if v is not None and (not math.isfinite(v) or v < 0):
            bad.append(f"{name} must be a non-negative number.")
    if p.service_level is not None and not (0.5 <= p.service_level < 1):
        bad.append("service_level must be at least 0.5 and below 1.")
    if p.minimum is not None and p.maximum is not None and p.minimum > p.maximum:
        bad.append("minimum cannot exceed maximum.")
    if p.safety_stock is not None and p.reorder_point is not None and p.reorder_point < p.safety_stock:
        bad.append("reorder_point cannot be below safety_stock.")
    if p.target is not None and p.maximum is not None and p.target > p.maximum:
        bad.append("target cannot exceed maximum.")
    if p.target is not None and p.minimum is not None and p.target < p.minimum:
        bad.append("target cannot be below minimum.")
    return bad


def computed_reorder_point(
    rate: float | None, lead_time_days: float | None, safety_stock: float | None
) -> dict[str, Any]:
    out = {"value": None, "formula": "average daily demand x lead time (days) + safety stock", "reason": None}
    if rate is None:
        out["reason"] = "Demand rate unavailable."
    elif lead_time_days is None:
        out["reason"] = "Lead time unavailable."
    elif safety_stock is None:
        out["reason"] = "Safety stock is not configured; it is not assumed to be zero."
    else:
        out["value"] = r6(rate * lead_time_days + safety_stock)
    return out


def estimate_safety_stock(
    rate: float | None, sigma_daily: float | None, lead_time_days: float | None, service_level: float | None
) -> dict[str, Any]:
    """z * sigma_d * sqrt(lead time). Lead-time variability is NOT supplied anywhere, so it is excluded and said so."""
    out = {"value": None, "method": "z(service level) x demand stddev per day x sqrt(lead time days)", "provenance": "MODEL_ESTIMATE",
           "caveat": "Lead-time variability is not recorded and is excluded. Assumes independent days. Shown for review; never used for classification.", "reason": None}  # fmt: skip
    if None in (rate, sigma_daily, lead_time_days, service_level):
        out["reason"] = "Needs demand variability (3+ observations), lead time and a service level."
    else:
        out["value"] = r6(NormalDist().inv_cdf(service_level) * sigma_daily * math.sqrt(lead_time_days))
    return out


def evaluate_policy(policy: PolicyIn | None, basis_qty: float | None, rate: float | None) -> dict[str, Any]:
    """Compare the inventory basis with configured thresholds. A violation is a finding, not an order."""
    out: dict[str, Any] = {"has_policy": policy is not None, "violations": [], "thresholds": {}, "computed_reorder_point": None, "reorder_point_used": None, "reorder_point_source": None, "missing": []}  # fmt: skip
    if policy is None:
        out["violations"].append("MISSING_POLICY")
        return out
    out["thresholds"] = {
        k: getattr(policy, k)
        for k in (
            "safety_stock",
            "reorder_point",
            "minimum",
            "maximum",
            "target",
            "lead_time_days",
            "review_period_days",
            "service_level",
        )
    }
    comp = computed_reorder_point(rate, policy.lead_time_days, policy.safety_stock)
    out["computed_reorder_point"] = comp
    if policy.reorder_point is not None:
        out["reorder_point_used"], out["reorder_point_source"] = policy.reorder_point, "CONFIGURED"
    elif comp["value"] is not None:
        out["reorder_point_used"], out["reorder_point_source"] = comp["value"], "COMPUTED"
    for k in ("safety_stock", "reorder_point"):
        if getattr(policy, k) is None:
            out["missing"].append(k)
    if basis_qty is None:
        return out
    if policy.safety_stock is not None and basis_qty < policy.safety_stock - EPS:
        out["violations"].append("BELOW_SAFETY_STOCK")
    if out["reorder_point_used"] is not None and basis_qty <= out["reorder_point_used"] + EPS:
        out["violations"].append("AT_OR_BELOW_REORDER_POINT")
    if policy.minimum is not None and basis_qty < policy.minimum - EPS:
        out["violations"].append("BELOW_MINIMUM")
    if policy.maximum is not None and basis_qty > policy.maximum + EPS:
        out["violations"].append("ABOVE_MAXIMUM")
    return out


# ------------------------------------------------------------------------------------------------------ projection


@dataclass(frozen=True)
class Receipt:
    id: int
    quantity: float
    unit: str
    expected_date: date | None
    status: str
    supplier_id: int | None = None
    reference: str | None = None


def classify_receipts(
    receipts: list[Receipt], *, as_of_date: date, unit: str | None, eligible: tuple[str, ...], horizon_days: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:  # fmt: skip
    """Split replenishments into those a projection may count and those it must not, each with the reason."""
    use, skip = [], []
    for r in sorted(receipts, key=lambda r: (r.expected_date or date.max, r.id)):
        why = None
        if r.status not in eligible:
            why = f"STATUS_{r.status}_NOT_ELIGIBLE"
        elif unit and norm_unit(r.unit) != norm_unit(unit):
            why = "UNIT_MISMATCH"
        elif r.expected_date is None:
            why = "NO_EXPECTED_DATE"
        elif r.expected_date <= as_of_date:
            why = "OVERDUE_OR_DUE_NOW"  # not yet counted in on-hand, so its arrival is unproven
        elif (r.expected_date - as_of_date).days > horizon_days:
            why = "BEYOND_HORIZON"
        elif r.quantity <= 0:
            why = "NON_POSITIVE_QUANTITY"
        if why:
            skip.append({"id": r.id, "reason": why, "quantity": r.quantity, "status": r.status, "expected_date": r.expected_date.isoformat() if r.expected_date else None})  # fmt: skip
        else:
            use.append({"id": r.id, "day": (r.expected_date - as_of_date).days, "quantity": r.quantity, "status": r.status, "supplier_id": r.supplier_id, "reference": r.reference, "expected_date": r.expected_date.isoformat()})  # fmt: skip
    return use, skip


def project(
    opening: float, rate: float, receipts: list[dict[str, Any]], horizon: int, thresholds: dict[str, float | None]
) -> dict[str, Any]:  # fmt: skip
    """Opening + receipts - constant demand, daily steps. Closing inventory is floored at zero; the shortfall is tracked."""
    return project_series(opening, [rate] * horizon, receipts, thresholds)


def project_series(
    opening: float, demand: list[float], receipts: list[dict[str, Any]], thresholds: dict[str, float | None], *, formula: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Opening + receipts - per-day demand (``demand[d-1]`` is consumed on day ``d``). The constant-rate projection is the
    special case of a flat series. Closing inventory is floored at zero; the shortfall is tracked."""
    horizon = len(demand)
    opening = max(0.0, opening)
    by_day: dict[int, float] = {}
    for r in receipts:
        if 1 <= r["day"] <= horizon:
            by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["quantity"]
    closing = [opening]
    unmet = 0.0
    stockout_day: int | None = 0 if opening <= EPS else None
    est: float | None = 0.0 if opening <= EPS else None
    crossings: dict[str, int | None] = {k: None for k, v in thresholds.items() if v is not None}
    for k, v in thresholds.items():
        if v is not None and opening <= v + EPS:
            crossings[k] = 0
    for d in range(1, horizon + 1):
        rate = demand[d - 1]
        prev = closing[-1]
        avail = prev + by_day.get(d, 0.0)
        if avail + EPS >= rate:
            now = max(0.0, avail - rate)
        else:
            unmet += rate - avail
            now = 0.0
        if stockout_day is None and now <= EPS:
            stockout_day = d
            est = (d - 1) + (min(avail / rate, 1.0) if rate > 0 else 0.0)
        closing.append(now)
        for k, v in thresholds.items():
            if v is not None and crossings[k] is None and now <= v + EPS:
                crossings[k] = d
    return {
        "series": [r6(x) for x in closing], "receipts_by_day": {str(k): r6(v) for k, v in sorted(by_day.items())},
        "min_closing": r6(min(closing)), "min_day": closing.index(min(closing)),
        "stockout_within_day": stockout_day, "estimated_stockout_after_days": r6(est),
        "stockout_precision": "daily steps: inventory reaches zero in (day-1, day]; the estimate assumes demand is spread evenly within a day; day 0 means already at zero",
        "crossings": crossings, "unfilled_demand": r6(unmet),
        "unfilled_demand_note": "Demand the projected stock cannot cover under the stated demand assumption. It is not a forecast of lost sales or backorders.",
        "receipts_counted": r6(sum(by_day.values())),
        "formula": formula or "closing(d) = max(0, closing(d-1) + receipts(d) - demand_rate)",
    }  # fmt: skip


# -------------------------------------------------------------------------------------------------- classification

SEVERITY_RANK = {
    "current_shortage": 4,
    "projected_stockout": 3,
    "low_coverage": 2,
    "within_policy": 1,
    "unknown": 0,
    "insufficient_data": 0,
}


def classify(
    qty: dict[str, Any], dem: dict[str, Any], doi: dict[str, Any], pol: dict[str, Any], proj: dict[str, Any] | None,
    *, low_coverage_days: float, backordered: float | None,
) -> dict[str, Any]:  # fmt: skip
    """Deterministic status with the exact condition that produced it."""

    def res(status: str, why: str, **kw: Any) -> dict[str, Any]:
        return {"status": status, "reason": why, "severity_rank": SEVERITY_RANK[status], **kw}

    qst = qty.get("status")
    if qst in ("MISSING_INVENTORY", "INCOMPATIBLE_UNITS", "INCONSISTENT", "FUTURE_DATED"):
        return res("insufficient_data", f"Inventory position unusable ({qst}).")
    if qty.get("stale"):
        return res("insufficient_data", "Inventory observation is stale; current status cannot be asserted.")
    basis = qty.get("basis_value")
    if basis is None:
        return res("insufficient_data", "Inventory basis could not be calculated.")
    viol = pol.get("violations", [])
    if "BELOW_SAFETY_STOCK" in viol or "BELOW_MINIMUM" in viol:
        which = "safety stock" if "BELOW_SAFETY_STOCK" in viol else "minimum"
        return res("current_shortage", f"Inventory ({basis:g}) is below the configured {which}.", basis="POLICY")
    if basis <= EPS and ((dem.get("status") == "calculable") or (backordered or 0) > 0):
        return res(
            "current_shortage",
            "No available inventory against positive observed demand or open backorders.",
            basis="DEMAND_OBLIGATION",
        )
    if proj is not None and proj.get("stockout_within_day") is not None:
        return res(
            "projected_stockout",
            f"Under the stated assumptions inventory reaches zero within {proj['stockout_within_day']} day(s).",
            basis="PROJECTION",
        )
    low = ("AT_OR_BELOW_REORDER_POINT" in viol) or (
        doi.get("status") == "calculable" and doi["days"] < low_coverage_days
    )
    if low:
        why = (
            "At or below the reorder point."
            if "AT_OR_BELOW_REORDER_POINT" in viol
            else f"Days of inventory ({doi['days']:g}) is below {low_coverage_days:g}."
        )
        return res("low_coverage", why, basis="POLICY" if "AT_OR_BELOW_REORDER_POINT" in viol else "COVERAGE")
    if proj is None:
        if pol.get("has_policy"):
            return res(
                "unknown",
                "Current inventory meets the configured thresholds but there is no demand basis to project forward.",
            )
        return res("insufficient_data", "No inventory policy and no usable demand basis.")
    if not pol.get("has_policy"):
        return res(
            "unknown",
            "No inventory policy applies; the projection shows no stockout in the horizon but coverage cannot be judged against a policy.",
        )
    if proj.get("crossings", {}).get("safety_stock") is not None:
        return res(
            "low_coverage",
            f"Projected to fall to the safety stock within {proj['crossings']['safety_stock']} day(s).",
            basis="PROJECTION",
        )
    return res(
        "within_policy", "Meets configured thresholds now and no stockout or safety-stock crossing in the horizon."
    )


def scenario_effect(
    finding: dict[str, Any] | None, durations: list[float | None], horizon: int, offset: int
) -> dict[str, Any]:
    """Translate an impact finding to a receipt effect. Qualitative exposure is never turned into a quantity."""
    if finding is None:
        return {"affected": False}
    if durations and all(d is not None for d in durations):
        end = offset + int(math.ceil(max(durations)))  # type: ignore[type-var]
        dur_known = True
    else:
        end, dur_known = horizon, False
    loss = finding.get("loss_pct")
    return {
        "affected": True, "loss_pct": loss, "loss_lower_bound_pct": finding.get("loss_pct_lower_bound"),
        "delay_days": finding.get("added_lead_time_days"), "window": [offset, min(end, horizon)], "duration_known": dur_known,
        "quantified": loss is not None, "impact_state": finding.get("impact_state"),
    }  # fmt: skip


def apply_effect(receipts: list[dict[str, Any]], eff: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    """mode: 'point' uses the quantified loss; 'worst' loses every receipt in the window; 'best' changes nothing."""
    if not eff.get("affected") or mode == "best":
        return [dict(r) for r in receipts]
    lo, hi = eff["window"]
    out = []
    for r in receipts:
        r = dict(r)
        if lo <= r["day"] <= hi:
            factor = 0.0 if mode == "worst" else (1 - (eff["loss_pct"] or 0.0) / 100)
            r["quantity"] = r["quantity"] * factor
            if mode == "point" and eff.get("delay_days"):
                r["day"] = r["day"] + int(math.ceil(eff["delay_days"]))
        out.append(r)
    return [r for r in out if r["quantity"] > EPS]
