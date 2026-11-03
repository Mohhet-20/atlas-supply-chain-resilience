"""Read-only adapters that turn finished runs of earlier modules into plans, conditions and metric cells.

Nothing is computed that the source run did not compute. A value the run does not state is simply absent (never zero). The adapters only read; the
tables of the source phases are never written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.decision.config import EvidenceRef, Limits
from app.models.enums import SimStatus
from app.models.resilience import ResilienceRun
from app.models.scenario import SimulationImpact, SimulationRun
from app.models.sourcing import SourcingRun

NORMAL = "NORMAL"
USABLE_RESILIENCE = ("completed", "completed_with_warnings")


@dataclass
class Evidence:
    ref: dict[str, Any]
    source: dict[str, Any]
    plans: list[dict[str, Any]] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    cells: dict[str, dict[str, dict[str, dict[str, Any]]]] = field(default_factory=dict)
    exposure: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


class EvidenceProblem(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


def cond_key(name: str, mode: str) -> str:
    return NORMAL if name == NORMAL else f"{name} [{mode}]"


def _m(value: Any, unit: str, **q: Any) -> dict[str, Any] | None:
    if value is None or isinstance(value, bool):
        return None
    return {"value": float(value), "unit": unit, **{k: v for k, v in q.items() if v is not None}}


def _plan_metrics(alt: dict[str, Any], currency: str | None) -> dict[str, dict[str, Any]]:
    v, cost = alt.get("values") or {}, alt.get("cost") or {}
    cells = {
        "MODELLED_COST": _m(
            cost.get("total_known"), "currency", currency=cost.get("currency") or currency, basis=cost.get("basis")
        ),
        "UNMET_UNITS": _m(v.get("UNMET_DEMAND"), "units"),
        "FULFILMENT": _m(alt.get("baseline_fulfilment"), "fraction"),
        "AVG_LEAD_TIME_DAYS": _m(v.get("DELIVERY_DELAY"), "days"),
        "CONCENTRATION_HHI": _m(v.get("CONCENTRATION"), "index 0-1"),
        "OUTAGE_EXPOSURE_UNITS": _m(v.get("DISRUPTION_EXPOSURE"), "units"),
        "WORST_CASE_FULFILMENT": _m(v.get("WORST_CASE_SERVICE"), "fraction"),
    }
    return {k: x for k, x in cells.items() if x is not None}


def _resilience(db: Session, ref: EvidenceRef, limits: Limits) -> Evidence:
    run = db.scalar(select(ResilienceRun).where(ResilienceRun.run_uid == ref.run_uid))
    if run is None:
        raise EvidenceProblem("RUN_NOT_FOUND", f"Resilience run {ref.run_uid} does not exist.")
    if run.status is not SimStatus.COMPLETED or run.result_status not in USABLE_RESILIENCE:
        raise EvidenceProblem(
            "RUN_NOT_USABLE",
            f"Resilience run {ref.run_uid} is {run.status.value}/{run.result_status}; only completed runs with a plan can be evidence.",
        )
    req = run.request or {}
    plan = ref.plan or (run.recommendation or {}).get("id")
    if plan is None:
        raise EvidenceProblem(
            "PLAN_REQUIRED", f"Resilience run {ref.run_uid} declared no single winner; name the alternative in `plan`."
        )
    comp = run.comparison or {}
    if plan == "REFERENCE":
        alt, kind = comp.get("reference_plan"), comp.get("reference")
        if alt is None:
            raise EvidenceProblem("PLAN_NOT_FOUND", f"Resilience run {ref.run_uid} has no baseline reference plan.")
    else:
        alt, kind = next((a for a in (run.alternatives or []) if a["id"] == plan), None), None
        if alt is None:
            raise EvidenceProblem("PLAN_NOT_FOUND", f"Resilience run {ref.run_uid} has no alternative '{plan}'.")
    cur = req.get("currency")
    scope = {
        "product_id": run.product_id,
        "destination_facility_id": run.destination_facility_id,
        "quantity": req.get("quantity"),
        "unit": req.get("unit"),
        "currency": cur,
    }
    key = f"{run.run_uid}:{plan}"
    ev = Evidence(ref=ref.model_dump(mode="json"), source={"kind": "RESILIENCE_RUN", "run_uid": run.run_uid, "algorithm_version": run.algorithm_version, "result_status": run.result_status,
                  "hash": run.recommendation_hash or run.snapshot_hash, "snapshot_hash": run.snapshot_hash, "finished_at": run.finished_at.isoformat() if run.finished_at else None})  # fmt: skip
    allocation = [
        {"supplier": a.get("supplier_code"), "quantity": a.get("quantity")} for a in alt.get("allocation", [])
    ]
    checks = alt.get("constraint_checks") or []
    ev.plans.append({
        "key": key, "label": ref.label or f"{run.run_uid} {plan}", "role": ref.role, "source": run.run_uid, "scope": scope, "allocation": allocation,
        "constraint_checks": [{"id": c["id"], "passed": c["passed"]} for c in checks], "feasible": bool(alt.get("feasible", True)),
        "pareto": alt.get("pareto"), "solver": (run.solver or {}).get("type"), "inexact": bool((run.provenance or {}).get("inexact")),
        "reference_kind": kind, "explanation": None,
    })  # fmt: skip
    cells = {NORMAL: _plan_metrics(alt, cur)}
    ev.conditions.append({"key": NORMAL, "name": NORMAL, "mode": "NORMAL", "probability": None})
    mode = (run.scenarios or {}).get("mode") or run.scenario_mode
    for s in alt.get("scenarios") or []:
        k = cond_key(s["name"], s.get("mode") or mode)
        cells[k] = {
            x: y
            for x, y in {
                "UNMET_UNITS": _m(s.get("unmet"), "units"),
                "FULFILMENT": _m(s.get("fulfilment"), "fraction"),
            }.items()
            if y is not None
        }
        ev.conditions.append(
            {
                "key": k,
                "name": s["name"],
                "mode": s.get("mode") or mode,
                "probability": s.get("probability"),
                "kind": s.get("kind"),
            }
        )
    ev.cells[key] = cells
    if kind == "REFERENCE_COST_MINIMAL":
        ev.warnings.append(
            {
                "code": "REFERENCE_IS_NOT_CURRENT",
                "text": f"{ref.run_uid}: the baseline reference is a cost-minimal plan computed from the offers; it is not your current operation.",
            }
        )
    if ev.plans[0]["inexact"]:
        ev.warnings.append(
            {
                "code": "SEARCH_LIMITED",
                "text": f"{ref.run_uid}: the optimiser's search was limited, so its plans are the best of those examined, not proven optimal.",
            }
        )
    if run.result_status == "completed_with_warnings":
        ev.warnings.append(
            {
                "code": "RUN_HAD_WARNINGS",
                "text": f"{ref.run_uid}: the run completed with warnings (see the run); some objectives may be unavailable.",
            }
        )
    return ev


def _sourcing(db: Session, ref: EvidenceRef, limits: Limits) -> Evidence:
    run = db.scalar(select(SourcingRun).where(SourcingRun.run_uid == ref.run_uid))
    if run is None:
        raise EvidenceProblem("RUN_NOT_FOUND", f"Sourcing run {ref.run_uid} does not exist.")
    if run.kind != "OPTIMIZE":
        raise EvidenceProblem(
            "RUN_NOT_USABLE", f"Sourcing run {ref.run_uid} is a strategy comparison; attach an optimisation run."
        )
    if run.status is not SimStatus.COMPLETED or run.result_status != "feasible":
        raise EvidenceProblem(
            "RUN_NOT_USABLE",
            f"Sourcing run {ref.run_uid} is {run.status.value}/{run.result_status}; only a feasible plan can be evidence.",
        )
    m = run.metrics or {}
    req = run.request or {}
    cur = m.get("currency") or req.get("currency")
    scope = {
        "product_id": run.product_id,
        "destination_facility_id": run.destination_facility_id,
        "quantity": req.get("quantity"),
        "unit": req.get("unit"),
        "currency": cur,
    }
    ful = m.get("fulfilment_pct")
    metrics = {
        "MODELLED_COST": _m(m.get("purchase_cost"), "currency", currency=cur, basis="PURCHASE_ONLY"),
        "UNMET_UNITS": _m(m.get("unmet_quantity"), "units"),
        "FULFILMENT": _m(None if ful is None else ful / 100.0, "fraction"),
        "AVG_LEAD_TIME_DAYS": _m(m.get("average_lead_time_days"), "days"),
        "CONCENTRATION_HHI": _m(m.get("hhi"), "index 0-1"),
        "SUPPLIER_COUNT": _m(m.get("supplier_count"), "count"),
    }
    cond = NORMAL if run.scenario_id is None else cond_key(f"scenario #{run.scenario_id}", "REOPTIMISED")
    key = run.run_uid
    ev = Evidence(ref=ref.model_dump(mode="json"), source={"kind": "SOURCING_RUN", "run_uid": run.run_uid, "algorithm_version": run.algorithm_version, "result_status": run.result_status,
                  "hash": run.snapshot_hash, "snapshot_hash": run.snapshot_hash, "finished_at": run.finished_at.isoformat() if run.finished_at else None})  # fmt: skip
    ev.plans.append({"key": key, "label": ref.label or run.run_uid, "role": ref.role, "source": run.run_uid, "scope": scope,
                     "allocation": [{"supplier": a.get("supplier_code"), "quantity": a.get("quantity")} for a in (run.allocations or [])],
                     "constraint_checks": [{"id": c.get("check"), "passed": c.get("passed")} for c in ((run.summary or {}).get("hard_constraint_checks") or [])],
                     "feasible": True, "pareto": None, "solver": ((run.summary or {}).get("solver") or {}).get("optimality"), "inexact": False, "reference_kind": None, "explanation": None})  # fmt: skip
    ev.conditions.append({"key": NORMAL, "name": NORMAL, "mode": "NORMAL", "probability": None})
    if cond != NORMAL:
        ev.conditions.append(
            {"key": cond, "name": f"scenario #{run.scenario_id}", "mode": "REOPTIMISED", "probability": None}
        )
    ev.cells[key] = {cond: {k: v for k, v in metrics.items() if v is not None}}
    if cond != NORMAL:
        ev.warnings.append(
            {
                "code": "REOPTIMISED_UNDER_SCENARIO",
                "text": f"{ref.run_uid}: the plan was re-optimised for scenario #{run.scenario_id}; its difference from a baseline reflects the disruption and the re-planning together.",
            }
        )
    return ev


def _simulation(db: Session, ref: EvidenceRef, limits: Limits) -> Evidence:
    run = db.scalar(select(SimulationRun).where(SimulationRun.run_uid == ref.run_uid))
    if run is None:
        raise EvidenceProblem("RUN_NOT_FOUND", f"Scenario simulation run {ref.run_uid} does not exist.")
    if run.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise EvidenceProblem(
            "RUN_NOT_USABLE",
            f"Scenario simulation run {ref.run_uid} is {run.status.value}; only finished runs can be evidence.",
        )
    s = run.summary or {}
    snap = run.scenario_snapshot or {}
    rows = list(
        db.scalars(
            select(SimulationImpact)
            .where(SimulationImpact.run_id == run.id)
            .order_by(SimulationImpact.depth, SimulationImpact.id)
            .limit(limits.max_impact_rows + 1)
        )
    )
    impacts = [
        {"entity_type": i.entity_type.value, "entity_id": i.entity_id, "entity_name": i.entity_name, "impact_kind": i.impact_kind.value, "depth": i.depth, "quantification": i.quantification.value,
         "exposure_level": i.exposure_level, "loss_pct": i.loss_pct, "loss_pct_lower_bound": i.loss_pct_lower_bound, "added_lead_time_days": i.added_lead_time_days,
         "path": i.primary_path, "event_uids": i.event_uids, "uncertainty": i.uncertainty, "explanation": i.explanation}
        for i in rows[: limits.max_impact_rows]
    ]  # fmt: skip
    metrics = {
        "AFFECTED_ENTITIES": _m(run.entities_affected, "count"),
        "DIRECT_IMPACTS": _m(run.direct_count, "count"),
        "INDIRECT_IMPACTS": _m(run.indirect_count, "count"),
        "AFFECTED_PRODUCTS": _m(s.get("affected_products"), "count"),
        "AFFECTED_FACILITIES": _m(s.get("affected_facilities"), "count"),
    }
    ev = Evidence(ref=ref.model_dump(mode="json"), source={"kind": "SIMULATION_RUN", "run_uid": run.run_uid, "algorithm_version": run.engine_version, "result_status": run.status.value,
                  "hash": run.definition_hash, "snapshot_hash": run.graph_hash, "finished_at": run.finished_at.isoformat() if run.finished_at else None})  # fmt: skip
    ev.exposure.append({
        "key": run.run_uid, "label": ref.label or snap.get("name") or run.run_uid, "scenario_id": run.scenario_id, "scenario_version": run.scenario_version, "metrics": {k: v for k, v in metrics.items() if v},
        "capacity_at_risk_by_unit": s.get("capacity_at_risk_by_unit") or {}, "events": [{"event_type": e.get("event_type"), "label": e.get("label"), "magnitude_pct": e.get("magnitude_pct"), "duration_days": e.get("duration_days")} for e in snap.get("events", [])],
        "impacts": impacts, "impacts_truncated": len(rows) > limits.max_impact_rows or bool(run.truncated), "truncation_reasons": run.truncation_reasons,
        "missing_data": sorted(m.get("code") for m in (run.missing_data or []) if isinstance(m, dict)), "warning_codes": sorted(w.get("code") for w in (run.warnings or []) if isinstance(w, dict)),
        "note": "Reachability through the dependency graph is exposure. It is not a quantified financial or operational loss.",
    })  # fmt: skip
    if run.status is SimStatus.PARTIAL or run.truncated:
        ev.warnings.append(
            {
                "code": "EXPOSURE_INCOMPLETE",
                "text": f"{ref.run_uid}: the simulation was partial or truncated, so its affected set may be incomplete.",
            }
        )
    return ev


ADAPTERS = {"RESILIENCE_RUN": _resilience, "SOURCING_RUN": _sourcing, "SIMULATION_RUN": _simulation}


def extract(db: Session, ref: EvidenceRef, limits: Limits) -> Evidence:
    return ADAPTERS[ref.kind](db, ref, limits)
