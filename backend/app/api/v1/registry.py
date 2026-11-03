"""Registry Intelligence API: search, canonical records, duplicate review, merging, quality and audit."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import func, select

from app.api.v1.deps import DbSession
from app.core.auth import Actor, CurrentActor, Role, require, token_table
from app.core.errors import DomainValidationError, NotFoundError, WriteDisabledError
from app.models.enums import (
    AuditOutcome,
    CandidateStatus,
    CandidateTier,
    EntityKind,
    EntityStatus,
    MergeStatus,
    RegistryType,
)
from app.models.registry import AuditEvent, DuplicateCandidate, MergeRecord, ResolutionRun
from app.quality import hooks as quality_hooks
from app.registry import audit, merge, quality, resolution, review, search, service, views
from app.registry.audit import audit_scope, request_id_of
from app.registry.config import config_hash, quality_config, resolution_config
from app.registry.entities import MODELS, get_entity
from app.risk import hooks as risk_hooks
from app.schemas.common import ErrorResponse
from app.schemas.registry import (
    CREATE_MODELS,
    PATCH_MODELS,
    AliasIn,
    CompensateRequest,
    GenerateRequest,
    IdentifierIn,
    MergeRequest,
    QualityRunRequest,
    ReviewDecision,
)

router = APIRouter(prefix="/registry", tags=["registry"])
ERR = {k: {"model": ErrorResponse} for k in (401, 403, 404, 409, 422)}
IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
Viewer = Annotated[Actor, Depends(require(Role.VIEWER))]
Editor = Annotated[Actor, Depends(require(Role.EDITOR))]
Reviewer = Annotated[Actor, Depends(require(Role.REVIEWER))]
Admin = Annotated[Actor, Depends(require(Role.ADMIN))]


class Plural(str, Enum):
    suppliers = "suppliers"
    facilities = "facilities"
    products = "products"


PLURAL = {
    Plural.suppliers: RegistryType.SUPPLIER,
    Plural.facilities: RegistryType.FACILITY,
    Plural.products: RegistryType.PRODUCT,
}
PluralPath = Annotated[Plural, Path(description="suppliers, facilities or products")]


def _writes(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError("Registry write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED).")


def _res_cfg(request: Request):
    cached = getattr(request.app.state, "_res_cfg", None)
    if cached is None:
        cached = request.app.state._res_cfg = resolution_config(request.app.state.settings.resolution_config_path)
    return cached


def _qual_cfg(request: Request):
    cached = getattr(request.app.state, "_qual_cfg", None)
    if cached is None:
        cached = request.app.state._qual_cfg = quality_config(request.app.state.settings.quality_config_path)
    return cached


# ------------------------------------------------------------------ session


@router.get("/me", summary="Current actor and authentication mode")
def me(request: Request, actor: CurrentActor) -> dict[str, Any]:
    return {
        "actor": actor.name,
        "role": actor.role.name.lower(),
        "authenticated": actor.authenticated,
        "auth_required": bool(token_table(request)) or actor.user_id is not None,
        "org_id": actor.org_id,
        "platform_admin": actor.platform_admin,
        "must_change_password": False,
        "writes_enabled": request.app.state.settings.write_api_allowed,
    }


# ----------------------------------------------------------------- overview


@router.get("/overview", responses=ERR, summary="Registry overview computed from stored data")
def overview(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for rtype, model in MODELS.items():
        total = db.scalar(select(func.count()).select_from(model)) or 0
        merged_n = db.scalar(select(func.count()).select_from(model).where(model.merged_into_id.is_not(None))) or 0
        counts[rtype.value] = {"total": total, "live": total - merged_n, "merged": merged_n}
    by_status = dict(
        db.execute(select(DuplicateCandidate.status, func.count()).group_by(DuplicateCandidate.status)).all()
    )
    by_tier_open = dict(
        db.execute(
            select(DuplicateCandidate.tier, func.count())
            .where(
                DuplicateCandidate.status.in_(
                    [CandidateStatus.NEW, CandidateStatus.UNDER_REVIEW, CandidateStatus.REOPENED]
                )
            )
            .group_by(DuplicateCandidate.tier)
        ).all()
    )
    q = quality.summary(db, _qual_cfg(request))
    incomplete = q["issue_counts"].get("MISSING") if q.get("has_data") else None
    recent = db.scalars(
        select(AuditEvent)
        .where(
            AuditEvent.action.like("registry.%")
            | AuditEvent.action.like("merge.%")
            | AuditEvent.action.like("candidate.%")
        )
        .where(audit_scope(_a))
        .order_by(AuditEvent.id.desc())
        .limit(10)
    ).all()
    return {
        "counts": counts,
        "candidates": {
            "by_status": {k.value: v for k, v in by_status.items()},
            "awaiting_review": sum(v for k, v in by_status.items() if k in (CandidateStatus.NEW, CandidateStatus.UNDER_REVIEW, CandidateStatus.REOPENED)),
            "open_by_tier": {k.value: v for k, v in by_tier_open.items()},
            "confirmed_duplicates": by_status.get(CandidateStatus.CONFIRMED_DUPLICATE, 0),
        },
        "merges": {
            "executed": db.scalar(select(func.count()).select_from(MergeRecord).where(MergeRecord.status == MergeStatus.EXECUTED)) or 0,
            "compensated": db.scalar(select(func.count()).select_from(MergeRecord).where(MergeRecord.status == MergeStatus.COMPENSATED)) or 0,
        },
        "quality": {
            "has_data": q.get("has_data", False),
            "average_overall": q["run"]["average_overall"] if q.get("has_data") else None,
            "records_with_missing_required_fields": incomplete if q.get("has_data") else None,
            "low_score_count": q.get("low_score_count"),
            "trend": q.get("trend", []),
            "message": None if q.get("has_data") else q.get("message"),
        },
        "recent_changes": [
            {"id": e.id, "occurred_at": e.occurred_at, "actor": e.actor, "action": e.action, "target_type": e.target_type, "target_id": e.target_id}
            for e in recent
        ],
    }  # fmt: skip


# ------------------------------------------------------------------- search


@router.get("/search", responses=ERR, summary="Unified registry search with explicit match reasons")
def search_registry(
    db: DbSession,
    _a: Viewer,
    q: Annotated[str, Query(min_length=1, max_length=100)],
    type: Annotated[list[RegistryType] | None, Query(description="Entity types; default all.")] = None,
    status: EntityStatus | None = None,
    country: Annotated[str | None, Query(max_length=100)] = None,
    category: Annotated[str | None, Query(max_length=100)] = None,
    include_merged: bool = False,
    fuzzy: bool = True,
    sort: Literal["relevance", "name", "updated"] = "relevance",
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
) -> dict[str, Any]:
    params = search.SearchParams(
        q=q, types=type or list(RegistryType), status=status, country=country, category=category,
        include_merged=include_merged, sort=sort, limit=limit, offset=offset, fuzzy_enabled=fuzzy,
    )  # fmt: skip
    with db.begin():
        return search.search(db, params)


# ----------------------------------------------------------------- candidates


@router.get("/candidates", responses=ERR, summary="List duplicate candidates")
def list_candidates(
    db: DbSession,
    _a: Viewer,
    entity_type: RegistryType | None = None,
    status: CandidateStatus | None = None,
    tier: Annotated[list[CandidateTier] | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=1)] = None,
    entity_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    tiers = tier or [CandidateTier.STRONG, CandidateTier.POSSIBLE]
    with db.begin():
        return review.list_candidates(
            db, entity_type=entity_type, status=status, tiers=tiers, min_score=min_score,
            entity_id=entity_id, limit=limit, offset=offset,
        )  # fmt: skip


@router.post("/candidates/generate", responses=ERR, summary="Generate or refresh duplicate candidates (bounded)")
def generate_candidates(body: GenerateRequest, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    _writes(request)
    cfg = _res_cfg(request)
    with db.begin():
        run = resolution.run_resolution(
            db, body.entity_type, cfg, actor, entity_ids=body.entity_ids, request_id=request_id_of(request)
        )
        return _run_out(run)


def _run_out(run: ResolutionRun) -> dict[str, Any]:
    return {
        "run_uid": run.run_uid, "entity_type": run.entity_type.value, "config_version": run.config_version,
        "config_hash": run.config_hash, "status": run.status.value, "duration_ms": run.duration_ms,
        "entities_considered": run.entities_considered, "blocks": run.blocks, "pairs_compared": run.pairs_compared,
        "candidates_found": run.candidates_found, "candidates_new": run.candidates_new,
        "truncated_reason": run.truncated_reason, "actor": run.actor, "started_at": run.started_at,
    }  # fmt: skip


@router.get("/candidates/compare", responses=ERR, summary="Score any two records without storing a candidate")
def compare_pair(
    request: Request,
    db: DbSession,
    _a: Viewer,
    entity_type: RegistryType,
    left_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
    right_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
) -> dict[str, Any]:
    if left_id == right_id:
        raise DomainValidationError("Choose two different records.")
    cfg = _res_cfg(request)
    with db.begin():
        result = resolution.evaluate_pair(db, entity_type, left_id, right_id, cfg)
        return result.evidence(cfg) | {"persisted": False}


@router.get("/candidates/{candidate_id}", responses=ERR, summary="Candidate with side-by-side comparison")
def get_candidate(candidate_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    with db.begin():
        return review.comparison(db, review.get_candidate(db, candidate_id))


@router.post("/candidates/{candidate_id}/decision", responses=ERR, summary="Record a review decision")
def decide_candidate(
    candidate_id: IdPath, body: ReviewDecision, db: DbSession, request: Request, actor: Reviewer
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        cand = review.get_candidate(db, candidate_id)
        review.decide(
            db, cand, body.decision, actor, reason=body.reason,
            acknowledge_conflict=body.acknowledge_identifier_conflict, request_id=request_id_of(request),
        )  # fmt: skip
        return review.candidate_out(db, cand) | {"history": review.history(db, cand.id)}


@router.get("/resolution/config", responses=ERR, summary="Active matching configuration (versioned)")
def resolution_cfg(request: Request, _a: Viewer) -> dict[str, Any]:
    cfg = _res_cfg(request)
    return {"version": cfg.version, "hash": config_hash(cfg), "config": cfg.model_dump(mode="json")}


@router.get("/resolution/runs", responses=ERR, summary="Recent matching runs")
def resolution_runs(db: DbSession, _a: Viewer, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> dict[str, Any]:
    with db.begin():
        runs = db.scalars(select(ResolutionRun).order_by(ResolutionRun.id.desc()).limit(limit)).all()
        return {"items": [_run_out(r) for r in runs]}


# --------------------------------------------------------------------- merges


@router.post("/merges/preview", responses=ERR, summary="Preview a merge (writes an audit event, changes no records)")
def merge_preview(body: MergeRequest, db: DbSession, request: Request, actor: Reviewer) -> dict[str, Any]:
    with db.begin():
        result = merge.build_preview(
            db, body.entity_type, body.survivor_id, body.merged_id,
            field_decisions=body.field_decisions, relationship_decisions=body.relationship_decisions,
            acknowledge_identifier_conflict=body.acknowledge_identifier_conflict, candidate_id=body.candidate_id,
        )  # fmt: skip
        audit.record(
            db, actor, "merge.previewed", target_type=body.entity_type.value, target_id=body.survivor_id,
            details={"merged_id": body.merged_id, "blockers": [b["code"] for b in result["blockers"]]},
            request_id=request_id_of(request),
        )  # fmt: skip
        return result


@router.post("/merges", status_code=201, responses=ERR, summary="Execute a merge (admin; transactional)")
def merge_execute(body: MergeRequest, db: DbSession, request: Request, actor: Admin) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        record = merge.execute_merge(db, body.entity_type, body, actor, request_id_of(request))
        return merge.merge_out(record, detail=True)


@router.get("/merges", responses=ERR, summary="Merge history")
def list_merges(
    db: DbSession,
    _a: Viewer,
    entity_type: RegistryType | None = None,
    entity_id: Annotated[int | None, Query(ge=1, le=2**31 - 1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    with db.begin():
        stmt = select(MergeRecord)
        if entity_type:
            stmt = stmt.where(MergeRecord.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where((MergeRecord.survivor_id == entity_id) | (MergeRecord.merged_id == entity_id))
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(stmt.order_by(MergeRecord.id.desc()).limit(limit).offset(offset)).all()
        return {"items": [merge.merge_out(m) for m in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/merges/{merge_id}", responses=ERR, summary="One merge with snapshots and decisions")
def get_merge(merge_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    with db.begin():
        record = db.get(MergeRecord, merge_id)
        if record is None:
            raise NotFoundError("Merge not found.")
        return merge.merge_out(record, detail=True)


@router.post("/merges/{merge_id}/compensate", responses=ERR, summary="Compensate (undo) a merge when still safe")
def compensate(
    merge_id: IdPath, body: CompensateRequest, db: DbSession, request: Request, actor: Admin
) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        record = merge.compensate_merge(db, merge_id, body.reason, actor, request_id_of(request))
        return merge.merge_out(record, detail=True)


# -------------------------------------------------------------------- quality


@router.get("/quality/config", responses=ERR, summary="Active quality configuration (versioned)")
def quality_cfg(request: Request, _a: Viewer) -> dict[str, Any]:
    cfg = _qual_cfg(request)
    return {
        "version": cfg.version,
        "hash": config_hash(cfg),
        "config": cfg.model_dump(mode="json"),
        "dimensions": quality.EXPLANATIONS,
    }


@router.post("/quality/run", responses=ERR, summary="Run a quality assessment")
def quality_run(body: QualityRunRequest, db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    with db.begin():
        run = quality.run_assessment(db, _qual_cfg(request), body.entity_types, actor, request_id_of(request))
        return quality.run_out(run)


@router.get("/quality/summary", responses=ERR, summary="Registry-level quality summary")
def quality_summary(db: DbSession, request: Request, _a: Viewer) -> dict[str, Any]:
    with db.begin():
        return quality.summary(db, _qual_cfg(request))


@router.get("/quality/issues", responses=ERR, summary="Actionable quality issues from the latest run")
def quality_issues(
    db: DbSession,
    _a: Viewer,
    entity_type: RegistryType | None = None,
    dimension: Annotated[str | None, Query(pattern=r"^[a-z_]{3,30}$")] = None,
    kind: Annotated[str | None, Query(pattern=r"^[A-Z_]{3,30}$")] = None,
    severity: Annotated[Literal["INFO", "WARNING", "ERROR"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    with db.begin():
        return quality.list_issues(
            db, entity_type=entity_type, dimension=dimension, kind=kind, severity=severity, limit=limit, offset=offset
        )


@router.get("/quality/batch/{batch_id}", responses=ERR, summary="Quality of the records touched by an ingestion batch")
def quality_batch(batch_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    with db.begin():
        return quality.summary_for_batch(db, batch_id)


# ---------------------------------------------------------------------- audit


@router.get("/audit", responses=ERR, summary="Audit events (reviewer)")
def list_audit(
    db: DbSession,
    _a: Reviewer,
    action: Annotated[str | None, Query(max_length=64, pattern=r"^[a-z_.]+$")] = None,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=64)] = None,
    outcome: AuditOutcome | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    stmt = select(AuditEvent).where(audit_scope(_a))
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if target_type:
        stmt = stmt.where(AuditEvent.target_type == target_type)
    if target_id:
        stmt = stmt.where(AuditEvent.target_id == target_id)
    if actor:
        stmt = stmt.where(AuditEvent.actor == actor)
    if outcome:
        stmt = stmt.where(AuditEvent.outcome == outcome)
    with db.begin():
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = db.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit).offset(offset)).all()
        return {
            "items": [
                {
                    "id": e.id, "occurred_at": e.occurred_at, "actor": e.actor, "actor_role": e.actor_role,
                    "authenticated": e.authenticated, "action": e.action, "target_type": e.target_type,
                    "target_id": e.target_id, "outcome": e.outcome.value, "request_id": e.request_id, "details": e.details,
                }
                for e in rows
            ],
            "total": total, "limit": limit, "offset": offset,
        }  # fmt: skip


@router.get("/audit/verify", responses=ERR, summary="Verify the audit hash chain (admin)")
def verify_audit(db: DbSession, _a: Admin) -> dict[str, Any]:
    with db.begin():
        return audit.verify_chain(db)


# ----------------------------------------------------------- canonical records


@router.post("/{entity_type}", status_code=201, responses=ERR, summary="Create a registry record")
def create_record(
    entity_type: PluralPath, body: dict[str, Any], db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    rtype = PLURAL[entity_type]
    payload = CREATE_MODELS[rtype].model_validate(body)
    with db.begin():
        entity = service.create_entity(db, rtype, payload, actor, request_id_of(request))
        out = views.entity_detail(db, rtype, entity)
        new_id = entity.id
    quality_hooks.reevaluate_entity(request, EntityKind(rtype.value), new_id, actor.name)
    if rtype is RegistryType.SUPPLIER:
        risk_hooks.reassess_supplier_edit(request, new_id, actor.name)
    return out


@router.get("/{entity_type}/{entity_id}", responses=ERR, summary="Canonical record with identity, lineage and quality")
def get_record(entity_type: PluralPath, entity_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    rtype = PLURAL[entity_type]
    with db.begin():
        return views.entity_detail(db, rtype, get_entity(db, rtype, entity_id))


@router.patch("/{entity_type}/{entity_id}", responses=ERR, summary="Update a record (optimistic concurrency)")
def patch_record(
    entity_type: PluralPath, entity_id: IdPath, body: dict[str, Any], db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    rtype = PLURAL[entity_type]
    patch = PATCH_MODELS[rtype].model_validate(body)
    with db.begin():
        entity = service.update_entity(db, rtype, entity_id, patch, actor, request_id_of(request))
        out = views.entity_detail(db, rtype, entity)
    quality_hooks.reevaluate_entity(request, EntityKind(rtype.value), entity_id, actor.name)
    if rtype is RegistryType.SUPPLIER:
        risk_hooks.reassess_supplier_edit(request, entity_id, actor.name)
    return out


@router.post("/products/{entity_id}/retire", responses=ERR, summary="Retire a product")
def retire(entity_id: IdPath, body: dict[str, int], db: DbSession, request: Request, actor: Editor) -> dict[str, Any]:
    _writes(request)
    version = _version(body)
    with db.begin():
        e = service.retire_product(db, entity_id, version, actor, request_id_of(request))
        return views.entity_detail(db, RegistryType.PRODUCT, e)


@router.post("/products/{entity_id}/reactivate", responses=ERR, summary="Reactivate a retired product (reviewer)")
def reactivate(
    entity_id: IdPath, body: dict[str, int], db: DbSession, request: Request, actor: Reviewer
) -> dict[str, Any]:
    _writes(request)
    version = _version(body)
    with db.begin():
        e = service.reactivate_product(db, entity_id, version, actor, request_id_of(request))
        return views.entity_detail(db, RegistryType.PRODUCT, e)


def _version(body: dict[str, int]) -> int:
    if (
        set(body) != {"expected_version"}
        or not isinstance(body["expected_version"], int)
        or body["expected_version"] < 1
    ):
        raise DomainValidationError('Body must be {"expected_version": <positive integer>}.')
    return body["expected_version"]


@router.get("/{entity_type}/{entity_id}/history", responses=ERR, summary="Change history of a record")
def record_history(
    entity_type: PluralPath,
    entity_id: IdPath,
    db: DbSession,
    _a: Viewer,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rtype = PLURAL[entity_type]
    with db.begin():
        get_entity(db, rtype, entity_id)
        return {"items": views.history_for(db, rtype, entity_id, limit)}


@router.get("/{entity_type}/{entity_id}/aliases", responses=ERR, summary="Aliases")
def get_aliases(entity_type: PluralPath, entity_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    rtype = PLURAL[entity_type]
    with db.begin():
        get_entity(db, rtype, entity_id)
        return {
            "items": [
                {"id": a.id, "alias": a.alias, "kind": a.kind.value, "origin_merge_id": a.origin_merge_id}
                for a in service.list_aliases(db, rtype, entity_id)
            ]
        }


@router.post("/{entity_type}/{entity_id}/aliases", status_code=201, responses=ERR, summary="Add an alias")
def post_alias(
    entity_type: PluralPath, entity_id: IdPath, body: AliasIn, db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    rtype = PLURAL[entity_type]
    with db.begin():
        a = service.add_alias(db, rtype, entity_id, body, actor, request_id_of(request))
        return {"id": a.id, "alias": a.alias, "kind": a.kind.value}


@router.delete(
    "/{entity_type}/{entity_id}/aliases/{alias_id}", status_code=204, responses=ERR, summary="Remove an alias"
)
def delete_alias(
    entity_type: PluralPath, entity_id: IdPath, alias_id: IdPath, db: DbSession, request: Request, actor: Editor
) -> None:
    _writes(request)
    with db.begin():
        service.remove_alias(db, PLURAL[entity_type], entity_id, alias_id, actor, request_id_of(request))


@router.get("/{entity_type}/{entity_id}/identifiers", responses=ERR, summary="External identifiers (source-scoped)")
def get_identifiers(entity_type: PluralPath, entity_id: IdPath, db: DbSession, _a: Viewer) -> dict[str, Any]:
    rtype = PLURAL[entity_type]
    with db.begin():
        return {"items": service.list_identifiers(db, rtype, get_entity(db, rtype, entity_id))}


@router.post("/{entity_type}/{entity_id}/identifiers", status_code=201, responses=ERR, summary="Add an identifier")
def post_identifier(
    entity_type: PluralPath, entity_id: IdPath, body: IdentifierIn, db: DbSession, request: Request, actor: Editor
) -> dict[str, Any]:
    _writes(request)
    rtype = PLURAL[entity_type]
    with db.begin():
        i = service.add_identifier(db, rtype, entity_id, body, actor, request_id_of(request))
        return {
            "id": i.id,
            "scheme": i.scheme,
            "value": i.value,
            "source_id": i.source_id,
            "authoritative": i.authoritative,
        }


@router.delete(
    "/{entity_type}/{entity_id}/identifiers/{identifier_id}",
    status_code=204,
    responses=ERR,
    summary="Remove an identifier",
)
def delete_identifier(
    entity_type: PluralPath, entity_id: IdPath, identifier_id: IdPath, db: DbSession, request: Request, actor: Editor
) -> None:
    _writes(request)
    with db.begin():
        service.remove_identifier(db, PLURAL[entity_type], entity_id, identifier_id, actor, request_id_of(request))
