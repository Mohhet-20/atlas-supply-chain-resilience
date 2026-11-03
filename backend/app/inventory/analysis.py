"""Stockout analysis engine (``inv-1``). Reads inventory, demand, replenishments, policies and (optionally) a finished
impact analysis; writes nothing but its own result rows. Pure decisions live in ``calc``."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.inventory import calc
from app.inventory import config as cfg
from app.models.facility import Facility
from app.models.impact import ImpactAnalysis, ImpactFinding
from app.models.inventory import DemandObservation, InventoryPolicy, InventoryPosition, InventoryReplenishment
from app.models.product import Product

Pair = tuple[int, int]


@dataclass(frozen=True)
class InvLimits:
    max_pairs: int = 1000
    time_budget_seconds: float = 30.0


@dataclass
class AnalysisResult:
    findings: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[dict[str, str]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    input_hash: str = ""
    truncation_reasons: list[str] = field(default_factory=list)


def _utc(d: datetime) -> datetime:
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def issue(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


def load_impact(
    session: Session, impact_id: int | None
) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, float | None], ImpactAnalysis | None]:
    if impact_id is None:
        return {}, {}, None
    ia = session.get(ImpactAnalysis, impact_id)
    if ia is None:
        return {}, {}, None
    dur = {e["event_uid"]: e.get("duration_days") for e in (ia.events or [])}
    out = {}
    for f in session.scalars(select(ImpactFinding).where(ImpactFinding.analysis_id == ia.id)):
        out[(f.entity_type.value, f.entity_id)] = {
            "loss_pct": f.loss_pct, "loss_pct_lower_bound": f.loss_pct_lower_bound, "added_lead_time_days": f.added_lead_time_days,
            "impact_state": f.impact_state, "classification": f.classification, "durations": [dur.get(u) for u in (f.event_uids or [])], "event_uids": f.event_uids or [],
            "finding_id": f.id, "name": f.entity_name,
        }  # fmt: skip
    return out, dur, ia


def _pairs_filter(conf: cfg.InventoryConfig, model: Any) -> list[Any]:
    cond = []
    if conf.product_ids:
        cond.append(model.product_id.in_(conf.product_ids))
    if conf.facility_ids:
        cond.append(model.facility_id.in_(conf.facility_ids))
    return cond


def latest_positions(session: Session, conf: cfg.InventoryConfig, as_of: datetime) -> dict[Pair, InventoryPosition]:
    sub = (
        select(InventoryPosition.product_id, InventoryPosition.facility_id, func.max(InventoryPosition.observed_at).label("t"))
        .where(InventoryPosition.observed_at <= as_of, *_pairs_filter(conf, InventoryPosition))
        .group_by(InventoryPosition.product_id, InventoryPosition.facility_id)
        .subquery()
    )  # fmt: skip
    q = select(InventoryPosition).join(
        sub,
        and_(
            InventoryPosition.product_id == sub.c.product_id,
            InventoryPosition.facility_id == sub.c.facility_id,
            InventoryPosition.observed_at == sub.c.t,
        ),
    )
    return {(p.product_id, p.facility_id): p for p in session.scalars(q)}


def effective_policies(session: Session, conf: cfg.InventoryConfig, as_of: datetime) -> dict[Pair, InventoryPolicy]:
    q = select(InventoryPolicy).where(
        InventoryPolicy.effective_from <= as_of, or_(InventoryPolicy.effective_to.is_(None), InventoryPolicy.effective_to > as_of), *_pairs_filter(conf, InventoryPolicy)
    )  # fmt: skip
    best: dict[Pair, InventoryPolicy] = {}
    for p in session.scalars(q):
        k = (p.product_id, p.facility_id)
        if k not in best or p.version > best[k].version:
            best[k] = p
    return best


def window_demand(session: Session, conf: cfg.InventoryConfig, as_of_date: date) -> dict[Pair, list[DemandObservation]]:
    w0 = as_of_date - timedelta(days=conf.demand_window_days)
    q = select(DemandObservation).where(
        DemandObservation.period_end > w0,
        DemandObservation.period_start < as_of_date,
        *_pairs_filter(conf, DemandObservation),
    )
    out: dict[Pair, list[DemandObservation]] = defaultdict(list)
    for d in session.scalars(q):
        out[(d.product_id, d.facility_id)].append(d)
    return out


def current_receipts(
    session: Session, conf: cfg.InventoryConfig, as_of: datetime
) -> dict[Pair, list[InventoryReplenishment]]:
    """Latest version of each order reference as known at the analysis time."""
    q = select(InventoryReplenishment).where(InventoryReplenishment.observed_at <= as_of, *_pairs_filter(conf, InventoryReplenishment)).order_by(InventoryReplenishment.observed_at, InventoryReplenishment.id)  # fmt: skip
    latest: dict[tuple[int, int, str], InventoryReplenishment] = {}
    for r in session.scalars(q):
        latest[(r.product_id, r.facility_id, r.reference)] = r
    out: dict[Pair, list[InventoryReplenishment]] = defaultdict(list)
    for (pid, fid, _ref), r in latest.items():
        out[(pid, fid)].append(r)
    return out


def input_hash(parts: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def run_analysis(
    session: Session, a: Any, conf: cfg.InventoryConfig, lim: InvLimits, *, scenario_impact_id: int | None,
    checkpoint: Callable[[], None] | None = None,
) -> AnalysisResult:  # fmt: skip
    t0 = time.monotonic()
    as_of = _utc(a.as_of)
    as_of_date = as_of.date()
    res = AnalysisResult()
    pos = latest_positions(session, conf, as_of)
    pols = effective_policies(session, conf, as_of)
    dem = window_demand(session, conf, as_of_date)
    rec = current_receipts(session, conf, as_of)
    impact, _dur, ia = load_impact(session, scenario_impact_id)
    fc = None
    if conf.forecast_run_id is not None:
        from app.forecast import integration as fint

        fc = fint.load_context(session, conf.forecast_run_id)
    ltx = None
    if conf.delivery_assessment_ids:
        from app.leadtime import integration as ltint

        ltx = ltint.load_context(session, conf.delivery_assessment_ids)
    pairs = sorted(set(pos) | set(pols) | set(dem) | set(rec))
    if len(pairs) > lim.max_pairs:
        res.truncation_reasons.append("max_pairs")
        res.warnings.append(
            {
                "code": "MAX_PAIRS",
                "text": f"{len(pairs)} product-facility pairs found; only the first {lim.max_pairs} (by id) were analysed. This result is incomplete.",
            }
        )
        pairs = pairs[: lim.max_pairs]
    pids, fids = {p for p, _ in pairs}, {f for _, f in pairs}
    prods = {p.id: p for p in session.scalars(select(Product).where(Product.id.in_(pids)))} if pids else {}
    facs = {f.id: f for f in session.scalars(select(Facility).where(Facility.id.in_(fids)))} if fids else {}
    hash_parts = {
        "positions": sorted(p.id for p in pos.values() if (p.product_id, p.facility_id) in set(pairs)),
        "demand": sorted(d.id for k, v in dem.items() if k in set(pairs) for d in v), "receipts": sorted(r.id for k, v in rec.items() if k in set(pairs) for r in v),
        "policies": sorted((p.id, p.version) for p in pols.values()), "config": cfg.config_hash(conf), "algorithm": cfg.ALGORITHM_VERSION,
        "as_of": as_of.isoformat(), "impact": [ia.id, ia.graph_hash, ia.config_hash] if ia else None,
        **({"forecast": [fc["run"].id, fc["run"].data_snapshot_hash, fc["run"].config_hash]} if fc else {}),
    }  # fmt: skip
    res.input_hash = input_hash(hash_parts)
    res.inputs = {
        "pairs": len(pairs), "positions": len(hash_parts["positions"]), "demand_observations": len(hash_parts["demand"]), "receipts": len(hash_parts["receipts"]),
        "policies": len(hash_parts["policies"]), "impact_analysis_id": ia.id if ia else None, "impact_graph_hash": ia.graph_hash if ia else None,
        "position_ids_sample": hash_parts["positions"][:20], "as_of": as_of.isoformat(),
        "forecast_run_id": fc["run"].id if fc else None,
    }  # fmt: skip
    gaps: Counter[str] = Counter()
    for n, key in enumerate(pairs):
        if time.monotonic() - t0 > lim.time_budget_seconds:
            res.truncation_reasons.append("time_budget")
            res.warnings.append(
                {
                    "code": "TIME_BUDGET",
                    "text": "The time budget was reached; remaining pairs were not analysed. This result is incomplete.",
                }
            )
            break
        if checkpoint and n % 50 == 0:
            checkpoint()
        try:
            res.findings.append(
                _pair(
                    key,
                    pos.get(key),
                    pols.get(key),
                    dem.get(key, []),
                    rec.get(key, []),
                    prods.get(key[0]),
                    facs.get(key[1]),
                    conf,
                    as_of,
                    as_of_date,
                    impact,
                    ia,
                    scenario_impact_id is not None,
                    gaps,
                    fc,
                    ltx,
                )
            )
        except Exception as exc:  # one bad pair must not sink the analysis; recorded, never hidden
            res.failures.append({"product_id": key[0], "facility_id": key[1], "error": type(exc).__name__})
    if ia is not None:
        for fnd in res.findings:
            if isinstance(fnd.get("scenario"), dict):
                fnd["scenario"]["impact_analysis_id"] = ia.id
    res.summary, res.coverage = _summarize(res.findings, gaps, scenario_impact_id is not None, ia)
    if ia is not None and ia.truncated:
        res.warnings.append(
            {
                "code": "IMPACT_PARTIAL",
                "text": "The impact analysis used is PARTIAL, so some exposed entities may be missing from this scenario analysis.",
            }
        )
    return res


def fint_obs_label() -> str:
    return "Observed average rate over the demand window — a historical average, not a forecast"


def _pair(
    key, p, pol, dem_rows, rec_rows, prod, fac, conf, as_of, as_of_date, impact, ia, scenario, gaps, fc=None, ltx=None
) -> dict[str, Any]:  # noqa: ANN001, C901
    pid, fid = key
    unit = (p.unit if p else None) or (prod.unit_of_measure if prod else None)
    dq: list[dict[str, str]] = []
    assumptions: list[str] = []
    evidence: dict[str, Any] = {"position_id": p.id if p else None, "demand_ids": [], "policy_id": pol.id if pol else None, "policy_version": pol.version if pol else None, "receipts_counted": [], "receipts_excluded": []}  # fmt: skip
    # --- inventory
    if p is None:
        qty = {"status": "MISSING_INVENTORY", "unit": unit, "usable": None, "available": None, "stale": False, "source": {}, "formulas": {}, "assumptions": [], "warnings": ["No inventory observation exists at or before the analysis time."], "unreported": []}  # fmt: skip
    else:
        qty = calc.derive_quantities(
            calc.Position(p.id, pid, fid, p.unit, p.observed_at, p.on_hand, p.reserved, p.allocated, p.quarantined, p.damaged, p.usable_source, p.atp_source, p.in_transit, p.backordered),
            product_unit=prod.unit_of_measure if prod else None, as_of=as_of, max_age_days=conf.max_inventory_age_days, allocated_overlap=conf.allocated_overlap,
        )  # fmt: skip
    qty["basis"] = conf.policy_basis
    qty["basis_value"] = qty.get("available") if conf.policy_basis == "AVAILABLE" else qty.get("usable")
    qty["calculated_at"] = as_of.isoformat()
    assumptions += qty.get("assumptions", [])
    if qty["status"] != "OK":
        dq.append(issue(f"INVENTORY_{qty['status']}", "ERROR", "; ".join(qty["warnings"])))
        gaps["usable inventory observation"] += 1
    elif qty["stale"]:
        dq.append(issue("INVENTORY_STALE", "WARNING", qty["warnings"][0]))
        gaps["fresh inventory observation"] += 1
    if qty.get("over_committed"):
        dq.append(issue("OVER_COMMITTED", "WARNING", "Allocations exceed usable inventory."))
    if qty.get("unreported"):
        dq.append(issue("COMPONENTS_UNREPORTED", "INFO", f"Not reported: {', '.join(qty['unreported'])}."))
    # --- demand
    obs = [calc.DemandObs(d.id, d.period_start, d.period_end, d.quantity, d.unit, d.kind.value) for d in dem_rows]
    dm = calc.demand_rate(obs, unit=unit, as_of_date=as_of_date, window_days=conf.demand_window_days, basis=conf.demand_basis, min_obs=conf.min_demand_observations, min_coverage=conf.min_demand_coverage)  # fmt: skip
    evidence["demand_ids"] = dm["observation_ids"]
    if dm["status"] != "calculable":
        dq.append(
            issue(
                f"DEMAND_{dm['status'].upper()}",
                "WARNING" if dm["status"] == "zero_observed_demand" else "ERROR",
                "; ".join(dm["warnings"]) or dm["status"],
            )
        )
        if dm["status"] != "zero_observed_demand":
            gaps["demand history"] += 1
    elif dm["intermittent"]:
        dq.append(issue("DEMAND_INTERMITTENT", "WARNING", "Intermittent demand."))
    if dm["status"] == "calculable" and dm["coverage_ratio"] < 0.75:
        dq.append(
            issue(
                "DEMAND_PARTIAL_COVERAGE",
                "INFO",
                f"Demand observed on {dm['coverage_ratio']:.0%} of the window; unobserved days are not read as zero.",
            )
        )
    doi = calc.days_of_inventory(qty, dm, basis="usable" if conf.policy_basis == "USABLE" else "available")
    # --- policy
    policy = None
    if pol is not None:
        policy = calc.PolicyIn(pol.id, pol.version, pol.unit, pol.safety_stock, pol.reorder_point, pol.minimum, pol.maximum, pol.target, pol.lead_time_days, pol.review_period_days, pol.service_level)  # fmt: skip
        if unit and calc.norm_unit(pol.unit) != calc.norm_unit(unit):
            dq.append(
                issue(
                    "POLICY_UNIT_MISMATCH",
                    "ERROR",
                    f"Policy unit '{pol.unit}' differs from the inventory unit '{unit}'; the policy is not applied.",
                )
            )
            policy = None
        elif calc.policy_problems(policy):
            dq.append(issue("POLICY_INVALID", "ERROR", " ".join(calc.policy_problems(policy))))
            policy = None
    if policy is None:
        gaps["inventory policy"] += 1
        if pol is None:
            dq.append(
                issue("MISSING_POLICY", "WARNING", "No inventory policy applies to this product at this facility.")
            )
    elif policy.lead_time_days is None:
        dq.append(
            issue(
                "MISSING_LEAD_TIME",
                "INFO",
                "The policy has no lead time; the reorder point cannot be computed and none is assumed.",
            )
        )
        gaps["policy lead time"] += 1
    rate = dm["rate_per_day"] if dm["status"] == "calculable" else None
    pe = calc.evaluate_policy(policy, qty["basis_value"] if qty["status"] == "OK" and not qty["stale"] else None, rate)
    if policy is not None and policy.safety_stock is None:
        dq.append(
            issue(
                "SAFETY_STOCK_NOT_CONFIGURED", "INFO", "Safety stock is not configured; it is not assumed to be zero."
            )
        )
    est = calc.estimate_safety_stock(
        rate,
        dm.get("daily_rate_stddev"),
        policy.lead_time_days if policy else None,
        policy.service_level if policy else None,
    )
    # --- replenishment and projection
    receipts = [
        calc.Receipt(r.id, r.quantity, r.unit, r.expected_date, r.status.value, r.supplier_id, r.reference)
        for r in rec_rows
    ]
    use, skip = calc.classify_receipts(
        receipts, as_of_date=as_of_date, unit=unit, eligible=conf.eligible_statuses, horizon_days=conf.horizon_days
    )
    evidence["receipts_counted"], evidence["receipts_excluded"] = [u["id"] for u in use], [s["id"] for s in skip]
    if not rec_rows:
        dq.append(
            issue(
                "NO_REPLENISHMENT_DATA",
                "INFO",
                "No replenishment records exist; the projection assumes no inbound supply.",
            )
        )
        assumptions.append("No replenishment data: the projection counts no inbound supply.")
        gaps["replenishment data"] += 1
    elif skip:
        why = Counter(s["reason"] for s in skip)
        dq.append(issue("RECEIPTS_EXCLUDED", "INFO", "Not counted: " + ", ".join(f"{k} x{v}" for k, v in why.items())))
    # --- demand source: a chosen forecast run or, by default / on fallback, the observed average rate
    dsrc: dict[str, Any] = {
        "source": "observed_rate",
        "label": fint_obs_label(),
        "window_days": conf.demand_window_days,
    }
    fser = None
    if fc is not None:
        from app.forecast import integration as fint

        if key == fc["pair"]:
            fres = fint.demand_series(fc, unit=unit, as_of_date=as_of_date, horizon_days=conf.horizon_days, factor=conf.forecast_conversion_factor)  # fmt: skip
            if fres["status"] == "usable":
                fser = fres
                dsrc = fint.source_block(fc["run"], covered_days=fres["covered_days"], conversion=fres["conversion"], warnings=fres["warnings"])  # fmt: skip
            else:
                dq.append(issue("FORECAST_UNUSABLE", "WARNING", fres["reason"]))
                dsrc["fallback_from_forecast"] = {
                    "forecast_run_id": fc["run"].id,
                    "status": fres["status"],
                    "reason": fres["reason"],
                }
                assumptions.append(f"The selected forecast could not be used ({fres['reason']}) so the observed average rate is used instead; it is a historical average, not a forecast.")  # fmt: skip
        else:
            dsrc["note"] = (
                "A forecast run was selected but belongs to a different product-facility pair; this pair uses the observed rate."
            )
        evidence["forecast_run_id"] = fc["run"].id
    can_project = (
        qty["status"] == "OK"
        and not qty["stale"]
        and (dm["status"] == "calculable" or fser is not None)
        and qty["basis_value"] is not None
    )
    thr = {"safety_stock": policy.safety_stock if policy else None, "reorder_point": pe.get("reorder_point_used")}
    proj = None
    if can_project and fser is not None:
        proj = calc.project_series(qty["basis_value"], fser["series"], use, thr, formula="closing(d) = max(0, closing(d-1) + receipts(d) - forecast_demand(d))")  # fmt: skip
        proj.update(opening=qty["basis_value"], rate_per_day=round(fser["mean_daily"], 6), demand_series=fser["series"], receipts_counted_detail=use, receipts_excluded=skip, horizon_days=len(fser["series"]), requested_horizon_days=conf.horizon_days, as_of=as_of.isoformat(), demand_basis="FORECAST", thresholds=thr, demand_source=dsrc, observed_rate_per_day=rate)  # fmt: skip
        assumptions.append(f"Demand follows forecast run {dsrc['run_uid']} ({dsrc['model']}, origin {dsrc['origin']}): a statistical estimate, not an observation. Mean {fser['mean_daily']:g} {unit}/day over {len(fser['series'])} day(s).")  # fmt: skip
        assumptions += [w for w in fser["warnings"]]
        assumptions.append("Only CONFIRMED/IN_TRANSIT" + (" and PLANNED" if conf.include_planned_receipts else "") + " receipts with a future expected date are counted; each arrives in full on its date.")  # fmt: skip
    elif can_project:
        proj = calc.project(qty["basis_value"], rate, use, conf.horizon_days, thr)
        proj.update(opening=qty["basis_value"], rate_per_day=rate, receipts_counted_detail=use, receipts_excluded=skip, horizon_days=conf.horizon_days, as_of=as_of.isoformat(), demand_basis=dm["basis"], thresholds=thr, demand_source=dsrc)  # fmt: skip
        assumptions.append(
            f"Constant demand of {rate:g} {unit}/day (average of {dm['usable_observations']} observation(s) covering {dm['covered_days']} of {dm['window']['days']} days)."
        )
        assumptions.append(
            "Only CONFIRMED/IN_TRANSIT"
            + (" and PLANNED" if conf.include_planned_receipts else "")
            + " receipts with a future expected date are counted; each arrives in full on its date."
        )
    if proj is not None and (conf.receipt_delay_days is not None or ltx is not None):
        from app.leadtime import integration as ltint

        dser = proj.get("demand_series") or [proj["rate_per_day"]] * proj["horizon_days"]
        proj["leadtime_effect"] = ltint.effect(
            proj=proj, use=use, demand=dser, opening=qty["basis_value"], thresholds=thr, as_of_date=as_of_date, pid=pid,
            fid=fid, delay_days=conf.receipt_delay_days, basis=conf.delivery_assessment_basis, ctx=ltx,
        )  # fmt: skip
    cl = calc.classify(
        qty, dm, doi, pe, proj, low_coverage_days=conf.low_coverage_days, backordered=p.backordered if p else None
    )
    # --- scenario
    scen, change = None, None
    if scenario:
        scen, change = _scenario(key, use, proj, qty, dm, doi, pe, cl, impact, conf, thr, fid, p, assumptions, dq)
    # --- confidence and coverage
    needs = {"inventory": qty["status"] == "OK" and not qty["stale"], "demand": dm["status"] == "calculable" or fser is not None, "policy": policy is not None, "lead_time": bool(policy and policy.lead_time_days is not None), "replenishment": bool(use)}  # fmt: skip
    coverage = sum(needs.values()) / len(needs)
    reasons = [f"missing: {k}" for k, v in needs.items() if not v]
    sev = {i["severity"] for i in dq}
    if (
        cl["status"] in ("insufficient_data", "unknown")
        or "ERROR" in sev
        or (fser is None and (dm.get("coverage_ratio", 0) < 0.5 or dm.get("intermittent")))
        or (fser is not None and not dsrc.get("validated"))
    ):
        conf_label = "LOW"
    elif coverage >= 0.8 and "WARNING" not in sev:
        conf_label = "HIGH"
    else:
        conf_label = "MEDIUM"
    explanation = _explain(cl, qty, dm, doi, pe, proj, change, unit)
    return {
        "product_id": pid, "facility_id": fid, "product_code": prod.product_code if prod else str(pid), "product_name": prod.name if prod else "?",
        "facility_code": fac.facility_code if fac else str(fid), "facility_name": fac.name if fac else "?", "unit": unit, "status": cl["status"],
        "severity_rank": cl["severity_rank"], "reason": cl["reason"][:500], "quantities": qty, "demand": dm, "coverage_days": doi,
        "policy": {**pe, "policy_id": pol.id if pol else None, "version": pol.version if pol else None, "estimated_safety_stock": est}, "projection": proj, "scenario": scen,
        "change": change, "confidence": conf_label, "confidence_basis": reasons or ["all five inputs present"], "coverage": round(coverage, 4),
        "coverage_detail": needs, "data_quality": dq, "assumptions": list(dict.fromkeys(assumptions)), "evidence": evidence, "explanation": explanation,
    }  # fmt: skip


def _scenario(key, use, proj, qty, dm, doi, pe, cl, impact, conf, thr, fid, p, assumptions, dq):  # noqa: ANN001
    """Apply scenario and impact results to the receipts that feed this facility. Qualitative exposure becomes bounds, never a number."""
    exposed_fac = impact.get(("FACILITY", fid))
    effs = []
    for r in use:
        f = impact.get(("SUPPLIER", r["supplier_id"])) if r.get("supplier_id") else None
        f = f or exposed_fac
        effs.append(
            calc.scenario_effect(f, f["durations"] if f else [], conf.horizon_days, conf.scenario_start_offset_days)
            if f
            else {"affected": False}
        )
    out: dict[str, Any] = {"impact_analysis_id": None, "facility_exposed": exposed_fac is not None, "effects": [{"receipt_id": r["id"], **e} for r, e in zip(use, effs, strict=True)]}  # fmt: skip
    if proj is None:
        out["outcome"] = "NOT_PROJECTABLE"
        out["note"] = (
            "The baseline cannot be projected from the available data, so the scenario effect cannot be measured."
        )
        dq.append(issue("SCENARIO_NOT_PROJECTABLE", "WARNING", out["note"]))
        return out, "UNKNOWN_INSUFFICIENT_DATA"
    affected = [e for e in effs if e.get("affected")]
    if not use and exposed_fac is None:
        out["outcome"] = "NOT_EXPOSED"
        return out, "UNAFFECTED"
    if not use:
        out["outcome"] = "EXPOSED_NO_MODELLED_SUPPLY"
        out["note"] = (
            "The facility is exposed in the scenario but no eligible replenishment is recorded, so no inventory effect can be quantified."
        )
        dq.append(issue("SCENARIO_UNSUPPORTED", "WARNING", out["note"]))
        assumptions.append("Scenario exposure is not translated into an inventory loss without replenishment records.")
        return out, "UNKNOWN_EFFECT"
    if not affected:
        out["outcome"] = "NOT_EXPOSED"
        return out, "UNAFFECTED"

    def traj(unq_mode: str):  # noqa: ANN202
        rs = []
        for r, e in zip(use, effs, strict=True):
            if not e.get("affected"):
                rs.append(dict(r))
                continue
            mode = "point" if e["quantified"] else unq_mode
            rs.extend(calc.apply_effect([r], e, mode))
        if proj.get("demand_series") is not None:
            return calc.project_series(qty["basis_value"], proj["demand_series"], rs, thr, formula=proj["formula"])
        return calc.project(qty["basis_value"], proj["rate_per_day"], rs, conf.horizon_days, thr)

    quantified = all(e["quantified"] for e in affected)

    def cls_for(pj):  # noqa: ANN001, ANN202
        c2 = calc.classify(
            qty, dm, doi, pe, pj, low_coverage_days=conf.low_coverage_days, backordered=p.backordered if p else None
        )
        return c2

    if quantified:
        sp = traj("point")
        sc = cls_for(sp)
        out.update(outcome="QUANTIFIED", series=sp["series"], min_closing=sp["min_closing"], stockout_within_day=sp["stockout_within_day"], crossings=sp["crossings"], unfilled_demand=sp["unfilled_demand"], status=sc["status"])  # fmt: skip
        change = _change(cl, proj, sc, sp)
        assumptions.append(
            "Scenario: the facility-level or supplier-level loss percentage from the impact analysis is applied to each affected receipt falling inside the disruption window; products are not distinguished."
        )
    else:
        worst, best = traj("worst"), traj("best")
        sw = cls_for(worst)
        out.update(outcome="CONDITIONAL_BOUNDS", series_worst=worst["series"], series_best=best["series"], min_closing_worst=worst["min_closing"], stockout_within_day_worst=worst["stockout_within_day"], status_worst=sw["status"], status_best=cls_for(best)["status"], unfilled_demand_worst=worst["unfilled_demand"])  # fmt: skip
        out["note"] = (
            "The impact analysis gives no quantity for some affected receipts. The range runs from no loss to total loss of those receipts inside the window. It is a bound, not a prediction."
        )
        change = "CONDITIONAL_" + _change(cl, proj, sw, worst)
        assumptions.append(
            "Scenario exposure without a quantity is shown as bounds: best case = no loss, worst case = all affected receipts in the window are lost."
        )
        dq.append(issue("SCENARIO_UNQUANTIFIED", "INFO", out["note"]))
    if any(not e.get("duration_known", True) for e in affected):
        assumptions.append(
            "A disruption duration is not stated, so the disruption is assumed to last the whole horizon."
        )
    return out, change


def _change(
    base_cls: dict[str, Any], base_proj: dict[str, Any], sc_cls: dict[str, Any], sc_proj: dict[str, Any]
) -> str:
    bad = ("projected_stockout", "current_shortage")
    b_day, s_day = base_proj.get("stockout_within_day"), sc_proj.get("stockout_within_day")
    if sc_cls["status"] in bad and base_cls["status"] not in bad:
        return "NEW_POTENTIAL_SHORTAGE"
    if s_day is not None and b_day is not None and s_day < b_day:
        return "EARLIER_CROSSING"
    sb, ss = base_proj["crossings"].get("safety_stock"), sc_proj["crossings"].get("safety_stock")
    if (ss is not None and sb is None) or (ss is not None and sb is not None and ss < sb):
        return "EARLIER_CROSSING"
    if sc_proj["min_closing"] < base_proj["min_closing"] - 1e-6 or sc_cls["severity_rank"] > base_cls["severity_rank"]:
        return "REDUCED_COVERAGE"
    return "UNCHANGED"


def _explain(cl, qty, dm, doi, pe, proj, change, unit) -> str:  # noqa: ANN001
    bits = [f"Status {cl['status']}: {cl['reason']}"]
    if qty.get("basis_value") is not None:
        bits.append(
            f"Inventory basis ({qty['basis']}) {qty['basis_value']:g} {unit or ''} observed {qty.get('observed_at', '?')[:16]}."
        )
    if proj is not None and (proj.get("demand_source") or {}).get("source") == "forecast":
        bits.append(
            f"Demand taken from forecast run {proj['demand_source']['run_uid']} (a model estimate, not an observation)."
        )
    if dm.get("rate_per_day") is not None:
        bits.append(f"Observed demand {dm['rate_per_day']:g}/day ({dm['basis']}, {dm['usable_observations']} obs).")
    if doi.get("days") is not None:
        bits.append(f"Days of inventory {doi['days']:g}.")
    if proj and proj.get("stockout_within_day") is not None:
        bits.append(
            f"Under the stated assumptions inventory reaches zero in day {proj['stockout_within_day']} (daily resolution)."
        )
    if change:
        bits.append(f"Scenario comparison: {change}.")
    bits.append("Conditional on the listed assumptions; not a forecast.")
    return " ".join(bits)


def _summarize(
    findings: list[dict[str, Any]], gaps: Counter[str], scenario: bool, ia: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    by = Counter(f["status"] for f in findings)
    n = len(findings)
    summary = {
        "pairs_analysed": n, "by_status": dict(by), "current_shortages": by.get("current_shortage", 0), "projected_stockouts": by.get("projected_stockout", 0),
        "low_coverage": by.get("low_coverage", 0), "within_policy": by.get("within_policy", 0), "unknown": by.get("unknown", 0), "insufficient_data": by.get("insufficient_data", 0),
        "stale_inventory": sum(1 for f in findings if f["quantities"].get("stale")), "missing_policy": sum(1 for f in findings if not f["policy"].get("has_policy")),
        "below_safety_stock": sum(1 for f in findings if "BELOW_SAFETY_STOCK" in f["policy"].get("violations", [])),
        "at_or_below_reorder_point": sum(1 for f in findings if "AT_OR_BELOW_REORDER_POINT" in f["policy"].get("violations", [])),
        "kind": "SCENARIO" if scenario else "BASELINE", "classification_note": cfg.CLASSIFICATION_NOTE,
        "missing_data_ranking": [{"input": k, "pairs": v} for k, v in gaps.most_common() if v],
        "note": "Unknown and insufficient-data pairs are counted separately from confirmed shortages and are never added to them.",
    }  # fmt: skip
    if scenario:
        c = Counter(f["change"] for f in findings if f["change"])
        summary["by_change"] = dict(c)
        summary["impact_analysis_id"] = ia.id if ia else None
    cov = [f["coverage"] for f in findings]
    coverage = {"mean_input_coverage": round(sum(cov) / n, 4) if n else None, "confidence": dict(Counter(f["confidence"] for f in findings)), "note": "Coverage = share of the five inputs a status needs that were present (fresh inventory, demand, policy, lead time, eligible replenishment). It is separate from confidence and data quality."}  # fmt: skip
    return summary, coverage
