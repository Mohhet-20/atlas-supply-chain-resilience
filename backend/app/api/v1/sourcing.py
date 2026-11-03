"""Supplier Substitution & Sourcing Optimization API.

Registers supplier offers, discovers and ranks eligible alternatives, optimises allocations under hard constraints, compares
strategies and disruption scenarios, and keeps every run as an immutable, traceable record. A proposed allocation is distinct from
an approved decision, and ATLAS never places an order or changes a contract. Data is limited to the caller's organisation; bodies reject
unknown fields (including any organisation id) and within it, sourcing data is visible to every user at or above the endpoint role.
Unit prices, freight and every cost derived from them are reserved for editors and above.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.models.enums import SimStatus
from app.models.sourcing import APPROVALS, OFFER_STATUSES, SourcingDecision, SourcingOffer, SourcingRun
from app.models.types import utcnow
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse
from app.sourcing import config as C
from app.sourcing import data
from app.sourcing import engine as eng
from app.sourcing import offers as O
from app.sourcing import runs as R
from app.sourcing.config import SourcingRequest

router = APIRouter(prefix="/sourcing", tags=["supplier substitution and sourcing optimization"])
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


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: Literal["ACTIVE", "SUSPENDED", "RETIRED"]


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]
    rationale: str = Field(min_length=10, max_length=1000)


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Sourcing write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _limits(request: Request) -> C.Limits:
    s = request.app.state.settings
    return C.Limits(
        max_candidates=s.sourcing_max_candidates, max_active_runs=s.sourcing_max_active_runs, time_budget_seconds=s.sourcing_time_budget_seconds,
        max_nodes=s.sourcing_max_nodes, max_grid_units=s.sourcing_max_grid_units, min_reliability_sample=s.sourcing_min_reliability_sample,
        max_delivery_rows=s.sourcing_max_delivery_rows, stale_days=s.sourcing_stale_days,
    )  # fmt: skip


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _page(db, q, limit, offset, shape) -> dict[str, Any]:  # noqa: ANN001
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    return {
        "items": [shape(r) for r in db.scalars(q.limit(limit).offset(offset))],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _parse(model, body: dict[str, Any]):  # noqa: ANN001, ANN202
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


def _hide(actor: Actor, payload: Any) -> Any:
    if actor.role >= Role.EDITOR:
        return payload
    out = R.redact(payload)
    if isinstance(out, dict):
        out["cost_redacted"] = True
    return out


def _run_get(db, uid: str) -> SourcingRun:  # noqa: ANN001
    r = db.scalar(select(SourcingRun).where(SourcingRun.run_uid == uid))
    if r is None:
        raise NotFoundError("Sourcing run not found.")
    return r


def _run(db, r: SourcingRun, actor: Actor, full: bool = False) -> dict[str, Any]:  # noqa: ANN001
    m = r.metrics or {}
    out = {
        "run_uid": r.run_uid, "kind": r.kind, "status": r.status.value, "result_status": r.result_status, "requested_by": r.requested_by,
        "product_id": r.product_id, "destination_facility_id": r.destination_facility_id, "scenario_id": r.scenario_id,
        "objective": (r.request or {}).get("objective"), "strategy": (r.request or {}).get("strategy"), "demand": (r.request or {}).get("quantity"),
        "unit": (r.request or {}).get("unit"), "algorithm_version": r.algorithm_version, "snapshot_hash": r.snapshot_hash, "scenario_hash": r.scenario_hash,
        "queued_at": _iso(r.queued_at), "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at), "duration_ms": r.duration_ms, "error": r.error,
        "supplier_count": m.get("supplier_count"), "unmet_quantity": m.get("unmet_quantity"), "hhi": m.get("hhi"),
        "hypothetical": bool((r.summary or {}).get("hypothetical")), "approval": R.approval_state(db, r) if r.kind == "OPTIMIZE" else None,
        "purchase_cost": m.get("purchase_cost"),
    }  # fmt: skip
    if full:
        out.update({k: getattr(r, k) for k in ("request", "request_hash", "candidates", "allocations", "baseline_allocations", "comparison", "infeasibility", "metrics", "disruptions", "summary", "warnings", "provenance", "reproducibility")})  # fmt: skip
        out["statements"] = [
            C.STATEMENTS[k]
            for k in ("decision_support", "score", "cost", "missing", "concentration", "resilience", "tenancy")
        ]
    return _hide(actor, out)


# ------------------------------------------------------------------------------------------------ meta
@router.get("/health", summary="Capabilities, limits and what is not supported")
def health(request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    s = request.app.state.settings
    runs = dict(db.execute(select(SourcingRun.status, func.count()).group_by(SourcingRun.status)).all())
    return {
        "algorithm_version": C.ALGORITHM_VERSION, "write_api_enabled": s.write_api_allowed, "offers": db.scalar(select(func.count()).select_from(SourcingOffer)) or 0,
        "runs_by_status": {k.value: v for k, v in runs.items()}, "approvals": list(APPROVALS), "offer_statuses": list(OFFER_STATUSES),
        "limits": {"max_candidates": s.sourcing_max_candidates, "max_active_runs": s.sourcing_max_active_runs, "time_budget_seconds": s.sourcing_time_budget_seconds, "max_nodes": s.sourcing_max_nodes, "max_grid_units": s.sourcing_max_grid_units, "min_reliability_sample": s.sourcing_min_reliability_sample},  # fmt: skip
        "algorithms": ["rule-based eligibility with per-rule evidence", "min-max normalised weighted suitability score", "exact branch and bound for continuous quantities", "exact dynamic programming on the order-increment grid", "bisection on the largest supplier share for the resilience objective"],  # fmt: skip
        "not_supported": ["placing purchase orders or changing contracts", "unit conversion and currency conversion", "landed cost beyond a stated per-unit freight", "multi-period or multi-product joint sourcing", "price breaks and volume discounts", "enterprise-wide concentration", "automatic approval or execution"],  # fmt: skip
        "statements": C.STATEMENTS,
    }  # fmt: skip


@router.get("/objectives", summary="Objectives and what each minimises")
def objectives(_a: Viewer) -> dict[str, Any]:
    return {
        "objectives": C.objective_catalogue(), "strategies": [{"id": "AUTO", "note": "any number of suppliers"}, {"id": "SINGLE", "note": "exactly one supplier"}, {"id": "DUAL", "note": "exactly two suppliers"}, {"id": "MULTI", "note": "at least two suppliers"}],  # fmt: skip
        "metrics": [{"name": m, "direction": C.DIRECTION[m], "default_weight": C.DEFAULT_WEIGHTS[m]} for m in C.METRICS],
        "normalisation": "Min-max across the eligible candidates; 1 is best. A missing metric scores 0 and flags the ranking incomplete.",
        "tie_break": ["higher suitability score", "lower unit price", "supplier code"],
    }  # fmt: skip


@router.get("/constraints", summary="Hard constraints and how each is judged")
def constraints(_a: Viewer) -> dict[str, Any]:
    return {
        "statuses": {"ELIGIBLE": "passed every hard rule on stated data", "ELIGIBLE_AFTER_QUALIFICATION": "qualification is pending; never allocated", "MISSING_INFORMATION": "a required fact is absent or in another unit/currency; never assumed", "UNAVAILABLE": "temporarily not usable", "EXCLUDED": "removed by a request filter or threshold", "DISQUALIFIED": "explicitly or structurally not allowed"},  # fmt: skip
        "constraints": C.constraint_catalogue(),
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------ offers
@router.post(
    "/offers", status_code=201, responses=ERR, summary="Register a supplier offer (terms are immutable once saved)"
)
def create_offer(body: dict[str, Any], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    b = _parse(O.OfferIn, body)
    with db.begin():
        row = O.create(db, b, actor.name)
        audit.record(db, actor, "sourcing.offer.create", target_type="SOURCING_OFFER", target_id=row.id, details={"supplier": row.supplier_id, "product": row.product_id, "destination": row.destination_facility_id, "approval": row.approval_status}, request_id=request_id_of(request))  # fmt: skip
        return O.out(row)


@router.get("/offers", summary="Offers (newest first). Prices are shown to editors and above only")
def list_offers(
    db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, product_id: Annotated[int | None, Query(ge=1)] = None, supplier_id: Annotated[int | None, Query(ge=1)] = None,
    destination_id: Annotated[int | None, Query(ge=1)] = None, status: Annotated[str | None, Query(max_length=12)] = None, approval: Annotated[str | None, Query(max_length=24)] = None,
) -> dict[str, Any]:  # fmt: skip
    if status is not None and status not in OFFER_STATUSES:
        raise DomainValidationError(f"Unknown status. Use: {', '.join(OFFER_STATUSES)}.")
    if approval is not None and approval not in APPROVALS:
        raise DomainValidationError(f"Unknown approval status. Use: {', '.join(APPROVALS)}.")
    q = select(SourcingOffer)
    for col, v in ((SourcingOffer.product_id, product_id), (SourcingOffer.supplier_id, supplier_id), (SourcingOffer.destination_facility_id, destination_id), (SourcingOffer.status, status), (SourcingOffer.approval_status, approval)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(
        db, q.order_by(SourcingOffer.id.desc()), limit, offset, lambda r: O.out(r, redact=actor.role < Role.EDITOR)
    )


@router.get("/offers/{uid}", responses=ERR, summary="One offer")
def get_offer(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    return O.out(O.get(db, uid), redact=actor.role < Role.EDITOR)


@router.post("/offers/{uid}/status", responses=ERR, summary="Suspend, reactivate or retire an offer (retired is final)")
def offer_status(uid: UidPath, body: StatusBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        row = O.get(db, uid)
        frm = row.status
        O.set_status(row, body.to)
        audit.record(db, actor, "sourcing.offer.status", target_type="SOURCING_OFFER", target_id=row.id, details={"from": frm, "to": body.to}, request_id=request_id_of(request))  # fmt: skip
        return O.out(row)


# ------------------------------------------------------------------------------------------------ discovery (read-only)
def _discover(db, request: Request, body: dict[str, Any]) -> tuple[SourcingRequest, dict[str, Any]]:  # noqa: ANN001
    req = _parse(SourcingRequest, body)
    lim = _limits(request)
    R.validate_refs(db, req)
    now = req.as_of or utcnow()
    cands, ctx, warns, trunc = data.load_candidates(db, req, lim, now)
    p = eng.plan(cands, ctx, req, lim, solve_plan=False)
    p["warnings"] = warns + p["ranking_warnings"] + (["The candidate list was truncated."] if trunc else [])
    return req, p


@router.post(
    "/alternatives",
    responses=ERR,
    summary="Alternative suppliers for a requirement, with eligibility evidence and ranking (no optimisation)",
)
def alternatives(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req, p = _discover(db, request, body)
    out = {"requirement": req.canonical(), "counts": p["counts"], "price_basis": p["price_basis"], "ranking_incomplete": p["ranking_incomplete"], "candidates": p["candidates"], "warnings": p["warnings"], "statements": [C.STATEMENTS["score"], C.STATEMENTS["missing"]]}  # fmt: skip
    return _hide(actor, out)


@router.post("/eligibility", responses=ERR, summary="Eligibility status and the evidence behind it, per candidate")
def eligibility(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    _, p = _discover(db, request, body)
    rows = [
        {
            k: c[k]
            for k in (
                "offer_uid",
                "supplier_id",
                "supplier_code",
                "supplier_name",
                "status",
                "reasons",
                "missing",
                "evidence",
            )
        }
        for c in p["candidates"]
    ]
    return {
        "counts": p["counts"],
        "candidates": rows,
        "statuses": [
            "ELIGIBLE",
            "ELIGIBLE_AFTER_QUALIFICATION",
            "MISSING_INFORMATION",
            "UNAVAILABLE",
            "EXCLUDED",
            "DISQUALIFIED",
        ],
    }


@router.post(
    "/ranking", responses=ERR, summary="Suitability ranking of the eligible candidates, with component contributions"
)
def ranking(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req, p = _discover(db, request, body)
    ranked = [c for c in p["candidates"] if c["rank"] is not None]
    out = {"weights": req.weights.normalised(), "price_basis": p["price_basis"], "ranking_incomplete": p["ranking_incomplete"], "ranked": ranked, "not_ranked": [{k: c[k] for k in ("offer_uid", "supplier_code", "status", "reasons")} for c in p["candidates"] if c["rank"] is None], "warnings": p["warnings"], "algorithm_version": C.ALGORITHM_VERSION, "statement": C.STATEMENTS["score"]}  # fmt: skip
    return _hide(actor, out)


@router.post("/validate", responses=ERR, summary="Check a requirement and its constraints without optimising")
def validate(body: dict[str, Any], db: DbSession, request: Request, actor: Viewer) -> dict[str, Any]:
    req = _parse(SourcingRequest, body)
    problems = eng.validate_request(req)
    refs_error = None
    try:
        R.validate_refs(db, req)
    except Exception as e:  # noqa: BLE001
        refs_error = str(getattr(e, "message", e))[:300]
    if refs_error and not problems:
        problems.append(refs_error)
    out: dict[str, Any] = {"valid": not problems, "problems": problems}
    if not refs_error:
        _, p = _discover(db, request, body)
        elig = [c for c in p["candidates"] if c["status"] == "ELIGIBLE"]
        cap = sum((c["terms"]["effective_capacity"] or 0.0) for c in elig)
        out.update({"counts": p["counts"], "eligible_capacity": cap, "demand": req.quantity, "capacity_sufficient": cap >= req.quantity, "capacity_shortfall": max(0.0, req.quantity - cap), "warnings": p["warnings"]})  # fmt: skip
    return out


# ------------------------------------------------------------------------------------------------ optimise / compare / runs
def _queue(
    body: dict[str, Any],
    tasks: BackgroundTasks,
    response: Response,
    db,
    request: Request,
    actor: Actor,
    key: str | None,
    kind: str,
) -> dict[str, Any]:  # noqa: ANN001
    _writes(request)
    req = _parse(SourcingRequest, body)
    lim = _limits(request)
    with db.begin():
        run, created = R.create(db, req, actor.name, lim, kind=kind, idempotency_key=key)
        if created:
            audit.record(db, actor, "sourcing.run.queued", target_type="SOURCING_RUN", target_id=run.id, details={"kind": kind, "objective": req.objective, "strategy": req.strategy, "product": req.product_id, "destination": req.destination_facility_id, "scenario": req.scenario_id}, request_id=request_id_of(request))  # fmt: skip
        out, rid = _run(db, run, actor), run.id
    if created:
        tasks.add_task(R.execute, request_session_factory(request), rid, lim)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.post(
    "/optimize", status_code=202, responses=ERR, summary="Queue a sourcing optimisation (idempotent with a key)"
)
def optimize(body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    return _queue(body, tasks, response, db, request, actor, idempotency_key, "OPTIMIZE")


@router.post(
    "/compare-strategies", status_code=202, responses=ERR, summary="Queue a single/dual/multi/auto strategy comparison"
)
def compare_strategies(body: dict[str, Any], tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor, idempotency_key: IdemKey = None) -> dict[str, Any]:  # fmt: skip
    return _queue(body, tasks, response, db, request, actor, idempotency_key, "COMPARE")


@router.get("/runs", summary="Sourcing run history (newest first)")
def list_runs(
    db: DbSession, actor: Viewer, limit: Lim = 20, offset: Off = 0, status: SimStatus | None = None, kind: Literal["OPTIMIZE", "COMPARE"] | None = None,
    product_id: Annotated[int | None, Query(ge=1)] = None, destination_id: Annotated[int | None, Query(ge=1)] = None, scenario_id: Annotated[int | None, Query(ge=1)] = None,
    result_status: Annotated[str | None, Query(max_length=24)] = None,
) -> dict[str, Any]:  # fmt: skip
    q = select(SourcingRun)
    for col, v in ((SourcingRun.status, status), (SourcingRun.kind, kind), (SourcingRun.product_id, product_id), (SourcingRun.destination_facility_id, destination_id), (SourcingRun.scenario_id, scenario_id), (SourcingRun.result_status, result_status)):  # fmt: skip
        if v is not None:
            q = q.where(col == v)
    return _page(db, q.order_by(SourcingRun.id.desc()), limit, offset, lambda r: _run(db, r, actor))


@router.get(
    "/runs/{uid}", responses=ERR, summary="One run: candidates, allocation, breakdowns, provenance and approval state"
)
def get_run(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    return _run(db, _run_get(db, uid), actor, full=True)


def _finished(r: SourcingRun) -> SourcingRun:
    if r.status not in (SimStatus.COMPLETED, SimStatus.PARTIAL):
        raise DomainValidationError(f"The run is {r.status.value}; this section exists only for finished runs.")
    return r


@router.post("/runs/{uid}/cancel", responses=ERR, summary="Cancel a queued run, or ask a running one to stop")
def cancel_run(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        if r.requested_by != actor.name and actor.role < Role.REVIEWER:
            raise ForbiddenError("Only the requester or a reviewer can cancel this run.")
        R.cancel(r)
        audit.record(db, actor, "sourcing.run.cancel", target_type="SOURCING_RUN", target_id=r.id, details={"status": r.status.value}, request_id=request_id_of(request))  # fmt: skip
        return _run(db, r, actor)


@router.get(
    "/runs/{uid}/allocation",
    responses=ERR,
    summary="The proposed allocation, unmet demand and hard-constraint validation",
)
def run_allocation(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    if r.kind != "OPTIMIZE":
        raise DomainValidationError(
            "Strategy comparison runs hold several allocations; use /runs/{uid} or /comparison."
        )
    s = r.summary or {}
    out = {"run": _run(db, r, actor), "allocations": r.allocations, "metrics": r.metrics, "hard_constraint_checks": s.get("hard_constraint_checks"), "infeasibility": r.infeasibility, "solver": s.get("solver"), "label": "PROPOSED - not an approved or executed procurement decision unless an approval is recorded", "warnings": r.warnings}  # fmt: skip
    return _hide(actor, out)


@router.get(
    "/runs/{uid}/comparison", responses=ERR, summary="Baseline versus scenario, or the strategy comparison table"
)
def run_comparison(uid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    if r.comparison is None:
        raise DomainValidationError(
            "This run applied no scenario or what-if and is not a strategy comparison, so there is nothing to compare."
        )
    out = {"run_uid": r.run_uid, "kind": r.kind, "scenario_hash": r.scenario_hash, "snapshot_hash": r.snapshot_hash, "disruptions": r.disruptions, "comparison": r.comparison, "baseline_allocations": r.baseline_allocations, "scenario_allocations": r.allocations if r.kind == "OPTIMIZE" else None, "statement": C.STATEMENTS["resilience"]}  # fmt: skip
    return _hide(actor, out)


@router.get(
    "/runs/{uid}/breakdown",
    responses=ERR,
    summary="Cost, delivery and concentration breakdown of the allocation (editors and above)",
)
def run_breakdown(uid: UidPath, db: DbSession, _a: Editor) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    if r.kind != "OPTIMIZE":
        raise DomainValidationError("The breakdown exists for optimisation runs.")
    rows = r.allocations or []
    lines = [{"offer_uid": a["offer_uid"], "supplier_code": a["supplier_code"], "quantity": a["quantity"], "share": a["share"], "unit_price": a["unit_price"], "freight_per_unit": a["freight_per_unit"], "purchase_cost": a["purchase_cost"], "lead_time_days": a["lead_time_days"], "capacity_utilisation": a["capacity_utilisation"]} for a in rows]  # fmt: skip
    return {
        "run_uid": r.run_uid,
        "lines": lines,
        "metrics": r.metrics,
        "statement": C.STATEMENTS["cost"],
        "concentration_statement": C.STATEMENTS["concentration"],
    }


@router.get(
    "/runs/{uid}/snapshot", responses=ERR, summary="The exact candidate terms the optimiser used (editors and above)"
)
def run_snapshot(uid: UidPath, db: DbSession, _a: Editor) -> dict[str, Any]:
    r = _finished(_run_get(db, uid))
    return {"run_uid": r.run_uid, "snapshot_hash": r.snapshot_hash, "candidates": r.snapshot}


@router.post(
    "/runs/{uid}/replay",
    responses=ERR,
    summary="Recompute a finished run from its stored request and compare with the stored result",
)
def replay_run(uid: UidPath, db: DbSession, request: Request, _a: Editor) -> dict[str, Any]:
    return R.replay(db, _run_get(db, uid), _limits(request))


@router.get("/runs/{uid}/decisions", responses=ERR, summary="Approval history (insert-only)")
def run_decisions(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = _run_get(db, uid)
    rows = db.scalars(select(SourcingDecision).where(SourcingDecision.run_id == r.id).order_by(SourcingDecision.id))
    return {"run_uid": r.run_uid, "current": R.approval_state(db, r), "history": [{"decision": d.decision, "rationale": d.rationale, "decided_by": d.decided_by, "decided_at": _iso(d.decided_at), "allocation_hash": d.allocation_hash} for d in rows], "statement": "An approval is an internal decision record. ATLAS does not place orders, change contracts or execute purchases."}  # fmt: skip


@router.post(
    "/runs/{uid}/decision",
    responses=ERR,
    summary="Approve or reject a feasible proposed allocation (reviewers and above)",
)
def decide(uid: UidPath, body: DecisionBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = _run_get(db, uid)
        d = R.decide(db, r, body.decision, body.rationale, actor.name, actor.role >= Role.ADMIN)
        audit.record(db, actor, f"sourcing.run.{body.decision.lower()}", target_type="SOURCING_RUN", target_id=r.id, details={"allocation_hash": d.allocation_hash}, request_id=request_id_of(request))  # fmt: skip
        return {"run_uid": r.run_uid, "approval": R.approval_state(db, r), "allocation_hash": d.allocation_hash}
