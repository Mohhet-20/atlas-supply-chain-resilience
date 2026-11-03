"""Input loading for one product-facility pair: everything the engine needs is gathered into one JSON snapshot.

The engine reads only the snapshot (never the database), so a stored run can be replayed bit-for-bit. Nothing here changes a stored
record. Missing values stay ``None`` with a reason; nothing is defaulted to zero. Time only flows forward: observations after ``as_of``
are never read, so a backtest cannot see the future.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecast import integration as fint
from app.inventory import analysis as inv
from app.inventory import calc
from app.inventory import config as invcfg
from app.invopt import safety
from app.invopt.config import Limits, ReplenishRequest, stable_hash
from app.leadtime import semantics as sem
from app.leadtime.resolve import load_deliveries
from app.models.facility import Facility
from app.models.forecast import ForecastValue
from app.models.invopt import InventoryCostProfile
from app.models.product import Product
from app.models.sourcing import SourcingOffer


def issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def _quantities(
    session: Session, req: ReplenishRequest, conf: invcfg.InventoryConfig, now: datetime, prod: Product, dq: list
) -> dict[str, Any]:
    pid, fid = req.product_id, req.facility_id
    p = inv.latest_positions(session, conf, now).get((pid, fid))
    out: dict[str, Any] = {"status": "MISSING_INVENTORY", "unit": prod.unit_of_measure, "usable": None, "available": None, "on_hand": None, "backordered": None, "in_transit_reported": None, "age_days": None, "stale": False, "observed_at": None, "position_id": None, "assumptions": [], "warnings": []}  # fmt: skip
    if p is None:
        dq.append(
            issue(
                "INVENTORY_MISSING",
                "ERROR",
                "No inventory observation exists at or before the as-of time; none is assumed.",
            )
        )
        return out
    q = calc.derive_quantities(
        calc.Position(p.id, pid, fid, p.unit, p.observed_at, p.on_hand, p.reserved, p.allocated, p.quarantined, p.damaged, p.usable_source, p.atp_source, p.in_transit, p.backordered),
        product_unit=prod.unit_of_measure, as_of=now, max_age_days=req.max_inventory_age_days, allocated_overlap=req.allocated_overlap,
    )  # fmt: skip
    out.update(status=q["status"], unit=q["unit"], usable=q["usable"], available=q["available"], on_hand=p.on_hand, backordered=p.backordered, in_transit_reported=p.in_transit, age_days=q["age_days"], stale=q["stale"], observed_at=q["observed_at"], position_id=p.id, assumptions=q["assumptions"], warnings=q["warnings"], source=q["source"], unreported=q["unreported"])  # fmt: skip
    if q["status"] != "OK":
        dq.append(issue(f"INVENTORY_{q['status']}", "ERROR", "; ".join(q["warnings"]) or q["status"]))
    elif q["stale"]:
        dq.append(
            issue(
                "INVENTORY_STALE",
                "WARNING",
                q["warnings"][0] if q["warnings"] else "The inventory observation is stale.",
            )
        )
    if q["status"] == "OK" and q["available"] is None and q["usable"] is not None:
        out["available"] = q["usable"]
        out["assumptions"].append("Allocated stock could not be separated from reserved; usable stock is used as available (may overstate available stock).")  # fmt: skip
    return out


def _receipts(
    session: Session, req: ReplenishRequest, conf: invcfg.InventoryConfig, now: datetime, unit: str | None
) -> dict[str, Any]:
    rows = inv.current_receipts(session, conf, now).get((req.product_id, req.facility_id), [])
    rec = [calc.Receipt(r.id, r.quantity, r.unit, r.expected_date, r.status.value if hasattr(r.status, "value") else str(r.status), r.supplier_id, r.reference) for r in rows]  # fmt: skip
    eligible = ("CONFIRMED", "IN_TRANSIT", "PLANNED") if req.include_planned_receipts else ("CONFIRMED", "IN_TRANSIT")
    use, skip = calc.classify_receipts(rec, as_of_date=now.date(), unit=unit, eligible=eligible, horizon_days=max(req.horizon_days, req.simulation_days))  # fmt: skip
    return {"counted": use, "excluded": skip, "eligible_statuses": list(eligible)}


def _observed_demand(
    session: Session,
    req: ReplenishRequest,
    conf: invcfg.InventoryConfig,
    now: datetime,
    unit: str | None,
    limits: Limits,
    dq: list,
) -> dict[str, Any]:
    rows = inv.window_demand(session, conf, now.date()).get((req.product_id, req.facility_id), [])
    obs = [calc.DemandObs(d.id, d.period_start, d.period_end, d.quantity, d.unit, d.kind.value) for d in rows]
    dm = calc.demand_rate(obs, unit=unit, as_of_date=now.date(), window_days=conf.demand_window_days, basis=req.demand_basis, min_obs=3, min_coverage=0.25)  # fmt: skip
    out: dict[str, Any] = {"status": dm["status"], "basis": dm["basis"], "rate_per_day": dm["rate_per_day"], "window": dm["window"], "coverage_ratio": dm["coverage_ratio"], "covered_days": dm["covered_days"], "usable_observations": dm["usable_observations"], "warnings": dm["warnings"], "observation_ids": dm["observation_ids"], "excluded": dm["excluded"], "periods": [], "daily": None, "stats": None, "latest_period_end": dm["latest_period_end"]}  # fmt: skip
    kind = dm["basis"]
    w0, w1 = now.date() - timedelta(days=conf.demand_window_days), now.date()
    if kind:
        base = sorted((o for o in obs if o.kind == kind and o.period_start >= w0 and o.period_end <= w1 and calc.norm_unit(o.unit) == calc.norm_unit(unit) and o.period_end > o.period_start), key=lambda o: o.period_start)  # fmt: skip
        periods = [((o.period_end - o.period_start).days, o.quantity) for o in base]
        out["periods"] = [[n, q] for n, q in periods]
        if base and all(n == 1 for n, _ in periods):
            out["daily"] = [q for _, q in periods]
        out["stats"] = safety.demand_stats(periods)
    code = {"calculable": None, "zero_observed_demand": "DEMAND_ZERO_OBSERVED"}.get(
        dm["status"], "DEMAND_" + dm["status"].upper()
    )
    if code:
        dq.append(
            issue(
                code,
                "WARNING" if dm["status"] == "zero_observed_demand" else "ERROR",
                "; ".join(dm["warnings"]) or dm["status"],
            )
        )
    elif out["stats"] and out["stats"]["intermittent"]:
        dq.append(issue("DEMAND_INTERMITTENT", "WARNING", "Demand is intermittent (half or more of the periods are zero); the normal methods are not used."))  # fmt: skip
    if out["stats"] and out["stats"]["resolution_days"] and out["stats"]["resolution_days"] > 1:
        dq.append(issue("DEMAND_COARSE_RESOLUTION", "INFO", f"Demand is recorded in periods of up to {out['stats']['resolution_days']} days; day-to-day variability inside a period cannot be observed, so the empirical (bootstrap) method is unavailable."))  # fmt: skip
    if dm["coverage_ratio"] and dm["coverage_ratio"] < 0.75 and kind:
        dq.append(issue("DEMAND_PARTIAL_COVERAGE", "INFO", f"Demand is observed on {dm['coverage_ratio']:.0%} of the window; unobserved days are not read as zero."))  # fmt: skip
    return out


def _forecast(
    session: Session, req: ReplenishRequest, now: datetime, unit: str | None, dq: list
) -> dict[str, Any] | None:
    if req.forecast_run_id is None:
        return None
    ctx = fint.validate_selectable(session, req.forecast_run_id)
    run = ctx["run"]
    if (run.product_id, run.facility_id) != (req.product_id, req.facility_id):
        raise ValueError("The forecast run is for a different product or facility.")
    ds = fint.demand_series(
        ctx,
        unit=unit,
        as_of_date=now.date(),
        horizon_days=max(req.horizon_days, 1),
        factor=req.forecast_conversion_factor,
    )
    out: dict[str, Any] = {"status": ds["status"], "reason": ds.get("reason"), "block": fint.source_block(run), "series": ds.get("series"), "warnings": ds.get("warnings", []), "conversion": ds.get("conversion"), "sigma_per_day": None, "sigma_basis": None}  # fmt: skip
    if ds["status"] != "usable":
        dq.append(issue("FORECAST_" + ds["status"].upper(), "WARNING", ds.get("reason") or "The forecast cannot drive this run; observed demand is used instead."))  # fmt: skip
        return out
    mult = ds["conversion"]["factor"] if ds.get("conversion") else 1.0
    vals = session.scalars(
        select(ForecastValue).where(ForecastValue.run_id == run.id).order_by(ForecastValue.step)
    ).all()
    sigs = []
    for v in vals:
        n = (v.period_end - v.period_start).days
        for iv in v.intervals or []:
            if iv.get("status") in ("evaluated", "calibrated", "ok", "supported", None) and iv.get("lower") is not None and iv.get("upper") is not None and 0.5 <= iv.get("level", 0) < 1 and n > 0:  # fmt: skip
                zz = safety.z((1 + iv["level"]) / 2)
                sigs.append(((iv["upper"] - iv["lower"]) * mult / (2 * zz)) / math.sqrt(n))
                break
    if sigs:
        out["sigma_per_day"] = safety.r6(sum(sigs) / len(sigs))
        out["sigma_basis"] = (
            "forecast prediction interval width / (2 z), divided by sqrt(period days); days within a period are treated as independent"
        )
    else:
        dq.append(issue("FORECAST_NO_INTERVAL", "INFO", "The forecast carries no usable prediction interval; demand variability comes from observed history, which describes historical rather than forecast error."))  # fmt: skip
    return out


def _offers(session: Session, req: ReplenishRequest, now: datetime, dq: list) -> dict[str, Any] | None:
    q = select(SourcingOffer).where(SourcingOffer.product_id == req.product_id, SourcingOffer.destination_facility_id == req.facility_id, SourcingOffer.retired_at.is_(None))  # fmt: skip
    if req.supplier_id:
        q = q.where(SourcingOffer.supplier_id == req.supplier_id)
    offers = [o for o in session.scalars(q.order_by(SourcingOffer.id)) if o.status != "RETIRED"]
    if not offers:
        if req.supplier_id:
            dq.append(issue("OFFER_NOT_FOUND", "WARNING", "No registered sourcing offer exists for this supplier, product and facility; supplier eligibility and terms are unverified."))  # fmt: skip
        return None
    if len(offers) > 1:
        dq.append(issue("OFFER_AMBIGUOUS", "INFO", f"{len(offers)} offers exist for this product and facility; supply supplier_id to use one. No offer terms are applied."))  # fmt: skip
        return {
            "ambiguous": True,
            "candidates": [{"offer_uid": o.offer_uid, "supplier_id": o.supplier_id} for o in offers[:20]],
        }
    o = offers[0]
    valid = (o.contract_valid_from is None or o.contract_valid_from <= now) and (
        o.contract_valid_to is None or o.contract_valid_to >= now
    )
    eligible = o.status == "ACTIVE" and o.approval_status == "APPROVED" and valid
    reasons = []
    if o.status != "ACTIVE":
        reasons.append(f"offer status is {o.status}")
    if o.approval_status != "APPROVED":
        reasons.append(f"supplier approval status is {o.approval_status}")
    if not valid:
        reasons.append("the contract is not valid at the as-of time")
    return {"ambiguous": False, "offer_uid": o.offer_uid, "supplier_id": o.supplier_id, "unit": o.unit, "unit_price": o.unit_price, "currency": o.currency, "price_quality": o.price_quality, "moq": o.min_order_quantity, "lot_size": o.lot_size, "max_allocation": o.max_allocation, "capacity_amount": o.capacity_amount, "lead_time_days": o.lead_time_days, "lead_time_std_days": o.lead_time_std_days, "lead_time_quality": o.lead_time_quality, "provenance": o.provenance.value, "eligible": eligible, "ineligible_reasons": reasons}  # fmt: skip


def _measured_lead(
    session: Session, req: ReplenishRequest, now: datetime, supplier_id: int | None, limits: Limits, dq: list
) -> dict[str, Any]:
    rows = load_deliveries(session, as_of=now, supplier_id=supplier_id, destination_id=req.facility_id, product_id=req.product_id, limit=limits.max_delivery_rows + 1)  # fmt: skip
    trunc = len(rows) > limits.max_delivery_rows
    rows = rows[: limits.max_delivery_rows]
    vals: list[float] = []
    for d in rows:
        st, _ = sem.classify(d, now, 0.0)
        if st != sem.COMPLETED or d.order_at is None:
            continue
        end = d.arrived_at or d.received_at
        if end is None or end <= d.order_at or end > now:
            continue
        vals.append((end - d.order_at).total_seconds() / 86400)
    n = len(vals)
    out: dict[str, Any] = {"n": n, "mean": None, "std": None, "samples": [], "truncated": trunc, "usable": n >= limits.min_lead_time_sample}  # fmt: skip
    if n:
        m = sum(vals) / n
        out["mean"] = safety.r6(m)
        out["std"] = safety.r6(math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))) if n >= 3 else None
        out["samples"] = [safety.r6(v) for v in vals[-500:]]
    if trunc:
        dq.append(
            issue(
                "DELIVERY_HISTORY_TRUNCATED",
                "WARNING",
                f"Delivery history was truncated at {limits.max_delivery_rows} records.",
            )
        )
    return out


def _lead(
    session: Session,
    req: ReplenishRequest,
    now: datetime,
    offer: dict | None,
    pol: dict | None,
    limits: Limits,
    dq: list,
) -> dict[str, Any]:
    sid = req.supplier_id or (offer.get("supplier_id") if offer and not offer.get("ambiguous") else None)
    meas = _measured_lead(session, req, now, sid, limits, dq)
    alts = []
    if meas["usable"]:
        alts.append({"source": "MEASURED", "days": meas["mean"], "std": meas["std"], "n": meas["n"]})
    elif meas["n"]:
        alts.append({"source": "MEASURED_INSUFFICIENT", "days": meas["mean"], "std": None, "n": meas["n"], "note": f"fewer than {limits.min_lead_time_sample} completed deliveries; not used"})  # fmt: skip
    if offer and not offer.get("ambiguous") and offer.get("lead_time_days") is not None:
        alts.append({"source": "SOURCING_OFFER", "days": offer["lead_time_days"], "std": offer.get("lead_time_std_days"), "quality": offer.get("lead_time_quality")})  # fmt: skip
    if pol and pol.get("lead_time_days") is not None:
        alts.append({"source": "POLICY", "days": pol["lead_time_days"], "std": None})
    chosen: dict[str, Any] = {"days": None, "std": None, "source": None, "provenance": None, "n": 0, "supplier_id": sid, "samples": [], "alternatives": alts}  # fmt: skip
    if req.lead_time_days is not None:
        chosen.update(
            days=req.lead_time_days, std=req.lead_time_std_days, source="USER_SUPPLIED", provenance="ESTIMATED"
        )
    elif meas["usable"]:
        chosen.update(
            days=meas["mean"],
            std=meas["std"],
            source="MEASURED",
            provenance="MEASURED",
            n=meas["n"],
            samples=meas["samples"],
        )
    elif offer and not offer.get("ambiguous") and offer.get("lead_time_days") is not None:
        chosen.update(days=offer["lead_time_days"], std=offer.get("lead_time_std_days"), source="SOURCING_OFFER", provenance="ESTIMATED" if offer.get("lead_time_quality") != "VERIFIED" else "VERIFIED")  # fmt: skip
    elif pol and pol.get("lead_time_days") is not None:
        chosen.update(days=pol["lead_time_days"], std=None, source="POLICY", provenance="ESTIMATED")
    if req.lead_time_days is not None and meas["usable"]:
        chosen["samples"] = meas["samples"]
        chosen["n"] = meas["n"]
    if chosen["days"] is None:
        dq.append(
            issue(
                "LEAD_TIME_UNKNOWN",
                "ERROR",
                "No lead time is available (no measured deliveries, offer, policy or analyst value); none is assumed.",
            )
        )
    elif chosen["source"] != "MEASURED":
        dq.append(issue("LEAD_TIME_NOT_MEASURED", "INFO", f"The lead time comes from {chosen['source']} ({chosen['provenance']}), not from measured deliveries."))  # fmt: skip
    if chosen["days"] is not None and chosen["std"] is None:
        dq.append(issue("LEAD_TIME_SPREAD_UNKNOWN", "WARNING", "Lead-time variability is unknown; it is excluded from safety stock, which may therefore be understated."))  # fmt: skip
    return chosen


def _policy(
    session: Session, req: ReplenishRequest, conf: invcfg.InventoryConfig, now: datetime, unit: str | None, dq: list
) -> dict[str, Any] | None:
    pol = inv.effective_policies(session, conf, now).get((req.product_id, req.facility_id))
    if pol is None:
        dq.append(
            issue(
                "POLICY_MISSING",
                "INFO",
                "No current inventory policy is registered; the comparison baseline is unavailable.",
            )
        )
        return None
    pin = calc.PolicyIn(pol.id, pol.version, pol.unit, pol.safety_stock, pol.reorder_point, pol.minimum, pol.maximum, pol.target, pol.lead_time_days, pol.review_period_days, pol.service_level)  # fmt: skip
    if unit and calc.norm_unit(pol.unit) != calc.norm_unit(unit):
        dq.append(
            issue(
                "POLICY_UNIT_MISMATCH",
                "ERROR",
                f"Policy unit '{pol.unit}' differs from the inventory unit '{unit}'; it is not used.",
            )
        )
        return None
    probs = calc.policy_problems(pin)
    if probs:
        dq.append(issue("POLICY_INVALID", "ERROR", " ".join(probs)))
        return None
    return {"id": pol.id, "version": pol.version, "unit": pol.unit, "safety_stock": pol.safety_stock, "reorder_point": pol.reorder_point, "minimum": pol.minimum, "maximum": pol.maximum, "target": pol.target, "lead_time_days": pol.lead_time_days, "review_period_days": pol.review_period_days, "service_level": pol.service_level}  # fmt: skip


def _costs(
    session: Session, req: ReplenishRequest, now: datetime, offer: dict | None, unit: str | None, dq: list
) -> dict[str, Any]:
    vals: dict[str, float | None] = {
        "holding_cost_per_unit_day": None,
        "ordering_cost": None,
        "shortage_cost_per_unit": None,
        "unit_cost": None,
    }
    prov: dict[str, str] = {}
    currency = None
    storage = None
    profile = None
    if req.use_stored_costs:
        profile = session.scalars(select(InventoryCostProfile).where(InventoryCostProfile.product_id == req.product_id, InventoryCostProfile.facility_id == req.facility_id, InventoryCostProfile.effective_from <= now, InventoryCostProfile.retired_at.is_(None)).order_by(InventoryCostProfile.effective_from.desc(), InventoryCostProfile.id.desc())).first()  # fmt: skip
        if profile is not None:
            if unit and profile.unit and calc.norm_unit(profile.unit) != calc.norm_unit(unit):
                dq.append(issue("COST_UNIT_MISMATCH", "ERROR", f"The stored cost profile is per '{profile.unit}' but the inventory is in '{unit}'; the profile is not used."))  # fmt: skip
                profile = None
            else:
                currency = profile.currency
                for k in vals:
                    v = getattr(profile, k)
                    if v is not None:
                        vals[k], prov[k] = v, f"STORED_PROFILE:{profile.profile_uid}"
                storage = profile.storage_capacity
    if req.costs is not None:
        c = req.costs
        if (
            currency
            and c.currency
            and c.currency != currency
            and any(getattr(c, k if k != "holding_cost_per_unit_day" else k) is not None for k in vals)
        ):
            dq.append(
                issue(
                    "COST_CURRENCY_CONFLICT",
                    "WARNING",
                    f"Request costs are in {c.currency} but the stored profile is in {currency}; request costs replace the profile's.",
                )
            )
            vals = {k: None for k in vals}
            prov = {}
        currency = c.currency or currency
        for k in vals:
            v = getattr(c, k)
            if v is not None:
                vals[k], prov[k] = v, "REQUEST"
    if (
        vals["unit_cost"] is None
        and offer
        and not offer.get("ambiguous")
        and offer.get("unit_price") is not None
        and (not currency or offer.get("currency") == currency)
    ):
        vals["unit_cost"], prov["unit_cost"] = (
            offer["unit_price"],
            f"SOURCING_OFFER:{offer['offer_uid']} ({offer.get('price_quality')})",
        )
        currency = currency or offer.get("currency")
    missing = [k for k, v in vals.items() if v is None]
    if missing:
        dq.append(
            issue(
                "COSTS_INCOMPLETE",
                "INFO",
                "Cost inputs not supplied (never treated as zero): " + ", ".join(missing) + ".",
            )
        )
    return {"values": vals, "provenance": prov, "currency": currency, "storage_capacity": storage, "missing": missing, "profile_uid": profile.profile_uid if profile else None}  # fmt: skip


def load(session: Session, req: ReplenishRequest, now: datetime, limits: Limits) -> dict[str, Any]:
    prod = session.get(Product, req.product_id)
    fac = session.get(Facility, req.facility_id)
    dq: list[dict[str, str]] = []
    conf = invcfg.InventoryConfig(product_ids=[req.product_id], facility_ids=[req.facility_id], demand_window_days=req.demand_window_days, demand_basis=req.demand_basis, allocated_overlap=req.allocated_overlap, max_inventory_age_days=req.max_inventory_age_days)  # fmt: skip
    pos = _quantities(session, req, conf, now, prod, dq)
    unit = pos["unit"] or prod.unit_of_measure
    receipts = _receipts(session, req, conf, now, unit)
    demand = _observed_demand(session, req, conf, now, unit, limits, dq)
    fc = _forecast(session, req, now, unit, dq)
    pol = _policy(session, req, conf, now, unit, dq)
    offer = _offers(session, req, now, dq)
    lead = _lead(session, req, now, offer, pol, limits, dq)
    costs = _costs(session, req, now, offer, unit, dq)
    if pos.get("in_transit_reported") and not receipts["counted"]:
        dq.append(issue("IN_TRANSIT_UNDATED", "WARNING", f"The inventory record reports {pos['in_transit_reported']:g} in transit but no dated receipt exists, so it is not counted as an inbound pipeline."))  # fmt: skip
    elif pos.get("in_transit_reported") is not None and receipts["counted"]:
        dq.append(issue("IN_TRANSIT_NOT_ADDED", "INFO", "The in-transit figure on the inventory record is not added to the pipeline; dated receipts are used, so nothing is counted twice."))  # fmt: skip
    for r in receipts["excluded"]:
        if r["reason"] == "OVERDUE_OR_DUE_NOW":
            dq.append(
                issue(
                    "RECEIPT_OVERDUE",
                    "WARNING",
                    f"Receipt {r['id']} ({r['quantity']:g}) is overdue; its arrival is unproven so it is not counted.",
                )
            )
    snap: dict[str, Any] = {
        "as_of": now.isoformat(), "as_of_date": now.date().isoformat(), "unit": unit,
        "product": {"id": prod.id, "code": prod.product_code, "name": prod.name}, "facility": {"id": fac.id, "code": fac.facility_code, "name": fac.name},
        "position": pos, "receipts": receipts, "demand": {"observed": demand, "forecast": fc}, "lead_time": lead, "policy": pol, "offer": offer, "costs": costs,
        "data_quality": dq,
    }  # fmt: skip
    snap["hash"] = stable_hash({k: v for k, v in snap.items() if k != "data_quality"})
    return snap
