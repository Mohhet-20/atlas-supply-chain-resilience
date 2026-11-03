"""Multi-Objective Resilience Optimization API.

Builds supplier-allocation alternatives for one product at one destination from the offers registered in ATLAS, evaluates them under normal
conditions and named stress scenarios (on copies), keeps the non-dominated trade-offs, and stores every run as an immutable, reproducible record.
A result is decision support: ATLAS never places an order, and an approval is an internal record. Data is limited to the caller's organisation; bodies reject
unknown fields (including any organisation id) and within it, data is visible to every user at or above the endpoint role. Prices and cost figures are
reserved for editors and above.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.models.enums import SimStatus
from app.models.resilience import ResilienceDecision, ResilienceRun
from app.registry import audit
from app.registry.audit import request_id_of
from app.resilience import config as C
from app.resilience import data as D
from app.resilience import engine as E
from app.resilience import objectives as O
from app.resilience import plan as PL
from app.resilience import runs as R
from app.resilience.config import ResilienceRequest
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/resilience", tags=["multi-objective resilience optimization"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
UidPath = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
IdemKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
]
NOT_SUPPORTED = [
    "creating, sending or tracking purchase orders or any automatic execution",
    "continuous (off-grid) optimisation: plans are enumerated on a share grid, plus the exact minimum-cost allocation from Sourcing as a reference",
    "transport lane capacity or lane-closure stress (lane events count only through scenarios the Sourcing overlay can map to a supplier)",
    "choosing inventory policies or safety stock (the replenishment module does that; this module takes opening stock as an input)",
    "multi-product, multi-period or multi-echelon coupling, shared budgets or shared storage",
    "scenario probabilities estimated by ATLAS (only requester-supplied probabilities are used)",
    "recovery curves, event timing and partial-period disruption (one-period supply model)",
    "unit and currency conversion",
]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]
    rationale: str = Field(min_length=10, max_length=1000)
    alternative_id: str | None = Field(default=None, min_length=3, max_length=16, pattern=r"^[A-Za-z0-9-]+$")


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Resilience write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED)."
        )


def _limits(request: Request) -> C.Limits:
    s = request.app.state.settings
    return C.Limits(max_active_runs=s.resilience_max_active_runs, time_budget_seconds=s.resilience_time_budget_seconds, max_candidates=s.resilience_max_candidates, max_scenarios=s.resilience_max_scenarios, max_sensitivity_variants=s.resilience_max_sensitivity_variants)  # fmt: skip


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _parse(body: dict[str, Any]) -> ResilienceRequest:
    try:
        return ResilienceRequest.model_validate(body)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


def _hide(actor: Actor, payload: Any) -> Any:
    if actor.role >= Role.EDITOR:
        return payload
    out = R.redact(payload)
    if isinstance(out, dict):
        out["cost_redacted"] = True
    return out


def _run_get(db, uid: str) -> ResilienceRun:  # noqa: ANN001
    r = db.scalar(select(ResilienceRun).where(ResilienceRun.run_uid == uid))
    if r is None:
        raise NotFoundError("Resilience run not found.")
    return r


def _finished(r: ResilienceRun) -> ResilienceRun:
    if r.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise DomainValidationError(f"The run is {r.status.value}; this section exists only for finished runs.")
    return r


def _run(db, r: ResilienceRun, actor: Actor, full: bool = False) -> dict[str, Any]:  # noqa: ANN001
    req = r.request or {}
    rec = r.recommendation or {}
    out: dict[str, Any] = {
        "run_uid": r.run_uid, "status": r.status.value, "result_status": r.result_status, "requested_by": r.requested_by, "product_id": r.product_id,
        "destination_facility_id": r.destination_facility_id, "strategy": r.strategy, "scenario_mode": r.scenario_mode, "quantity": req.get("quantity"), "unit": req.get("unit"),
        "currency": req.get("currency"), "objectives_requested": [o.get("id") for o in req.get("objectives", [])], "stress_count": len(req.get("stress", [])),
        "algorithm_version": r.algorithm_version, "snapshot_hash": r.snapshot_hash, "recommendation_hash": r.recommendation_hash, "queued_at": _iso(r.queued_at),
        "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at), "duration_ms": r.duration_ms, "error": r.error, "recommended_id": rec.get("id"),
        "alternative_count": len(r.alternatives or []), "front_size": (r.pareto or {}).get("front_size"), "inexact": (r.provenance or {}).get("inexact"),
        "approval": R.approval_state(db, r),
    }  # fmt: skip
    if full:
        out.update({k: getattr(r, k) for k in ("request", "request_hash", "objectives", "constraints", "scenarios", "alternatives", "pareto", "recommendation", "comparison", "sensitivity", "diagnostics", "explanation", "warnings", "solver", "statistics", "provenance", "reproducibility")})  # fmt: skip
        out["statements"] = (r.provenance or {}).get("statements") or {}
    return _hide(actor, out)


@router.get("/health", summary="Capabilities, limits and what is not supported")
def health(request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    runs = dict(db.execute(select(ResilienceRun.status, func.count()).group_by(ResilienceRun.status)).all())
    lim = _limits(request)
    return {
        "algorithm_version": C.ALGORITHM_VERSION, "write_api_enabled": request.app.state.settings.write_api_allowed, "runs_by_status": {k.value: v for k, v in runs.items()},
        "limits": {"max_active_runs": lim.max_active_runs, "time_budget_seconds": lim.time_budget_seconds, "max_candidates": lim.max_candidates, "max_eligible_suppliers": lim.max_eligible_suppliers, "max_scenarios": lim.max_scenarios, "max_sensitivity_variants": lim.max_sensitivity_variants},  # fmt: skip
        "solver": E.SOLVER, "algorithms": ["supplier eligibility from Sourcing (sc-1)", "grid enumeration of supplier shares with an independent validator", "exact minimum-cost allocation from Sourcing as an off-grid reference", "weighted sum with min-max normalisation", "Pareto non-dominated filtering", "epsilon-constraint", "bounded one-at-a-time sensitivity", "fixed-plan and adaptive-plan scenario evaluation on copies"],  # fmt: skip
        "not_supported": NOT_SUPPORTED, "statements": C.STATEMENTS,
    }  # fmt: skip


@router.get("/catalogue", summary="Objective definitions, constraints, strategies and stress kinds")
def catalogue(_a: Viewer) -> dict[str, Any]:
    return {
        "objectives": O.REGISTRY, "strategies": [{"id": "WEIGHTED_SUM", "label": "Weighted sum", "note": "One recommended plan for the weights you set; changing weights can change it."}, {"id": "PARETO", "label": "Pareto set", "note": "All non-dominated alternatives; no single winner is declared."}, {"id": "EPSILON_CONSTRAINT", "label": "Primary objective with limits", "note": "Minimise one objective while keeping the others within limits you set."}],  # fmt: skip
        "stress_kinds": [{"id": k, "label": k.replace("_", " ").lower()} for k in C.STRESS_KINDS], "scenario_modes": [{"id": "FIXED", "note": "The allocation is not changed after a disruption."}, {"id": "ADAPTIVE", "note": "Spare capacity of other suppliers can cover a shortfall after response_days."}],  # fmt: skip
        "constraints": [{"id": c[0], "kind": c[1], "description": c[2]} for c in PL.CATALOGUE], "result_statuses": list(C.RESULT_STATUSES), "statements": C.STATEMENTS,
    }  # fmt: skip


@router.post(
    "/availability",
    responses=ERR,
    summary="Which objectives can be computed for this scope, and why not (nothing is stored)",
)
def availability(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req = _parse(body)
    lim = _limits(request)
    R.validate_refs(db, req, lim)
    snap = D.load(db, req, req.as_of or datetime.now().astimezone(), lim)
    problem = {
        "eligible": [s for s in snap["suppliers"] if s["status"] == "ELIGIBLE"],
        "has_stress": bool(snap["scenarios"]),
    }
    av = O.availability(problem)
    counts: dict[str, int] = {}
    for s in snap["suppliers"]:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    out = {"objectives": [{"id": o["id"], "name": o["name"], "direction": o["direction"], "unit": o["unit"], **av[o["id"]]} for o in O.REGISTRY], "eligibility_counts": counts, "eligible_capacity_total": sum(s["capacity"] for s in problem["eligible"] if s["capacity"] is not None), "demand": req.quantity, "opening_stock": snap["opening_stock"], "scenarios_applied": [s["name"] for s in snap["scenarios"]], "warnings": snap["warnings"], "snapshot_hash": snap["hash"]}  # fmt: skip
    return _hide(actor, out)


@router.post(
    "/optimize",
    status_code=202,
    responses=ERR,
    summary="Queue a multi-objective resilience optimisation (idempotent with a key)",
)
def optimize(body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    req = _parse(body)
    lim = _limits(request)
    with db.begin():
        run, created = R.create(db, req, actor.name, lim, idempotency_key=idempotency_key)
        if created:
            audit.record(db, actor, "resilience.run.queued", target_type="RESILIENCE_RUN", target_id=run.id, details={"strategy": req.strategy, "objectives": [o.id for o in req.objectives], "stress": len(req.stress), "mode": req.scenario_mode, "product": req.product_id, "destination": req.destination_facility_id}, request_id=request_id_of(request))  # fmt: skip
        out, rid = _run(db, run, actor), run.id
    if created:
        tasks.add_task(R.execute, request_session_factory(request), rid, lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/runs", summary="Run history (newest first)")
def list_runs(
    db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, status: SimStatus | None = None, product_id: Annotated[int | None, Query(ge=1)] = None,
    destination_facility_id: Annotated[int | None, Query(ge=1)] = None, strategy: Literal["WEIGHTED_SUM", "PARETO", "EPSILON_CONSTRAINT"] | None = None,
    result_status: Annotated[str | None, Query(max_length=32)] = None, requested_by: Annotated[str | None, Query(max_length=64)] = None,
    scenario_mode: Literal["FIXED", "ADAPTIVE"] | None = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(ResilienceRun)
    for col, v in ((ResilienceRun.status, status), (ResilienceRun.product_id, product_id), (ResilienceRun.destination_facility_id, destination_facility_id), (ResilienceRun.strategy, strategy), (ResilienceRun.result_status, result_status), (ResilienceRun.requested_by, requested_by), (ResilienceRun.scenario_mode, scenario_mode)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    rows = db.scalars(q.order_by(ResilienceRun.id.desc()).limit(limit).offset(offset))
    return {"items": [_run(db, r, actor) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{uid}", responses=ERR, summary="One run with every result block, provenance and approval state")
def get_run(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    return _run(db, _run_get(db, uid), actor, full=True)


@router.post("/runs/{uid}/cancel", responses=ERR, summary="Cancel a queued run, or ask a running one to stop")
def cancel_run(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        R.cancel(r)
        audit.record(db, actor, "resilience.run.cancel", target_type="RESILIENCE_RUN", target_id=r.id, details={"status": r.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _run(db, r, actor)


@router.get(
    "/runs/{uid}/alternatives",
    responses=ERR,
    summary="Candidate alternatives (paged); pareto_only keeps the non-dominated ones",
)
def run_alternatives(
    uid: UidPath, db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, pareto_only: bool = False
) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    alts = [a for a in (r.alternatives or []) if a.get("pareto") or not pareto_only]
    return _hide(actor, {"run_uid": r.run_uid, "items": alts[offset : offset + limit], "total": len(alts), "limit": limit, "offset": offset, "pareto": r.pareto, "recommended_id": (r.recommendation or {}).get("id"), "statement": C.STATEMENTS["pareto"]})  # fmt: skip


@router.get("/runs/{uid}/comparison", responses=ERR, summary="Baseline versus candidate on every comparable metric")
def run_comparison(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return _hide(actor, {"run_uid": r.run_uid, "comparison": r.comparison})


@router.get(
    "/runs/{uid}/scenarios", responses=ERR, summary="Scenario definitions and outcomes (fixed or adaptive, labelled)"
)
def run_scenarios(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return _hide(
        actor,
        {
            "run_uid": r.run_uid,
            "scenarios": r.scenarios,
            "statements": {k: C.STATEMENTS[k] for k in ("scenarios", "adaptive", "probabilities")},
        },
    )


@router.get(
    "/runs/{uid}/constraints", responses=ERR, summary="Constraint status, binding limits and infeasibility diagnostics"
)
def run_constraints(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return _hide(
        actor,
        {
            "run_uid": r.run_uid,
            "result_status": r.result_status,
            "constraints": r.constraints,
            "diagnostics": r.diagnostics,
            "statistics": r.statistics,
        },
    )


@router.get("/runs/{uid}/sensitivity", responses=ERR, summary="Sensitivity stored with the run (if it was requested)")
def run_sensitivity(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return _hide(actor, {"run_uid": r.run_uid, "sensitivity": r.sensitivity})


@router.post(
    "/runs/{uid}/sensitivity",
    responses=ERR,
    summary="Run a bounded sensitivity sweep now from the stored snapshot (not stored; editors)",
)
def run_sensitivity_now(
    uid: UidPath, body: dict[str, Any], db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    try:
        cfg = C.SensitivityConfig.model_validate({**body, "enabled": True}).model_dump()
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e
    return {"run_uid": r.run_uid, **R.sensitivity_now(r, cfg, _limits(request))}


@router.get("/runs/{uid}/snapshot", responses=ERR, summary="The exact inputs the engine used (editors and above)")
def run_snapshot(uid: UidPath, db: DbSession, _a: Editor) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return {"run_uid": r.run_uid, "snapshot_hash": r.snapshot_hash, "snapshot": r.snapshot}


@router.post(
    "/runs/{uid}/replay",
    responses=ERR,
    summary="Recompute a finished run from its stored snapshot and compare with the stored result",
)
def replay_run(uid: UidPath, db: DbSession, request: Request, _a: Editor) -> dict[str, Any]:
    return R.replay(db, _run_get(db, uid), _limits(request))


@router.get("/runs/{uid}/decisions", responses=ERR, summary="Approval history (insert-only)")
def run_decisions(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _run_get(db, uid)
    rows = db.scalars(
        select(ResilienceDecision).where(ResilienceDecision.run_id == r.id).order_by(ResilienceDecision.id)
    )
    return {"run_uid": r.run_uid, "current": R.approval_state(db, r), "history": [{"decision": d.decision, "alternative_id": d.alternative_id, "rationale": d.rationale, "decided_by": d.decided_by, "decided_at": _iso(d.decided_at), "recommendation_hash": d.recommendation_hash} for d in rows], "statement": "An approval is an internal decision record. ATLAS does not place orders or execute any plan."}  # fmt: skip


@router.post(
    "/runs/{uid}/decision",
    responses=ERR,
    summary="Approve (naming one alternative) or reject a completed run (reviewers and above)",
)
def decide(uid: UidPath, body: DecisionBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        d = R.decide(db, r, body.decision, body.rationale, body.alternative_id, actor.name, actor.role >= Role.ADMIN)
        audit.record(db, actor, f"resilience.run.{body.decision.lower()}", target_type="RESILIENCE_RUN", target_id=r.id, details={"recommendation_hash": d.recommendation_hash, "alternative": body.alternative_id}, request_id=request_id_of(request))  # fmt: skip
        return {"run_uid": r.run_uid, "approval": R.approval_state(db, r), "recommendation_hash": d.recommendation_hash}
