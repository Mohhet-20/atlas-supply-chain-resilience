"""Scenario Comparison & Decision Support API.

A decision case compares results that earlier modules already produced (resilience, sourcing and scenario-simulation runs), freezes the values it
read into an immutable evaluation, and records a traceable human review. It never starts an optimisation, edits a scenario or executes a decision:
approval is an internal record. Data is limited to the caller's organisation; bodies reject unknown fields (including any organisation id). Cost figures are
reserved for editors and above.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, NotFoundError, WriteDisabledError
from app.decision import compare, service
from app.decision import metrics as M
from app.decision.config import ALGORITHM_VERSION, METRIC_REGISTRY_VERSION, STATEMENTS, CaseDefinition, Limits
from app.models.decision import CASE_STATUSES, DecisionCase, DecisionCaseVersion, DecisionEvaluation, DecisionEvent
from app.models.enums import SimStatus
from app.models.resilience import ResilienceRun
from app.models.scenario import SimulationRun
from app.models.sourcing import SourcingRun
from app.registry import audit
from app.registry.audit import request_id_of
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/decision", tags=["scenario comparison and decision support"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
UidPath = Annotated[str, Path(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Lim = Annotated[int, Query(ge=1, le=200)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
NOT_SUPPORTED = [
    "starting or scheduling the runs that serve as evidence (create them in the Sourcing, Replenishment, Resilience and Scenario centers, then attach them)",
    "creating or editing scenarios (use the Scenario Studio; a case references runs of scenarios, whose definitions are frozen inside those runs)",
    "reading replenishment runs (no inventory-level, holding-cost, shortage-cost or stockout metrics) or route runs",
    "time-series comparison (no attached evidence source reports a time series)",
    "recovery time (no recovery model exists)",
    "scenario probabilities estimated by ATLAS (only user-supplied or run-supplied probabilities are used)",
    "executing a decision: approval changes nothing operational",
]


def _limits(request: Request) -> Limits:
    s = request.app.state.settings
    return Limits(max_evidence=s.decision_max_evidence, max_events_per_case=s.decision_max_events_per_case, max_evaluations_per_case=s.decision_max_evaluations_per_case, max_impact_rows=s.decision_max_impact_rows)  # fmt: skip


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Decision write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _iso(d: Any) -> str | None:
    return None if d is None else d.isoformat()


def _parse(body: dict[str, Any]) -> CaseDefinition:
    try:
        return CaseDefinition.model_validate(body)
    except ValidationError as e:
        raise DomainValidationError("; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e


class UpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    definition: dict[str, Any]


class CloneBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=3, max_length=200)


class ReviewerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: str = Field(min_length=1, max_length=64)


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["COMMENT", "CONCERN", "ASSUMPTION"] = "COMMENT"
    text: str = Field(min_length=3, max_length=2000)
    alternative_key: str | None = Field(default=None, min_length=1, max_length=80)


class ReturnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=10, max_length=2000)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["APPROVED", "REJECTED"]
    rationale: str = Field(min_length=10, max_length=2000)
    alternative_key: str | None = Field(default=None, min_length=1, max_length=80)


class SupersedeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=10, max_length=2000)
    superseded_by: str | None = Field(default=None, min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")


class CompareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evaluation_uids: list[Annotated[str, Field(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]] = Field(
        min_length=2, max_length=5
    )


def _case_view(db, c: DecisionCase, full: bool = True) -> dict[str, Any]:  # noqa: ANN001
    out: dict[str, Any] = {
        "case_uid": c.case_uid, "title": c.title, "status": c.status, "version": c.version, "created_by": c.created_by, "reviewer": c.reviewer, "cloned_from": c.cloned_from, "superseded_by": c.superseded_by,
        "current_evaluation_uid": c.current_evaluation_uid, "selected_alternative": c.selected_alternative, "created_at": _iso(c.created_at), "updated_at": _iso(c.updated_at),
        "allowed_transitions": sorted(service.TRANSITIONS[c.status]),
    }  # fmt: skip
    if full:
        raw, _ = service.definition_of(db, c)
        out["definition"] = raw
        out["approval"] = service.approval_state(db, c)
        out["evaluation_count"] = db.scalar(
            select(func.count()).select_from(DecisionEvaluation).where(DecisionEvaluation.case_id == c.id)
        )
        out["event_count"] = db.scalar(
            select(func.count()).select_from(DecisionEvent).where(DecisionEvent.case_id == c.id)
        )
    return out


def _eval_summary(e: DecisionEvaluation) -> dict[str, Any]:
    r = e.result
    return {"evaluation_uid": e.evaluation_uid, "case_version": e.case_version, "result_status": e.result_status, "algorithm_version": e.algorithm_version, "metric_registry_version": e.metric_registry_version,
            "result_hash": e.result_hash, "snapshot_hash": e.snapshot_hash, "evaluated_by": e.evaluated_by, "evaluated_at": _iso(e.evaluated_at), "duration_ms": e.duration_ms,
            "alternatives": [{"key": a["key"], "classification": a["classification"]} for a in r.get("alternatives", [])], "warning_count": len(e.warnings or [])}  # fmt: skip


def _eval_view(e: DecisionEvaluation, actor: Actor) -> dict[str, Any]:
    out = _eval_summary(e)
    res = e.result if actor.role >= Role.EDITOR else compare.cost_hidden(e.result)
    out["result"] = res
    if actor.role < Role.EDITOR:
        out["cost_redacted"] = True
    return out


def _evt(e: DecisionEvent) -> dict[str, Any]:
    return {"id": e.id, "kind": e.kind, "from_status": e.from_status, "to_status": e.to_status, "actor": e.actor, "at": _iso(e.at), "text": e.text, "evaluation_uid": e.evaluation_uid, "alternative_key": e.alternative_key, "data": e.data}  # fmt: skip


@router.get("/health", summary="Version, limits, workflow and what is not supported")
def health(request: Request, db: DbSession, _a: Viewer) -> dict[str, Any]:
    lim = _limits(request)
    counts = dict(db.execute(select(DecisionCase.status, func.count()).group_by(DecisionCase.status)).all())
    return {"algorithm_version": ALGORITHM_VERSION, "metric_registry_version": METRIC_REGISTRY_VERSION, "write_api_enabled": request.app.state.settings.write_api_allowed, "cases_by_status": counts,
            "limits": {"max_evidence": lim.max_evidence, "max_events_per_case": lim.max_events_per_case, "max_evaluations_per_case": lim.max_evaluations_per_case, "max_impact_rows": lim.max_impact_rows},
            "statuses": list(CASE_STATUSES), "transitions": {k: sorted(v) for k, v in service.TRANSITIONS.items()}, "evidence_kinds": ["RESILIENCE_RUN", "SOURCING_RUN", "SIMULATION_RUN"],
            "not_supported": NOT_SUPPORTED, "statements": STATEMENTS}  # fmt: skip


@router.get(
    "/metrics",
    summary="The metric registry: definition, unit, direction, time basis, missing-data behaviour, and the metrics that are not available",
)
def metrics(_a: Viewer) -> dict[str, Any]:
    return {"registry_version": METRIC_REGISTRY_VERSION, "metrics": M.REGISTRY, "unavailable": M.UNAVAILABLE}


@router.get("/evidence/candidates", summary="Finished runs that can be attached as evidence (newest first)")
def candidates(
    db: DbSession,
    _a: Viewer,
    kind: Literal["RESILIENCE_RUN", "SOURCING_RUN", "SIMULATION_RUN"] | None = None,
    limit: Lim = 50,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if kind in (None, "RESILIENCE_RUN"):
        for r in db.scalars(select(ResilienceRun).where(ResilienceRun.status == SimStatus.COMPLETED, ResilienceRun.result_status.in_(("completed", "completed_with_warnings"))).order_by(ResilienceRun.id.desc()).limit(limit)):  # fmt: skip
            items.append({"kind": "RESILIENCE_RUN", "run_uid": r.run_uid, "result_status": r.result_status, "product_id": r.product_id, "destination_facility_id": r.destination_facility_id, "quantity": (r.request or {}).get("quantity"), "unit": (r.request or {}).get("unit"), "recommended_id": (r.recommendation or {}).get("id"), "alternatives": [a["id"] for a in (r.alternatives or [])][:50], "has_reference_plan": bool((r.comparison or {}).get("reference_plan")), "finished_at": _iso(r.finished_at)})  # fmt: skip
    if kind in (None, "SOURCING_RUN"):
        for s in db.scalars(select(SourcingRun).where(SourcingRun.status == SimStatus.COMPLETED, SourcingRun.kind == "OPTIMIZE", SourcingRun.result_status == "feasible").order_by(SourcingRun.id.desc()).limit(limit)):  # fmt: skip
            items.append({"kind": "SOURCING_RUN", "run_uid": s.run_uid, "result_status": s.result_status, "product_id": s.product_id, "destination_facility_id": s.destination_facility_id, "quantity": (s.request or {}).get("quantity"), "unit": (s.request or {}).get("unit"), "scenario_id": s.scenario_id, "finished_at": _iso(s.finished_at)})  # fmt: skip
    if kind in (None, "SIMULATION_RUN"):
        for m in db.scalars(select(SimulationRun).where(SimulationRun.status.in_((SimStatus.COMPLETED, SimStatus.PARTIAL))).order_by(SimulationRun.id.desc()).limit(limit)):  # fmt: skip
            items.append({"kind": "SIMULATION_RUN", "run_uid": m.run_uid, "result_status": m.status.value, "scenario_id": m.scenario_id, "scenario_version": m.scenario_version, "name": (m.scenario_snapshot or {}).get("name"), "entities_affected": m.entities_affected, "finished_at": _iso(m.finished_at)})  # fmt: skip
    return {
        "items": items,
        "note": "Runs are created in their own centers. Only finished runs with a usable result are listed.",
    }


@router.post(
    "/cases/validate", responses=ERR, summary="Check a definition against the persisted runs without saving anything"
)
def validate_definition(body: dict[str, Any], db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    return service.validate(db, _parse(body), _limits(request))


@router.post("/cases", status_code=201, responses=ERR, summary="Create a decision case (editors and above)")
def create_case(body: dict[str, Any], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    d = _parse(body)
    with db.begin():
        c = service.create(db, d, actor.name, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.created",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"title": d.title, "evidence": len(d.evidence)},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.get("/cases", summary="List cases (newest first) with filters")
def list_cases(db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0, status: Annotated[str | None, Query(pattern="^(" + "|".join(CASE_STATUSES) + ")$")] = None, created_by: Annotated[str | None, Query(max_length=64)] = None, reviewer: Annotated[str | None, Query(max_length=64)] = None, q: Annotated[str | None, Query(max_length=100)] = None, sort: Literal["updated", "created", "title"] = "updated") -> dict[str, Any]:  # fmt: skip
    st = select(DecisionCase)
    if status:
        st = st.where(DecisionCase.status == status)
    if created_by:
        st = st.where(DecisionCase.created_by == created_by)
    if reviewer:
        st = st.where(DecisionCase.reviewer == reviewer)
    if q:
        st = st.where(DecisionCase.title.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    total = db.scalar(select(func.count()).select_from(st.subquery()))
    order = {
        "updated": DecisionCase.updated_at.desc(),
        "created": DecisionCase.id.desc(),
        "title": DecisionCase.title.asc(),
    }[sort]
    rows = list(db.scalars(st.order_by(order, DecisionCase.id.desc()).limit(limit).offset(offset)))
    items = []
    for c in rows:
        v = _case_view(db, c, full=False)
        ev = (
            db.scalar(select(DecisionEvaluation).where(DecisionEvaluation.evaluation_uid == c.current_evaluation_uid))
            if c.current_evaluation_uid
            else None
        )
        v["current_result_status"] = ev.result_status if ev else None
        v["alternative_count"] = len(ev.result.get("alternatives", [])) if ev else None
        items.append(v)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _case(db, uid: str) -> DecisionCase:  # noqa: ANN001
    return service.get_case(db, uid)


@router.get("/cases/{uid}", summary="One case with its latest definition and approval state")
def get_case(uid: UidPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _case_view(db, _case(db, uid))


@router.put(
    "/cases/{uid}",
    responses=ERR,
    summary="Edit a case: creates a new version (optimistic concurrency via expected_version)",
)
def edit_case(uid: UidPath, body: UpdateBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    d = _parse(body.definition)
    with db.begin():
        c = service.update(db, _case(db, uid), d, actor.name, body.expected_version, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.versioned",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"version": c.version},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.post(
    "/cases/{uid}/clone", status_code=201, responses=ERR, summary="Copy the latest definition into a new draft case"
)
def clone_case(uid: UidPath, body: CloneBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = service.clone(db, _case(db, uid), actor.name, _limits(request), body.title)
        audit.record(
            db,
            actor,
            "decision.case.cloned",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"from": uid},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.get("/cases/{uid}/versions", summary="Every definition version of a case (insert-only)")
def versions(uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0) -> dict[str, Any]:
    c = _case(db, uid)
    total = db.scalar(select(func.count()).select_from(DecisionCaseVersion).where(DecisionCaseVersion.case_id == c.id))
    rows = db.scalars(
        select(DecisionCaseVersion)
        .where(DecisionCaseVersion.case_id == c.id)
        .order_by(DecisionCaseVersion.version.desc())
        .limit(limit)
        .offset(offset)
    )
    return {"items": [{"version": v.version, "definition_hash": v.definition_hash, "created_by": v.created_by, "created_at": _iso(v.created_at), "definition": v.definition} for v in rows], "total": total, "limit": limit, "offset": offset}  # fmt: skip


@router.post(
    "/cases/{uid}/ready", responses=ERR, summary="Validate the definition and mark the case ready for evaluation"
)
def ready(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = _case(db, uid)
        rep = service.mark_ready(db, c, actor.name, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.validated",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"ok": rep["ok"], "issues": len(rep["issues"])},
            request_id=request_id_of(request),
        )
        return {"case": _case_view(db, c), "validation": rep}


@router.post(
    "/cases/{uid}/evaluate",
    responses=ERR,
    summary="Read the attached runs, freeze their values and compare (idempotent for identical evidence)",
)
def evaluate(uid: UidPath, response: Response, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = _case(db, uid)
        ev, reused = service.evaluate(db, c, actor.name, _limits(request))
        if not reused:
            audit.record(db, actor, "decision.case.evaluated", target_type="DECISION_CASE", target_id=c.id, details={"evaluation": ev.evaluation_uid, "result_status": ev.result_status}, request_id=request_id_of(request))  # fmt: skip
        out = {"case": _case_view(db, c), "evaluation": _eval_view(ev, actor), "reused": reused}
    if reused:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/cases/{uid}/evaluations", summary="Evaluations of a case (newest first)")
def evaluations(uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0) -> dict[str, Any]:
    c = _case(db, uid)
    total = db.scalar(select(func.count()).select_from(DecisionEvaluation).where(DecisionEvaluation.case_id == c.id))
    rows = db.scalars(
        select(DecisionEvaluation)
        .where(DecisionEvaluation.case_id == c.id)
        .order_by(DecisionEvaluation.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return {"items": [_eval_summary(e) for e in rows], "total": total, "limit": limit, "offset": offset}


@router.get(
    "/cases/{uid}/evaluations/{euid}", summary="One evaluation with its comparison, aggregation and alternatives"
)
def evaluation(uid: UidPath, euid: UidPath, db: DbSession, actor: Viewer) -> dict[str, Any]:
    return _eval_view(service.get_evaluation(db, _case(db, uid), euid), actor)


@router.get("/cases/{uid}/evaluations/{euid}/snapshot", summary="The frozen evidence values (editors and above)")
def snapshot(uid: UidPath, euid: UidPath, db: DbSession, _a: Editor) -> dict[str, Any]:
    e = service.get_evaluation(db, _case(db, uid), euid)
    return {"evaluation_uid": e.evaluation_uid, "snapshot_hash": e.snapshot_hash, "snapshot": e.snapshot}


@router.post(
    "/cases/{uid}/evaluations/{euid}/replay",
    responses=ERR,
    summary="Recompute from the stored snapshot and compare hashes",
)
def replay(uid: UidPath, euid: UidPath, db: DbSession, request: Request, _a: Editor) -> dict[str, Any]:
    c = _case(db, uid)
    return service.replay(db, service.get_evaluation(db, c, euid), c, _limits(request))


@router.post(
    "/evaluations/compare",
    responses=ERR,
    summary="Put several evaluations side by side (any cases), with comparability notes",
)
def compare_evaluations(body: CompareBody, db: DbSession, actor: Viewer) -> dict[str, Any]:
    uids = list(dict.fromkeys(body.evaluation_uids))
    if len(uids) < 2:
        raise DomainValidationError("Give at least two distinct evaluations.")
    rows = {
        e.evaluation_uid: e
        for e in db.scalars(select(DecisionEvaluation).where(DecisionEvaluation.evaluation_uid.in_(uids)))
    }
    for u in uids:
        if u not in rows:
            raise NotFoundError(f"Evaluation {u} not found.")
    ordered = [rows[u] for u in uids]
    notes: list[dict[str, str]] = []
    if len({e.metric_registry_version for e in ordered}) > 1 or len({e.algorithm_version for e in ordered}) > 1:
        notes.append(
            {
                "code": "VERSIONS_DIFFER",
                "text": "The evaluations used different algorithm or metric-registry versions; metric definitions may differ.",
            }
        )
    scopes = [(e.result.get("baseline") or {}).get("scope") for e in ordered]
    if any(s != scopes[0] for s in scopes):
        notes.append(
            {
                "code": "SCOPES_DIFFER",
                "text": "The baselines answer different requirements (product, destination, quantity, unit or currency); their scope-dependent metrics are not like-for-like.",
            }
        )
    out = []
    for e in ordered:
        res = e.result if actor.role >= Role.EDITOR else compare.cost_hidden(e.result)
        b = res.get("baseline") or {}
        out.append({**_eval_summary(e), "baseline": {"key": b.get("key"), "label": b.get("label"), "scope": b.get("scope")}, "comparisons": [{"plan_key": c["plan_key"], "label": c["label"], "classification": c["classification"], "counts": c["counts"]} for c in res.get("comparisons", [])]})  # fmt: skip
    return {
        "evaluations": out,
        "notes": notes,
        "cost_redacted": actor.role < Role.EDITOR,
        "statement": STATEMENTS["tradeoff"],
    }


@router.post("/cases/{uid}/reviewer", responses=ERR, summary="Assign the reviewer (reviewers and above)")
def reviewer(uid: UidPath, body: ReviewerBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = service.assign_reviewer(db, _case(db, uid), body.reviewer, actor, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.reviewer_assigned",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"reviewer": body.reviewer},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.post(
    "/cases/{uid}/notes", status_code=201, responses=ERR, summary="Add a comment, concern or assumption (kept for ever)"
)
def add_note(uid: UidPath, body: NoteBody, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = _case(db, uid)
        e = service.note(db, c, body.kind, body.text, actor.name, _limits(request), body.alternative_key)
        audit.record(
            db,
            actor,
            f"decision.case.{body.kind.lower()}",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={},
            request_id=request_id_of(request),
        )
        return _evt(e)


@router.post("/cases/{uid}/submit", responses=ERR, summary="Submit an evaluated case for review")
def submit(uid: UidPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = service.submit(db, _case(db, uid), actor.name, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.submitted",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"evaluation": c.current_evaluation_uid},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.post(
    "/cases/{uid}/return", responses=ERR, summary="Return a case under review for changes (assigned reviewer or admin)"
)
def return_case(uid: UidPath, body: ReturnBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = service.return_for_changes(db, _case(db, uid), body.text, actor, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.returned",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.post(
    "/cases/{uid}/decision", responses=ERR, summary="Approve (naming one alternative) or reject a case under review"
)
def decide(uid: UidPath, body: DecisionBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = service.decide(
            db, _case(db, uid), body.decision, body.rationale, body.alternative_key, actor, _limits(request)
        )
        audit.record(db, actor, f"decision.case.{body.decision.lower()}", target_type="DECISION_CASE", target_id=c.id, details={"alternative": body.alternative_key, "evaluation": c.current_evaluation_uid}, request_id=request_id_of(request))  # fmt: skip
        return _case_view(db, c)


@router.post("/cases/{uid}/supersede", responses=ERR, summary="Mark a decided case as superseded (reviewers and above)")
def supersede(uid: UidPath, body: SupersedeBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        c = service.supersede(db, _case(db, uid), body.text, body.superseded_by, actor.name, _limits(request))
        audit.record(
            db,
            actor,
            "decision.case.superseded",
            target_type="DECISION_CASE",
            target_id=c.id,
            details={"by": body.superseded_by},
            request_id=request_id_of(request),
        )
        return _case_view(db, c)


@router.get("/cases/{uid}/events", summary="The full trail of a case: transitions, assignments, notes and decisions")
def events(
    uid: UidPath, db: DbSession, _a: Viewer, limit: Lim = 50, offset: Off = 0, kind: str | None = None
) -> dict[str, Any]:
    c = _case(db, uid)
    st = select(DecisionEvent).where(DecisionEvent.case_id == c.id)
    if kind:
        st = st.where(DecisionEvent.kind == kind.upper())
    total = db.scalar(select(func.count()).select_from(st.subquery()))
    rows = db.scalars(st.order_by(DecisionEvent.id.asc()).limit(limit).offset(offset))
    return {"items": [_evt(e) for e in rows], "total": total, "limit": limit, "offset": offset}
