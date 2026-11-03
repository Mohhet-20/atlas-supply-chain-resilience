"""Supplier Risk Intelligence API: assessments and history, evidence, dependency, runs, comparison, reviews, alerts."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Path, Query, Request, Response
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import (
    DomainValidationError,
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    VersionConflictError,
    WriteDisabledError,
)
from app.db.tenancy import request_session_factory
from app.models.enums import (
    EntityStatus,
    EvalStatus,
    FactorState,
    ReliabilityBand,
    RiskAlertStatus,
    RiskAlertType,
    RiskCategory,
    RiskClass,
    RiskRunScope,
    Severity,
)
from app.models.risk import RiskAlert, RiskAssessment, RiskRun
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.registry import audit
from app.registry.audit import request_id_of
from app.risk import reports, reviews, runs, service
from app.risk.methodology import (
    CATEGORY_DOCS,
    CORRELATION_GROUPS,
    RELIABILITY_ROLE_CATEGORIES,
    RiskConfig,
    config_hash,
    risk_config,
)
from app.schemas.common import ErrorResponse
from app.schemas.supplier_risk import (
    AlertOut,
    AlertPage,
    AlertTransition,
    AssessmentDetail,
    HistoryPage,
    ReviewCreate,
    ReviewOut,
    RunCreate,
    RunOut,
    RunPage,
    SupplierPage,
    SupplierRiskOut,
)

router = APIRouter(prefix="/risk", tags=["supplier risk"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Lim = Annotated[int, Query(ge=1, le=100)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Risk write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _cfg(request: Request) -> RiskConfig:
    return risk_config(request.app.state.settings.risk_config_path)


def _brief(s: Supplier) -> dict[str, Any]:
    return {"id": s.id, "supplier_code": s.supplier_code, "name": s.name, "country_or_region": s.country_or_region, "category": s.category,
            "status": s.status.value, "provenance": s.provenance.value}  # fmt: skip


def _supplier_view(db, s: Supplier, now) -> dict[str, Any]:
    a = service.current_assessment(db, s.id)
    revs = [service.review_out(r) for r in service.reviews_for(db, s.id, 20)]
    alerts = [service.alert_out(x, s.name) for x in service.alerts_for(db, s.id)]
    ov = service.active_override(db, s.id, now, a.id if a else None)
    if a is None:
        return {"supplier": _brief(s), "assessed": False, "assessment": None, "override": ov, "reviews": revs, "alerts": alerts,
                "unassessed_note": "This supplier has not been assessed. No risk level is implied; run an assessment."}  # fmt: skip
    detail = service.assessment_out(a) | {"factors": service.load_factors(db, a.id)}
    return {
        "supplier": _brief(s),
        "assessed": True,
        "assessment": detail,
        "override": ov,
        "reviews": revs,
        "alerts": alerts,
        "unassessed_note": None,
    }


# ---------------------------------------------------------------------------------------------------- reference


@router.get("/methodology", summary="Current methodology: categories, weights, thresholds, data availability")
def methodology(request: Request, _a: Viewer) -> dict[str, Any]:
    cfg = _cfg(request)
    cats = []
    for cat, d in CATEGORY_DOCS.items():
        cats.append({
            "category": cat.value, "label": d.label, "role": d.role, "weight": 0.0 if cat in RELIABILITY_ROLE_CATEGORIES else cfg.weights[cat.value],
            "data_available": d.data_available, "data_dependency": d.data_dependency, "required_inputs": list(d.required_inputs), "method": d.method,
            "evidence_requirements": d.evidence_requirements, "freshness_policy": d.freshness_policy, "uncertainty_treatment": d.uncertainty_treatment,
            "unavailable_when": d.unavailable_when, "limitations": list(d.limitations), "score_range": list(d.score_range),
            "correlation_group": next((g for g, m in CORRELATION_GROUPS.items() if cat in m), None),
        })  # fmt: skip
    return {
        "version": cfg.version, "hash": config_hash(cfg), "kind": "deterministic rule-based indicator (not a trained or validated predictive model)",
        "formula": "score = sum(W_i x s_i) / sum(W_i) over available risk-role factors; correlated factors use the group's highest member",
        "scale": {"range": [0, 100], "direction": "higher means greater supported risk"},
        "thresholds": cfg.thresholds.model_dump(), "min_coverage_for_score": cfg.min_coverage_for_score,
        "min_coverage_for_classification": cfg.min_coverage_for_classification, "min_scored_factors": cfg.min_scored_factors,
        "reliability_bands": {"high": cfg.reliability_high, "moderate": cfg.reliability_moderate},
        "freshness": cfg.freshness.model_dump(), "categories": cats, "parameters_editable_via_api": False,
        "max_possible_coverage": round(sum(w for k, w in cfg.weights.items() if CATEGORY_DOCS[RiskCategory(k)].data_available), 4),
        "disclaimer": service.INTERPRETATION,
    }  # fmt: skip


@router.get("/overview", summary="Portfolio overview (exact counts)")
def overview(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    return reports.overview(db, _cfg(request))


@router.get("/geography", summary="Suppliers and facilities by registry region label")
def geography(db: DbSession, _a: Viewer, region: Annotated[str | None, Query(max_length=100)] = None) -> dict[str, Any]:
    return reports.geography(db, region)


# ----------------------------------------------------------------------------------------------------- suppliers


@router.get(
    "/suppliers",
    response_model=SupplierPage,
    responses=ERR,
    summary="Suppliers with their current assessment (filter, sort, paginate)",
)
def list_suppliers(
    db: DbSession, request: Request, _a: Viewer, limit: Lim = 25, offset: Off = 0,
    q: Annotated[str | None, Query(max_length=80)] = None, supplier_status: EntityStatus | None = None,
    region: Annotated[str | None, Query(max_length=100)] = None, classification: RiskClass | None = None,
    assessment_status: Annotated[str | None, Query(pattern="^(ASSESSED|PARTIAL|INSUFFICIENT_EVIDENCE|NOT_ASSESSED)$")] = None,
    review_state: Annotated[str | None, Query(pattern="^(UNREVIEWED|ACKNOWLEDGED|OVERRIDDEN)$")] = None, requires_review: bool | None = None,
    min_coverage: Annotated[float | None, Query(ge=0, le=1)] = None, max_coverage: Annotated[float | None, Query(ge=0, le=1)] = None,
    reliability_band: ReliabilityBand | None = None, category: RiskCategory | None = None, category_state: FactorState | None = None,
    min_category_score: Annotated[float | None, Query(ge=0, le=100)] = None,
    sort: Annotated[str, Query(pattern="^(score_desc|score_asc|name|coverage_desc|reliability_asc|computed_desc|code)$")] = "score_desc",
) -> dict[str, Any]:  # fmt: skip
    if min_coverage is not None and max_coverage is not None and min_coverage > max_coverage:
        raise DomainValidationError("min_coverage must not exceed max_coverage.")
    return reports.list_suppliers(
        db, _cfg(request), limit=limit, offset=offset, sort=sort, q=q, supplier_status=supplier_status, region=region,
        classification=classification, assessment_status=assessment_status, review_state=review_state, requires_review=requires_review,
        min_coverage=min_coverage, max_coverage=max_coverage, reliability_band=reliability_band, category=category.value if category else None,
        category_state=category_state, min_category_score=min_category_score,
    )  # fmt: skip


@router.get(
    "/suppliers/{supplier_id}",
    response_model=SupplierRiskOut,
    responses=ERR,
    summary="Supplier risk summary: current assessment, factors, evidence, override",
)
def supplier_summary(supplier_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return _supplier_view(db, service.get_supplier(db, supplier_id), utcnow())


@router.get(
    "/suppliers/{supplier_id}/assessments",
    response_model=HistoryPage,
    responses=ERR,
    summary="Assessment history (newest first)",
)
def supplier_history(
    supplier_id: IdPath, db: DbSession, _a: Viewer, limit: Lim = 20, offset: Off = 0
) -> dict[str, Any]:
    service.get_supplier(db, supplier_id)
    rows, total = service.history(db, supplier_id, limit, offset)
    return {"items": service.history_changes(rows), "total": total, "limit": limit, "offset": offset,
            "note": "Each row is an immutable assessment. score_change is shown only against the previous row; change_comparable is false when the methodology differs."}  # fmt: skip


@router.get(
    "/suppliers/{supplier_id}/assessments/compare",
    responses=ERR,
    summary="Factor-level difference between two assessments of one supplier",
)
def compare_two(supplier_id: IdPath, db: DbSession, _a: Viewer, from_id: Annotated[int, Query(alias="from", ge=1, le=2**31 - 1)],
                to_id: Annotated[int, Query(alias="to", ge=1, le=2**31 - 1)]) -> dict[str, Any]:  # fmt: skip
    service.get_supplier(db, supplier_id)
    a, b = db.get(RiskAssessment, from_id), db.get(RiskAssessment, to_id)
    if a is None or b is None or a.supplier_id != supplier_id or b.supplier_id != supplier_id:
        raise NotFoundError("Assessment not found for this supplier.")
    return service.compare_assessments(db, a, b)


@router.get(
    "/assessments/{assessment_id}",
    response_model=AssessmentDetail,
    responses=ERR,
    summary="One assessment with factors and evidence",
)
def assessment_detail(assessment_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    a = db.get(RiskAssessment, assessment_id)
    if a is None:
        raise NotFoundError("Assessment not found.")
    return service.assessment_out(a) | {"factors": service.load_factors(db, a.id)}


@router.get(
    "/suppliers/{supplier_id}/dependencies", responses=ERR, summary="Live dependency and concentration indicators"
)
def dependencies(supplier_id: IdPath, db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    return reports.dependency_view(db, supplier_id, _cfg(request))


@router.get("/compare", responses=ERR, summary="Compare 2-6 suppliers (ranking shown only when scores are comparable)")
def compare(
    db: DbSession, request: Request, _a: Viewer, ids: Annotated[list[int], Query(min_length=2, max_length=6)]
) -> dict[str, Any]:
    if any(i < 1 or i > 2**31 - 1 for i in ids):
        raise DomainValidationError("Supplier ids must be positive integers.")
    out = reports.compare(db, _cfg(request), ids)
    out["suppliers"] = [{**x} for x in out["suppliers"]]
    return out


# -------------------------------------------------------------------------------------------------------- runs


def _run_scope_ref(body: RunCreate) -> dict[str, Any]:
    if body.scope is RiskRunScope.SUPPLIER:
        if body.supplier_id is None:
            raise DomainValidationError("supplier scope needs supplier_id.")
        return {"supplier_id": body.supplier_id}
    if body.scope is RiskRunScope.SUPPLIERS:
        if not body.supplier_ids:
            raise DomainValidationError("suppliers scope needs supplier_ids.")
        return {"supplier_ids": body.supplier_ids}
    if body.supplier_id is not None or body.supplier_ids:
        raise DomainValidationError("network scope takes no supplier ids.")
    return {}


@router.post(
    "/runs",
    status_code=202,
    response_model=RunOut,
    responses={**ERR, 202: {"model": RunOut}},
    summary="Queue a bounded assessment run (idempotent with a key)",
)
def create_run(body: RunCreate, tasks: BackgroundTasks, response: Response, db: DbSession, request: Request, actor: Editor,
               idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")] = None) -> dict[str, Any]:  # fmt: skip
    _writes(request)
    if body.scope is RiskRunScope.NETWORK and actor.role < Role.REVIEWER:
        raise ForbiddenError("Network-wide assessment runs require the reviewer role.")
    key = idempotency_key or body.idempotency_key
    s = request.app.state.settings
    limits = runs.limits_from(s)
    with db.begin():
        run, created = runs.create_run(
            db, body.scope, _run_scope_ref(body), actor.name, reason=body.reason, idempotency_key=key, limits=limits
        )
        if created:
            audit.record(
                db,
                actor,
                "risk.run.queued",
                target_type="RISK_RUN",
                target_id=run.id,
                details={"scope": body.scope.value, "ref": run.scope_ref},
                request_id=request_id_of(request),
            )
        out = service.run_out(run)
    if created:
        tasks.add_task(runs.execute_run, request_session_factory(request), out["id"], _cfg(request), limits)
    else:
        response.headers["Idempotent-Replayed"] = "true"
    return out


@router.get("/runs", response_model=RunPage, responses=ERR, summary="Assessment run history")
def list_runs(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 20,
    offset: Off = 0,
    scope: RiskRunScope | None = None,
    status: EvalStatus | None = None,
) -> dict[str, Any]:
    q = select(RiskRun)
    if scope:
        q = q.where(RiskRun.scope_type == scope)
    if status:
        q = q.where(RiskRun.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(RiskRun.id.desc()).limit(limit).offset(offset))
    return {"items": [service.run_out(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get(
    "/runs/{run_id}", response_model=RunOut, responses=ERR, summary="One assessment run with its recorded outcomes"
)
def get_run(run_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    r = db.get(RiskRun, run_id)
    if r is None:
        raise NotFoundError("Run not found.")
    return service.run_out(r)


@router.post("/suppliers/{supplier_id}/assess", responses=ERR, summary="Assess one supplier now (bounded, synchronous)")
def assess_supplier(supplier_id: IdPath, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    s = request.app.state.settings
    service.get_supplier(db, supplier_id)
    db.rollback()  # end this read transaction: the run writes through its own session and SQLite allows one writer
    run = runs.assess_now(
        request_session_factory(request),
        RiskRunScope.SUPPLIER,
        {"supplier_id": supplier_id},
        actor.name,
        _cfg(request),
        limits=runs.limits_from(s),
    )
    db.expire_all()
    with db.begin():
        audit.record(
            db,
            actor,
            "risk.assess",
            target_type="SUPPLIER",
            target_id=supplier_id,
            details={"run_id": run.id, "status": run.status.value},
            request_id=request_id_of(request),
        )
    sup = service.get_supplier(db, supplier_id)
    return {"run": service.run_out(db.get(RiskRun, run.id)), "result": _supplier_view(db, sup, utcnow())}


# ----------------------------------------------------------------------------------------------------- reviews


@router.get(
    "/suppliers/{supplier_id}/reviews",
    response_model=list[ReviewOut],
    responses=ERR,
    summary="Review decisions for a supplier (newest first)",
)
def list_reviews(supplier_id: IdPath, db: DbSession, _a: Viewer) -> list[dict[str, Any]]:
    service.get_supplier(db, supplier_id)
    return [service.review_out(r) for r in service.reviews_for(db, supplier_id, 100)]


@router.post(
    "/suppliers/{supplier_id}/reviews",
    status_code=201,
    response_model=ReviewOut,
    responses=ERR,
    summary="Acknowledge, note, override, request evidence or mark a factor for follow-up",
)
def create_review(
    supplier_id: IdPath, body: ReviewCreate, db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        r = reviews.add_review(
            db, actor, supplier_id, assessment_id=body.assessment_id, action=body.action, note=body.note, factor_category=body.factor_category,
            override_classification=body.override_classification, expires_at=body.override_expires_at, target_review_id=body.target_review_id,
            request_id=request_id_of(request),
        )  # fmt: skip
        audit.record(db, actor, f"risk.review.{body.action.value.lower()}", target_type="RISK_ASSESSMENT", target_id=body.assessment_id,
                     details={"supplier_id": supplier_id, "category": body.factor_category, "override": body.override_classification.value if body.override_classification else None, "review_id": r.id},
                     request_id=request_id_of(request))  # fmt: skip
        return service.review_out(r)


# ------------------------------------------------------------------------------------------------------ alerts


@router.get("/alerts", response_model=AlertPage, responses=ERR, summary="In-application review queue")
def list_alerts(db: DbSession, _a: Viewer, limit: Lim = 25, offset: Off = 0, status: Annotated[str | None, Query(pattern="^(OPEN|ACKNOWLEDGED|DISMISSED|RESOLVED|ACTIVE)$")] = "ACTIVE",
                alert_type: RiskAlertType | None = None, severity: Severity | None = None, supplier_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None) -> dict[str, Any]:  # fmt: skip
    q = select(RiskAlert, Supplier.name).join(Supplier, Supplier.id == RiskAlert.supplier_id)
    if status == "ACTIVE":
        q = q.where(RiskAlert.status.in_([RiskAlertStatus.OPEN, RiskAlertStatus.ACKNOWLEDGED]))
    elif status:
        q = q.where(RiskAlert.status == RiskAlertStatus(status))
    if alert_type:
        q = q.where(RiskAlert.alert_type == alert_type)
    if severity:
        q = q.where(RiskAlert.severity == severity)
    if supplier_id:
        q = q.where(RiskAlert.supplier_id == supplier_id)
    total = db.scalar(select(func.count()).select_from(q.with_only_columns(RiskAlert.id).subquery())) or 0
    rows = db.execute(q.order_by(RiskAlert.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [service.alert_out(a, n) for a, n in rows], "total": total, "limit": limit, "offset": offset}


ALERT_MOVES = {
    RiskAlertStatus.OPEN: {RiskAlertStatus.ACKNOWLEDGED, RiskAlertStatus.DISMISSED, RiskAlertStatus.RESOLVED},
    RiskAlertStatus.ACKNOWLEDGED: {RiskAlertStatus.DISMISSED, RiskAlertStatus.RESOLVED},
}


@router.post(
    "/alerts/{alert_id}/transition",
    response_model=AlertOut,
    responses=ERR,
    summary="Acknowledge, dismiss (with a note) or resolve an alert",
)
def alert_transition(
    alert_id: IdPath, body: AlertTransition, db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        a = db.get(RiskAlert, alert_id)
        if a is None:
            raise NotFoundError("Alert not found.")
        if a.version != body.version:
            raise VersionConflictError("The alert changed since you loaded it; reload and retry.")
        if body.to not in ALERT_MOVES.get(a.status, set()):
            raise InvalidTransitionError(f"An alert cannot move from {a.status.value} to {body.to.value}.")
        note = (body.note or "").strip() or None
        if body.to is RiskAlertStatus.DISMISSED and (note is None or len(note) < 5):
            raise DomainValidationError("Dismissing an alert needs a reason of at least 5 characters.")
        before = a.status
        a.status, a.updated_at = body.to, utcnow()
        if body.to in (RiskAlertStatus.DISMISSED, RiskAlertStatus.RESOLVED):
            a.resolved_at, a.resolved_by, a.resolution_note = utcnow(), actor.name, note
        audit.record(db, actor, "risk.alert.transition", target_type="RISK_ALERT", target_id=alert_id,
                     details={"from": before.value, "to": body.to.value, "supplier_id": a.supplier_id}, request_id=request_id_of(request))  # fmt: skip
        db.flush()
        name = db.scalar(select(Supplier.name).where(Supplier.id == a.supplier_id))
        return service.alert_out(a, name)
