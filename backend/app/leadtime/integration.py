"""Delivery-delay scenarios for the inventory projection (the inventory engine stays the authority).

Two explicit, analyst-chosen inputs are supported: a uniform ``receipt_delay_days`` applied to every counted receipt, or
``delivery_assessment_ids`` whose estimated arrival replaces the expected date of the matching receipt (same order
reference, product and facility). The projection is re-run with the shifted receipts and the *effect* is reported beside
the unchanged baseline; baseline classification and findings are never rewritten. No disruption-induced delay is invented.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError, NotFoundError
from app.inventory import calc
from app.leadtime.config import ALGORITHM_VERSION, STATEMENTS
from app.models.delivery import DeliveryAssessment


def validate_selectable(session: Session, ids: list[int]) -> None:
    rows = {a.id: a for a in session.scalars(select(DeliveryAssessment).where(DeliveryAssessment.id.in_(ids)))}
    for i in ids:
        a = rows.get(i)
        if a is None:
            raise NotFoundError(f"Delivery assessment {i} not found.")
        if a.status != "ASSESSED" or not a.arrival or a.arrival.get("status") != "estimated":
            raise DomainValidationError(f"Delivery assessment {i} has no estimated arrival (status {a.status}); it cannot drive an inventory scenario.")  # fmt: skip


def load_context(session: Session, ids: list[int] | None) -> dict[str, Any]:
    by_key: dict[tuple[str, int, int], DeliveryAssessment] = {}
    for a in session.scalars(select(DeliveryAssessment).where(DeliveryAssessment.id.in_(ids or []))):
        k = (a.delivery_ref or "", a.product_id, a.destination_facility_id)
        if k not in by_key or a.id > by_key[k].id:
            by_key[k] = a
    return {"by_key": by_key, "ids": sorted(ids or [])}


def _summary(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p[k] for k in ("min_closing", "min_day", "stockout_within_day", "estimated_stockout_after_days", "crossings", "unfilled_demand", "receipts_counted")}  # fmt: skip


def _days_below(series: list[float], limit: float | None) -> int | None:
    return None if limit is None else sum(1 for x in series[1:] if x <= limit + 1e-9)


def effect(
    *, proj: dict[str, Any], use: list[dict[str, Any]], demand: list[float], opening: float, thresholds: dict[str, float | None],
    as_of_date: date, pid: int, fid: int, delay_days: float | None, basis: str, ctx: dict[str, Any] | None,
) -> dict[str, Any]:  # fmt: skip
    horizon = len(demand)
    adj: list[dict[str, Any]] = []
    shifted: list[dict[str, Any]] = []
    for r in use:
        new_day, aid, note = r["day"], None, None
        if delay_days is not None:
            new_day = r["day"] + math.ceil(delay_days)
            note = "uniform explicit delay (rounded up to whole days)"
        elif ctx is not None:
            a = ctx["by_key"].get((r.get("reference") or "", pid, fid))
            if a is not None:
                arr = a.arrival or {}
                stamp = (
                    (arr.get("window") or {}).get("latest")
                    if basis == "UPPER_BOUND" and arr.get("window")
                    else arr.get("estimated_arrival")
                )
                if stamp:
                    ts = datetime.fromisoformat(stamp)
                    new_day = math.ceil(
                        (ts.date() - as_of_date).days + (ts.hour * 3600 + ts.minute * 60 + ts.second) / 86400
                    )
                    aid = a.id
                    note = f"assessment {a.assessment_uid} ({'upper bound' if basis == 'UPPER_BOUND' and arr.get('window') else 'point estimate'}, rounded up to whole days)"
        adj.append({"receipt_id": r["id"], "reference": r.get("reference"), "expected_day": r["day"], "adjusted_day": new_day, "shift_days": new_day - r["day"], "assessment_id": aid, "quantity": r["quantity"], "basis": note or "unchanged: no matching assessment", "beyond_horizon": new_day > horizon})  # fmt: skip
        shifted.append({**r, "day": max(1, new_day)})
    p2 = calc.project_series(opening, demand, shifted, thresholds, formula=proj.get("formula"))
    b, w = proj, p2
    ss = thresholds.get("safety_stock")
    cmp: dict[str, Any] = {
        "earlier_threshold_crossing": {k: {"baseline_day": b["crossings"].get(k), "with_delay_day": w["crossings"].get(k)} for k in w["crossings"] if (w["crossings"][k] is not None) and (b["crossings"].get(k) is None or w["crossings"][k] < b["crossings"][k])},  # fmt: skip
        "lower_minimum_inventory": w["min_closing"] < b["min_closing"] - 1e-9,
        "min_closing_change": round(w["min_closing"] - b["min_closing"], 6),
        "days_at_or_below_safety_stock": {"baseline": _days_below(b["series"], ss), "with_delay": _days_below(w["series"], ss)},
        "potential_stockout": (w["stockout_within_day"] is not None) and (b["stockout_within_day"] is None or w["stockout_within_day"] < b["stockout_within_day"]),  # fmt: skip
        "days_out_of_stock": {"baseline": sum(1 for x in b["series"][1:] if x <= 1e-9), "with_delay": sum(1 for x in w["series"][1:] if x <= 1e-9)},
        "additional_unfilled_demand": round(w["unfilled_demand"] - b["unfilled_demand"], 6),
    }  # fmt: skip
    matched = sum(1 for a in adj if a["assessment_id"] is not None)
    return {
        "status": "calculated", "scenario": "UNIFORM_DELAY" if delay_days is not None else "ASSESSED_ARRIVALS", "delay_days": delay_days,
        "basis": basis if delay_days is None else "explicit", "receipt_adjustments": adj, "receipts_matched_to_assessments": matched if delay_days is None else None,
        "baseline": _summary(b), "with_delay": _summary(w), "with_delay_series": w["series"], "comparison": cmp,
        "versions": {"leadtime": ALGORITHM_VERSION, "assessment_ids": (ctx or {}).get("ids", []), "projection_formula": proj.get("formula")},
        "statements": [STATEMENTS["decision"], "This is a conditional what-if on the recorded receipts, not a prediction that the delay will occur. The baseline projection and classification above are unchanged.", "No disruption-induced delay is assumed: the delay is the explicit value or the cited assessment."],  # fmt: skip
    }  # fmt: skip
