"""Load a consistent, JSON-serialisable snapshot of everything a resilience run reads.

Reads the sourcing offers/eligibility, the scenarios (through the sourcing overlay, on COPIES), and optionally the inventory
position. Writes nothing. The engine works only from this snapshot, so a stored run replays exactly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.resilience.config import Limits, ResilienceRequest, Stress, stable_hash
from app.sourcing import data as sdata
from app.sourcing import eligibility as elig
from app.sourcing import engine as seng
from app.sourcing import overlay as soverlay
from app.sourcing.config import Limits as SLimits
from app.sourcing.config import SourcingRequest


def sourcing_request(req: ResilienceRequest) -> SourcingRequest:
    return SourcingRequest(
        product_id=req.product_id, destination_facility_id=req.destination_facility_id, quantity=req.quantity, unit=req.unit, currency=req.currency,
        objective="LOWEST_COST", strategy="AUTO", fulfillment="PARTIAL" if req.allow_partial else "FULL", constraints=req.constraints, as_of=req.as_of,
    )  # fmt: skip


def _opening_stock(
    session: Session, req: ResilienceRequest, now: datetime, warnings: list[dict[str, str]]
) -> dict[str, Any]:
    if req.opening_stock is not None:
        return {"value": float(req.opening_stock), "source": "USER_SUPPLIED", "observed_at": None}
    if not req.use_inventory_position:
        return {"value": None, "source": None, "observed_at": None}
    try:
        from app.invopt import data as idata
        from app.invopt.config import Limits as ILimits
        from app.invopt.config import ReplenishRequest

        snap = idata.load(session, ReplenishRequest(product_id=req.product_id, facility_id=req.destination_facility_id, lead_time_days=1.0, as_of=now), now, ILimits())  # fmt: skip
        pos = snap.get("position") or {}
        usable = pos.get("usable")
        if usable is None:
            warnings.append({"code": "INVENTORY_POSITION_MISSING", "severity": "WARNING", "message": "No inventory observation was found for this product and destination, so no opening stock is assumed."})  # fmt: skip
            return {"value": None, "source": None, "observed_at": None}
        if pos.get("stale"):
            warnings.append(
                {
                    "code": "INVENTORY_POSITION_STALE",
                    "severity": "WARNING",
                    "message": "The inventory observation used for opening stock is stale.",
                }
            )
        return {"value": float(usable), "source": "INVENTORY_OBSERVATION", "observed_at": str(pos.get("observed_at"))}
    except Exception as exc:  # noqa: BLE001
        warnings.append({"code": "INVENTORY_POSITION_UNAVAILABLE", "severity": "WARNING", "message": f"The inventory position could not be read ({type(exc).__name__}); no opening stock is assumed."})  # fmt: skip
        return {"value": None, "source": None, "observed_at": None}


def _effects(cands: list[Any], base_eligible: dict[str, Any], mutate: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in cands:
        if c.uid not in base_eligible:
            continue
        out[c.uid] = {
            "available": c.unavailable_reason is None,
            "capacity": c.effective_capacity,
            "lead_days": c.lead_days,
        }
    return out


def load(session: Session, req: ResilienceRequest, now: datetime, limits: Limits) -> dict[str, Any]:
    sreq = sourcing_request(req)
    slim = SLimits()
    cands, ctx, warns, truncated = sdata.load_candidates(session, sreq, slim, now)
    warnings: list[dict[str, str]] = [{"code": "SOURCING_DATA", "severity": "INFO", "message": w} for w in warns]
    if truncated:
        warnings.append({"code": "CANDIDATES_TRUNCATED", "severity": "WARNING", "message": "The registered relationships were truncated by the sourcing candidate limit."})  # fmt: skip
    ass = {c.uid: elig.assess(c, ctx) for c in cands}
    suppliers = [
        {
            "uid": c.uid, "supplier_id": c.supplier_id, "code": c.supplier_code, "name": c.supplier_name, "country": c.country, "status": ass[c.uid]["status"],
            "reasons": ass[c.uid]["reasons"], "price": c.price, "freight": c.freight, "capacity": c.effective_capacity, "moq": c.moq, "lot": c.lot,
            "lead_days": c.lead_days, "lead_std_days": c.lead_std_days, "risk": c.risk, "reliability": {"value": c.reliability.value, "n": c.reliability.n},
            "provenance": c.provenance, "offer_status": c.offer_status,
        }
        for c in cands
    ]  # fmt: skip
    eligible = {s["uid"]: s for s in suppliers if s["status"] == "ELIGIBLE"}
    scenarios: list[dict[str, Any]] = []
    for st in req.stress:
        sc = _stress(session, st, cands, eligible, req, warnings)
        if sc is not None:
            scenarios.append(sc)
    anchors: list[dict[str, Any]] = []
    if eligible:
        try:
            res = seng.plan(cands, ctx, sreq, slim)
            if res["result_status"] in ("feasible", "partial"):
                anchors.append({"name": "SOURCING_EXACT_MIN_COST", "optimality": res["solver"].get("optimality"), "allocation": {a["offer_uid"]: a["quantity"] for a in res["allocations"]}})  # fmt: skip
        except Exception:  # noqa: BLE001
            warnings.append({"code": "ANCHOR_UNAVAILABLE", "severity": "INFO", "message": "The exact minimum-cost reference plan from Sourcing could not be computed."})  # fmt: skip
    snap = {
        "as_of": now.isoformat(), "product_id": req.product_id, "destination_facility_id": req.destination_facility_id, "unit": req.unit, "currency": req.currency,
        "demand": float(req.quantity), "suppliers": suppliers, "scenarios": scenarios, "opening_stock": _opening_stock(session, req, now, warnings), "anchors": anchors,
        "warnings": warnings, "algorithm_inputs": {"sourcing": "sc-1", "scenario_overlay": "sourcing.overlay"},
    }  # fmt: skip
    snap["hash"] = stable_hash({k: v for k, v in snap.items() if k != "warnings"})
    return snap


def _stress(session: Session, st: Stress, cands: list[Any], eligible: dict[str, Any], req: ResilienceRequest, warnings: list[dict[str, str]]) -> dict[str, Any] | None:  # fmt: skip
    base = {"name": st.name, "kind": st.kind, "probability": st.probability, "demand_multiplier": 1.0, "effects": {}, "outage": None, "applied": [], "not_applied": []}  # fmt: skip

    def targets() -> list[Any]:
        return [c for c in cands if c.uid in eligible and (st.supplier_id is None or c.supplier_id == st.supplier_id)]

    if st.kind == "DEMAND_SURGE":
        return {
            **base,
            "demand_multiplier": float(st.demand_multiplier or 1.0),
            "applied": [{"effect": f"demand x{st.demand_multiplier:g}"}],
        }
    if st.kind == "SUPPLIER_OUTAGE":
        if st.supplier_id is None:
            return {
                **base,
                "outage": "LARGEST",
                "applied": [{"effect": "the largest supplier of the evaluated plan is unavailable"}],
            }
        tg = targets()
        if not tg:
            warnings.append({"code": "SCENARIO_NOT_APPLIED", "severity": "WARNING", "message": f"Scenario '{st.name}' was left out: supplier {st.supplier_id} has no eligible offer for this requirement, so the outage changes nothing."})  # fmt: skip
            return None
        return {**base, "effects": {c.uid: {"available": False, "capacity": None, "lead_days": None} for c in tg}, "applied": [{"effect": f"supplier {st.supplier_id} unavailable", "offers": [c.uid for c in tg]}]}  # fmt: skip
    if st.kind in ("CAPACITY_DROP", "LEAD_TIME_INCREASE"):
        tg = targets()
        if not tg:
            warnings.append({"code": "SCENARIO_NOT_APPLIED", "severity": "WARNING", "message": f"Scenario '{st.name}' was left out: no eligible offer matches its target."})  # fmt: skip
            return None
        eff = {}
        for c in tg:
            if st.kind == "CAPACITY_DROP":
                eff[c.uid] = {
                    "available": True,
                    "capacity": c.effective_capacity * float(st.capacity_multiplier),
                    "lead_days": c.lead_days,
                }
            else:
                eff[c.uid] = {"available": True, "capacity": c.effective_capacity, "lead_days": None if c.lead_days is None else c.lead_days + float(st.add_lead_days)}  # fmt: skip
                if c.lead_days is None:
                    warnings.append({"code": "LEAD_TIME_UNKNOWN_IN_SCENARIO", "severity": "WARNING", "message": f"Scenario '{st.name}': offer {c.uid} has no lead time, so the increase could not be applied to it."})  # fmt: skip
        return {
            **base,
            "effects": eff,
            "applied": [{"effect": st.kind.lower().replace("_", " "), "offers": sorted(eff)}],
        }
    # SCENARIO: a registered, READY scenario mapped onto offers by the sourcing overlay (copies only)
    dis, applied, skipped, info = soverlay.from_scenario(
        session, int(st.scenario_id), cands, req.destination_facility_id
    )
    eff = _effects(dis, eligible, None)
    changed = {
        u: e
        for u, e in eff.items()
        if (not e["available"])
        or e["capacity"] != eligible[u]["capacity"]
        or e["lead_days"] != eligible[u]["lead_days"]
    }
    if not changed and not applied:
        warnings.append({"code": "SCENARIO_NOT_APPLIED", "severity": "WARNING", "message": f"Scenario '{st.name}' ({info['scenario_uid']}) has no event that maps to an eligible offer; it was left out rather than counted as harmless."})  # fmt: skip
        return None
    sc = {**base, "effects": eff, "applied": applied, "not_applied": skipped, "scenario": info}
    if skipped:
        warnings.append({"code": "SCENARIO_EVENTS_NOT_APPLIED", "severity": "WARNING", "message": f"Scenario '{st.name}': {len(skipped)} event(s) could not be mapped to supplier offers and were not applied."})  # fmt: skip
    return sc
