"""Data Quality API: overview, rules, findings and remediation, evaluation runs, scores, reports, lineage."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, Role, require
from app.core.errors import DomainValidationError, ForbiddenError, NotFoundError, WriteDisabledError
from app.db.tenancy import request_session_factory
from app.models.enums import EntityKind, EvalScope, EvalStatus, FindingOrigin, Severity
from app.models.quality import QualityEvalRun, QualityRuleSetting, QualityScore
from app.models.types import utcnow
from app.quality import engine, findings, hooks, lineage, remediation, reports, service
from app.quality.framework import catalog, catalog_hash
from app.quality.scoring import FORMULA_VERSION
from app.registry import audit
from app.registry.audit import request_id_of
from app.registry.config import QualityConfig, config_hash, quality_config
from app.schemas.common import ErrorResponse
from app.schemas.quality import (
    AssignBody,
    BulkRemediateBody,
    FindingDetail,
    FindingEventOut,
    FindingOut,
    FindingPage,
    RemediateBody,
    RuleSettingBody,
    RunCreate,
    RunOut,
    RunPage,
    TransitionBody,
)

router = APIRouter(prefix="/quality", tags=["data quality"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Admin = Annotated[Actor, Depends(require(Role.ADMIN))]
Lim = Annotated[int, Query(ge=1, le=100)]
Off = Annotated[int, Query(ge=0, le=1_000_000)]
RuleId = Annotated[str, Path(pattern=r"^QR\d{3}$")]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Quality write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _qcfg(request: Request) -> QualityConfig:
    return quality_config(request.app.state.settings.quality_config_path)


def _kind(value: str) -> EntityKind:
    try:
        return EntityKind(value.upper())
    except ValueError:
        raise DomainValidationError("Unknown entity type.") from None


EntityKindPath = Annotated[str, Path(max_length=20)]


@router.get("/overview", summary="Network quality overview (counts, scores, coverage)")
def overview(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    return service.overview(db, _qcfg(request))


@router.get("/rules", summary="Rule catalog with current enabled state")
def rules(db: DbSession, _a: Viewer) -> dict[str, Any]:
    en = engine.enabled_map(db)
    overrides = {r.rule_id: r for r in db.scalars(select(QualityRuleSetting))}
    items = []
    for rid, r in catalog().items():
        o = overrides.get(rid)
        items.append({"rule_id": rid, "name": r.name, "version": r.version, "description": r.description, "dimension": r.dimension.value, "severity": r.severity.value, "entity_types": [k.value for k in r.applies_to], "enabled": en[rid], "default_enabled": r.default_enabled, "requires_review": r.requires_review, "remediation": r.remediation, "fix_action": r.fix_action, "params": list(r.params), "override": {"reason": o.reason, "updated_by": o.updated_by, "updated_at": o.updated_at} if o else None})  # fmt: skip
    return {"items": items, "total": len(items), "formula_version": FORMULA_VERSION}


@router.patch("/rules/{rule_id}", summary="Enable or disable a catalog rule (admin, audited)", responses=ERR)
def set_rule(rule_id: RuleId, body: RuleSettingBody, db: DbSession, request: Request, actor: Admin) -> dict[str, Any]:
    _writes(request)
    spec = catalog().get(rule_id)
    if spec is None:
        raise NotFoundError("Rule not found.")
    with db.begin():
        row = db.scalar(select(QualityRuleSetting).where(QualityRuleSetting.rule_id == rule_id))
        old = row.enabled if row else spec.default_enabled
        if row is None:
            db.add(
                QualityRuleSetting(
                    rule_id=rule_id,
                    enabled=body.enabled,
                    reason=body.reason.strip(),
                    updated_by=actor.name,
                    updated_at=utcnow(),
                    revision=1,
                )
            )
        else:
            row.enabled, row.reason, row.updated_by, row.updated_at, row.revision = (
                body.enabled,
                body.reason.strip(),
                actor.name,
                utcnow(),
                row.revision + 1,
            )
        audit.record(
            db,
            actor,
            "quality.rule.setting",
            target_type="QUALITY_RULE",
            target_id=rule_id,
            details={"from": old, "to": body.enabled},
            request_id=request_id_of(request),
        )
    return {
        "rule_id": rule_id,
        "enabled": body.enabled,
        "previous": old,
        "note": "Takes effect at the next evaluation; existing findings and scores are not rewritten.",
    }


# --------------------------------------------------------------------------------------------------- findings


@router.get("/findings", response_model=FindingPage, summary="Filterable, paginated findings", responses=ERR)
def list_findings(
    db: DbSession, _a: Viewer, limit: Lim = 25, offset: Off = 0,
    entity_type: EntityKind | None = None, entity_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    severity: Severity | None = None, min_severity: Severity | None = None, dimension: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[str | None, Query(max_length=24, description="A status, or OPEN_ANY / CLOSED_ANY / EXCEPTION_ANY.")] = None,
    rule_id: Annotated[str | None, Query(pattern=r"^[A-Z]{2}\d{3}$")] = None,
    source_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None, origin: FindingOrigin | None = None,
    batch_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None, q: Annotated[str | None, Query(max_length=80)] = None,
    assigned_to: Annotated[str | None, Query(max_length=64)] = None, requires_review: bool | None = None,
    sort: Annotated[str, Query(pattern="^(severity|newest|oldest|entity|rule)$")] = "severity",
    include_disabled: bool = False,
) -> dict[str, Any]:  # fmt: skip
    from app.models.enums import ResolutionStatus

    if status and status not in service.STATUS_GROUPS:
        try:
            ResolutionStatus(status)
        except ValueError:
            raise DomainValidationError("Unknown status filter.") from None
    return service.list_findings(db, entity_type=entity_type, entity_id=entity_id, severity=severity, min_severity=min_severity, dimension=dimension, status=status, rule_id=rule_id, source_id=source_id, origin=origin, batch_id=batch_id, q=q, assigned_to=assigned_to, requires_review=requires_review, sort=sort, limit=limit, offset=offset, include_disabled=include_disabled)  # fmt: skip


@router.get(
    "/findings/{finding_id}", response_model=FindingDetail, responses=ERR, summary="Finding detail with history"
)
def finding_detail(finding_id: IdPath, db: DbSession, _a: Viewer) -> FindingDetail:
    f = findings.get_finding(db, finding_id)
    names, srcs = service._names(db, [f])
    spec = catalog().get(f.rule_id)
    rule = (
        {
            "rule_id": spec.rule_id,
            "name": spec.name,
            "version": spec.version,
            "description": spec.description,
            "remediation": spec.remediation,
            "fix_action": spec.fix_action,
        }
        if spec
        else None
    )
    evs = [
        FindingEventOut(
            id=e.id,
            occurred_at=e.occurred_at,
            actor=e.actor,
            action=e.action,
            from_status=e.from_status,
            to_status=e.to_status,
            note=e.note,
            details=e.details,
            run_id=e.run_id,
        )
        for e in findings.events_of(db, f.id)
    ]
    return FindingDetail(finding=FindingOut(**service.finding_out(f, names, srcs)), rule=rule, events=evs)


@router.post(
    "/findings/{finding_id}/transition",
    response_model=FindingOut,
    responses=ERR,
    summary="Acknowledge, resolve, accept an exception or reopen",
)
def transition(finding_id: IdPath, body: TransitionBody, db: DbSession, request: Request, actor: Editor) -> FindingOut:
    _writes(request)
    with db.begin():
        f = findings.transition(
            db, actor, finding_id, body.action, body.note, body.expected_version, request_id_of(request)
        )
        names, srcs = service._names(db, [f])
        return FindingOut(**service.finding_out(f, names, srcs))


@router.post(
    "/findings/{finding_id}/assign", response_model=FindingOut, responses=ERR, summary="Assign or unassign a finding"
)
def assign(finding_id: IdPath, body: AssignBody, db: DbSession, request: Request, actor: Reviewer) -> FindingOut:
    _writes(request)
    with db.begin():
        f = findings.assign(db, actor, finding_id, body.assignee, body.expected_version, request_id_of(request))
        return FindingOut(**service.finding_out(f))


@router.post("/findings/{finding_id}/remediate", responses=ERR, summary="Apply the finding's safe automated fix")
def remediate(
    finding_id: IdPath, body: RemediateBody, db: DbSession, request: Request, actor: Reviewer
) -> dict[str, Any]:
    _writes(request)
    if not body.confirm:
        raise DomainValidationError("Set confirm=true to apply the automated fix.")
    with db.begin():
        res, key = remediation.apply_fix(db, actor, finding_id, body.expected_version, request_id_of(request))
    hooks.reevaluate_entity(request, key[0], key[1], actor.name)
    return {**res, "re_evaluated": bool(request.app.state.settings.quality_auto_reevaluate)}


@router.post(
    "/findings/bulk-remediate", responses=ERR, summary="Apply safe fixes to up to 25 findings (per-item results)"
)
def bulk_remediate(body: BulkRemediateBody, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    if not body.confirm:
        raise DomainValidationError("Set confirm=true to apply the automated fixes.")
    with db.begin():
        results, touched = remediation.apply_bulk(
            db, actor, [(i.finding_id, i.expected_version) for i in body.items], request_id_of(request)
        )
        audit.record(
            db,
            actor,
            "quality.remediation.bulk",
            target_type="FINDING",
            target_id=None,
            details={"requested": len(body.items), "applied": sum(1 for r in results if r["status"] == "APPLIED")},
            request_id=request_id_of(request),
        )
    for kind, eid in sorted(touched, key=lambda t: (t[0].value, t[1])):
        hooks.reevaluate_entity(request, kind, eid, actor.name)
    return {
        "results": results,
        "applied": sum(1 for r in results if r["status"] == "APPLIED"),
        "skipped": sum(1 for r in results if r["status"] != "APPLIED"),
    }


# ------------------------------------------------------------------------------------------------------ runs


def _scope_ref(body: RunCreate) -> dict[str, Any]:
    if body.scope is EvalScope.ENTITY:
        return {"entity_type": body.entity_type.value if body.entity_type else None, "entity_id": body.entity_id}
    if body.scope is EvalScope.BATCH:
        return {"batch_id": body.batch_id}
    if body.scope is EvalScope.SOURCE:
        return {"source_id": body.source_id}
    if body.scope is EvalScope.RELATIONSHIPS:
        return {"refs": body.refs}
    return {}


@router.post(
    "/runs",
    status_code=202,
    response_model=RunOut,
    responses={**ERR, 202: {"model": RunOut}},
    summary="Queue a bounded evaluation run",
)
def create_run(body: RunCreate, tasks: BackgroundTasks, db: DbSession, request: Request, actor: Editor) -> RunOut:
    _writes(request)
    if body.scope in (EvalScope.NETWORK, EvalScope.SOURCE) and actor.role < Role.REVIEWER:
        raise ForbiddenError("Network and source evaluations require the reviewer role.")
    s = request.app.state.settings
    with db.begin():
        run = engine.create_run(db, body.scope, _scope_ref(body), actor.name, "API", engine.limits_from(s))
        audit.record(
            db,
            actor,
            "quality.run.queued",
            target_type="QUALITY_RUN",
            target_id=run.id,
            details={"scope": body.scope.value, "ref": run.scope_ref},
            request_id=request_id_of(request),
        )
        out = RunOut(**service.run_out(run))
    tasks.add_task(engine.execute_run, request_session_factory(request), out.id, _qcfg(request), engine.limits_from(s))
    return out


@router.get("/runs", response_model=RunPage, summary="Evaluation run history")
def list_runs(
    db: DbSession,
    _a: Viewer,
    limit: Lim = 20,
    offset: Off = 0,
    scope: EvalScope | None = None,
    status: EvalStatus | None = None,
) -> dict[str, Any]:
    return service.list_runs(db, scope, status, limit, offset)


@router.get("/runs/{run_id}", response_model=RunOut, responses=ERR, summary="One evaluation run")
def get_run(run_id: IdPath, db: DbSession, _a: Viewer) -> RunOut:
    r = db.get(QualityEvalRun, run_id)
    if r is None:
        raise NotFoundError("Run not found.")
    return RunOut(**service.run_out(r))


# ---------------------------------------------------------------------------------- scores, entities, reports


@router.get("/scores/{entity_type}/{entity_id}", responses=ERR, summary="Latest score snapshot for a record")
def get_score(entity_type: EntityKindPath, entity_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    kind = _kind(entity_type)
    sc = db.scalar(
        select(QualityScore)
        .where(QualityScore.subject_type == kind, QualityScore.subject_id == entity_id)
        .order_by(QualityScore.id.desc())
        .limit(1)
    )
    return service.score_out(sc)


@router.get("/entities/{entity_type}/{entity_id}", responses=ERR, summary="Quality detail for one record")
def entity_quality(entity_type: EntityKindPath, entity_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return service.entity_quality(db, _kind(entity_type), entity_id)


@router.get("/batches/{batch_id}", responses=ERR, summary="Import batch quality report (every row accounted for)")
def batch_report(
    batch_id: IdPath,
    db: DbSession,
    actor: Viewer,
    classification: Annotated[str | None, Query(pattern="^[A-Z_]{4,30}$")] = None,
    limit: Lim = 50,
    offset: Off = 0,
) -> dict[str, Any]:
    if classification and classification not in reports.CLASSES:
        raise DomainValidationError("Unknown classification.")
    return reports.batch_report(db, batch_id, actor.role, classification, limit, offset)


@router.get("/sources", responses=ERR, summary="Per-source quality indicators")
def sources(
    db: DbSession, request: Request, _a: Viewer, source_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None
) -> dict[str, Any]:
    items = reports.source_reports(db, _qcfg(request), source_id)
    return {"items": items, "total": len(items)}


@router.get("/analytics", responses=ERR, summary="Descriptive quality analytics with defined denominators")
def analytics(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    return service.analytics(db, _qcfg(request))


@router.get("/lineage/{entity_type}/{entity_id}", responses=ERR, summary="Provenance layers for a record")
def get_lineage(entity_type: EntityKindPath, entity_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    return lineage.lineage(db, _kind(entity_type), entity_id)


@router.get("/config", summary="Active catalog identity (for comparing scores)")
def config(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    q = _qcfg(request)
    ch = config_hash(q)
    return {
        "catalog_hash": catalog_hash(engine.enabled_map(db), ch, FORMULA_VERSION),
        "formula_version": FORMULA_VERSION,
        "config_version": q.version,
        "config_hash": ch,
        "thresholds": {
            "fresh_days": q.fresh_days,
            "stale_days": q.stale_days,
            "low_score_threshold": q.low_score_threshold,
        },
    }


__all__ = ["router", "JSONResponse"]
