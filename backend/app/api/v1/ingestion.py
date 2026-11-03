"""Ingestion, mapping profile, data-quality and lineage endpoints (thin: validation + delegation)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.deps import EDIT, REVIEW, VIEW, DbSession, Limit, Offset
from app.core.auth import CurrentActor
from app.core.errors import (
    DomainValidationError,
    PayloadTooLargeError,
    UnsupportedMediaError,
    WriteDisabledError,
)
from app.ingestion import export, service
from app.ingestion.constants import (
    ALLOWED_DELIMITERS,
    ALLOWED_ENCODINGS,
    MAX_JSON_DEPTH,
    NORMALIZATION_VERSION,
    PREVIEW_FINDINGS_RETURNED,
    PREVIEW_ROWS_RETURNED,
)
from app.ingestion.fieldspecs import SPECS
from app.ingestion.planner import Plan
from app.ingestion.rules import RULES
from app.models.enums import (
    BatchStatus,
    EntityKind,
    IngestPolicy,
    ProfileStatus,
    ResolutionStatus,
    RowOutcome,
    Severity,
    SourceFormat,
)
from app.models.ingestion import (
    DataQualityFinding,
    IngestionBatch,
    IngestionRowResult,
    MappingProfile,
    RecordLineage,
)
from app.models.source import DataSource
from app.quality import hooks as quality_hooks
from app.registry import audit
from app.registry.audit import request_id_of
from app.risk import hooks as risk_hooks
from app.schemas.common import ErrorResponse, Page
from app.schemas.ingestion import (
    BatchRead,
    CommitResponse,
    FindingRead,
    FindingResolution,
    FormatInfo,
    FormatsResponse,
    LineageRead,
    PreviewFinding,
    PreviewResponse,
    PreviewRow,
    ProfileCreate,
    ProfileRead,
    ProfileValidateRequest,
    ProfileValidateResponse,
    RowResultRead,
    RuleRead,
    SourceCreate,
    SourceRead,
)
from app.services.mappers import batch_read

router = APIRouter(prefix="/ingestion", tags=["ingestion"])
lineage_router = APIRouter(tags=["lineage"])

IdPath = Annotated[int, Path(ge=1, le=2**31 - 1)]
ERR = {
    404: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    413: {"model": ErrorResponse},
    415: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

SourceIdQ = Annotated[int, Query(ge=1, le=2**31 - 1, description="Registered data source id.")]
EntityQ = Annotated[EntityKind, Query(description="Entity type contained in the file.")]
PolicyQ = Annotated[IngestPolicy, Query(description="ATOMIC: all-or-nothing. PARTIAL: valid rows apply.")]
ProfileQ = Annotated[
    int | None, Query(ge=1, le=2**31 - 1, description="Mapping profile id; omit for the canonical layout.")
]
FormatQ = Annotated[SourceFormat | None, Query(description="CSV or JSON; inferred from Content-Type when omitted.")]
FilenameQ = Annotated[str | None, Query(max_length=255, description="Display-only file name; never used as a path.")]
EncodingQ = Annotated[str | None, Query(max_length=20, description="Text encoding; auto-detected when omitted.")]
DelimiterQ = Annotated[str, Query(max_length=8, description="CSV delimiter: ',' ';' 'tab' or 'pipe'.")]


def _require_write(request: Request) -> None:
    if not request.app.state.settings.write_api_allowed:
        raise WriteDisabledError(
            "Write endpoints are disabled in this environment (ATLAS_WRITE_API_ENABLED). "
            "ATLAS has no authentication yet, so writes are off by default in production."
        )


async def _read_body(request: Request) -> bytes:
    limit = request.app.state.settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(f"Upload exceeds the {limit} byte limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _format_of(request: Request, fmt: SourceFormat | None, filename: str | None) -> str:
    if fmt in (SourceFormat.CSV, SourceFormat.JSON):
        return fmt.value
    ctype = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype in ("text/csv", "application/csv", "application/vnd.ms-excel"):
        return "CSV"
    if ctype in ("application/json", "text/json"):
        return "JSON"
    if filename:
        low = filename.lower()
        if low.endswith(".csv"):
            return "CSV"
        if low.endswith(".json"):
            return "JSON"
    raise UnsupportedMediaError("Set Content-Type to text/csv or application/json, or pass format=CSV|JSON.")


def _check_options(encoding: str | None, delimiter: str) -> None:
    if encoding is not None and encoding not in ALLOWED_ENCODINGS:
        raise DomainValidationError(f"encoding must be one of {', '.join(ALLOWED_ENCODINGS)}.")
    if delimiter not in ALLOWED_DELIMITERS:
        raise DomainValidationError(f"delimiter must be one of {', '.join(ALLOWED_DELIMITERS)}.")


def _delim(d: str) -> str:
    return d  # parsers map the symbolic name to the character


# ----------------------------------------------------------------------------- reference data


@router.get(
    "/formats",
    dependencies=VIEW,
    response_model=FormatsResponse,
    summary="Supported formats, canonical fields and limits",
)
def formats(request: Request) -> FormatsResponse:
    s = request.app.state.settings
    return FormatsResponse(
        formats=[
            FormatInfo(
                format="CSV",
                content_types=["text/csv"],
                description="Header row + one record per row. Quote fields containing delimiters or line breaks.",
                options={"delimiter": list(ALLOWED_DELIMITERS), "encoding": list(ALLOWED_ENCODINGS)},
            ),
            FormatInfo(
                format="JSON",
                content_types=["application/json"],
                description='A single object, an array of objects, or {"records": [...]}. Nested objects flatten to dotted keys.',  # noqa: E501
                options={"max_depth": MAX_JSON_DEPTH},
            ),
        ],
        entity_types={
            e.value: [
                {"name": f.name, "kind": f.kind, "required": f.required, "description": f.description} for f in fields
            ]
            for e, fields in SPECS.items()
        },
        limits={
            "max_upload_bytes": s.max_upload_bytes,
            "max_rows": s.max_ingest_rows,
            "max_json_depth": MAX_JSON_DEPTH,
        },
        encodings=list(ALLOWED_ENCODINGS),
        delimiters=list(ALLOWED_DELIMITERS),
        policies=[p.value for p in IngestPolicy],
        normalization_version=NORMALIZATION_VERSION,
    )


@router.get("/rules", dependencies=VIEW, response_model=list[RuleRead], summary="Data-quality rule catalog")
def rules() -> list[RuleRead]:
    return [
        RuleRead(rule_id=r.rule_id, name=r.name, severity=r.severity, description=r.description) for r in RULES.values()
    ]


@router.get("/summary", dependencies=VIEW, summary="Ingestion dashboard counters")
def summary(session: DbSession) -> dict:
    return service.summary(session)


# ----------------------------------------------------------------------------- sources


@router.get("/sources", dependencies=VIEW, response_model=Page[SourceRead], summary="List data sources")
def list_sources(session: DbSession, limit: Limit = 50, offset: Offset = 0) -> Page[SourceRead]:
    total = session.scalar(select(func.count()).select_from(DataSource)) or 0
    rows = session.scalars(select(DataSource).order_by(DataSource.id).limit(limit).offset(offset))
    return Page(items=[SourceRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post(
    "/sources",
    dependencies=EDIT,
    response_model=SourceRead,
    status_code=201,
    responses=ERR,
    summary="Register a data source",
)
def create_source(body: SourceCreate, request: Request, session: DbSession) -> SourceRead:
    _require_write(request)
    return SourceRead.model_validate(
        service.create_source(
            session,
            namespace=body.namespace,
            name=body.name,
            source_type=body.source_type,
            description=body.description,
        )
    )


@router.get(
    "/sources/{source_id}", dependencies=VIEW, response_model=SourceRead, responses=ERR, summary="Get a data source"
)
def get_source(source_id: IdPath, session: DbSession) -> SourceRead:
    return SourceRead.model_validate(service.get_source(session, source_id))


# ----------------------------------------------------------------------------- mapping profiles


@router.get(
    "/mapping-profiles",
    dependencies=VIEW,
    response_model=Page[ProfileRead],
    summary="List mapping profiles (all versions)",
)
def list_profiles(
    session: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
    entity_type: EntityKind | None = None,
    status: ProfileStatus | None = None,
) -> Page[ProfileRead]:
    stmt = select(MappingProfile)
    if entity_type:
        stmt = stmt.where(MappingProfile.entity_type == entity_type)
    if status:
        stmt = stmt.where(MappingProfile.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(
        stmt.order_by(MappingProfile.name, MappingProfile.version.desc()).limit(limit).offset(offset)
    )
    return Page(items=[ProfileRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.post(
    "/mapping-profiles",
    dependencies=EDIT,
    response_model=ProfileRead,
    status_code=201,
    responses=ERR,
    summary="Create a mapping profile (new DRAFT version of the name)",
)
def create_profile(body: ProfileCreate, request: Request, session: DbSession) -> ProfileRead:
    _require_write(request)
    return ProfileRead.model_validate(
        service.create_profile(
            session,
            name=body.name,
            entity=body.entity_type,
            fmt=body.source_format,
            source_id=body.source_id,
            definition=body.definition,
            description=body.description,
        )
    )


@router.post(
    "/mapping-profiles/validate",
    dependencies=EDIT,
    response_model=ProfileValidateResponse,
    summary="Validate a mapping definition without saving it",
)
def validate_profile(body: ProfileValidateRequest) -> ProfileValidateResponse:
    problems = service.validate_definition(body.entity_type, body.definition)
    return ProfileValidateResponse(valid=not problems, problems=problems)


@router.get(
    "/mapping-profiles/{profile_id}",
    dependencies=VIEW,
    response_model=ProfileRead,
    responses=ERR,
    summary="Get a mapping profile",
)
def get_profile(profile_id: IdPath, session: DbSession) -> ProfileRead:
    return ProfileRead.model_validate(service.get_profile(session, profile_id))


@router.post(
    "/mapping-profiles/{profile_id}/activate",
    dependencies=REVIEW,
    response_model=ProfileRead,
    responses=ERR,
    summary="Activate a DRAFT profile (retires the previously active version of the same name)",
)
def activate_profile(profile_id: IdPath, request: Request, session: DbSession) -> ProfileRead:
    _require_write(request)
    return ProfileRead.model_validate(service.set_profile_status(session, profile_id, ProfileStatus.ACTIVE))


@router.post(
    "/mapping-profiles/{profile_id}/retire",
    dependencies=REVIEW,
    response_model=ProfileRead,
    responses=ERR,
    summary="Retire a profile so it can no longer be used",
)
def retire_profile(profile_id: IdPath, request: Request, session: DbSession) -> ProfileRead:
    _require_write(request)
    return ProfileRead.model_validate(service.set_profile_status(session, profile_id, ProfileStatus.RETIRED))


# ----------------------------------------------------------------------------- inspect / preview / commit


@router.post("/inspect", dependencies=EDIT, responses=ERR, summary="Detect columns and suggest a mapping (read-only)")
async def inspect_file(
    request: Request,
    entity_type: EntityQ,
    format: FormatQ = None,
    filename: FilenameQ = None,
    encoding: EncodingQ = None,
    delimiter: DelimiterQ = ",",
) -> dict:
    _check_options(encoding, delimiter)
    fmt = _format_of(request, format, filename)
    data = await _read_body(request)
    return await run_in_threadpool(
        service.inspect,
        data,
        entity=entity_type,
        fmt=fmt,
        encoding=encoding,
        delimiter=_delim(delimiter),
        max_rows=request.app.state.settings.max_ingest_rows,
    )


def _preview_response(plan: Plan, applied: IngestionBatch | None) -> PreviewResponse:
    findings = plan.all_findings()
    rank = {Severity.CRITICAL: 0, Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3}
    findings.sort(key=lambda f: (rank[f.severity], f.row_index or 0, f.rule_id))  # type: ignore[index]
    sev: dict[str, int] = {}
    for f in findings:
        sev[f.severity.value] = sev.get(f.severity.value, 0) + 1  # type: ignore[union-attr]
    counts = plan.counts()
    if plan.policy is IngestPolicy.ATOMIC:
        would_apply = not plan.has_blocking and counts["total_rows"] > 0
    else:
        would_apply = counts["to_create"] + counts["to_update"] + counts["unchanged"] > 0 and not any(
            f.blocking for f in plan.file_findings
        )
    rows = [
        PreviewRow(
            row_index=r.index,
            source_line=r.line,
            external_id=r.external_id,
            action=r.action,
            changed_fields=list(r.changed),
            messages=[f"{f.rule_id}: {f.message}" for f in r.findings][:5],
        )
        for r in plan.rows[:PREVIEW_ROWS_RETURNED]
    ]
    return PreviewResponse(
        fingerprint=plan.fingerprint,
        plan_digest=plan.plan_digest,
        policy=plan.policy,
        entity_type=plan.entity,
        source_format=SourceFormat(plan.fmt),
        source_namespace=plan.source.namespace,
        mapping_profile_ref=plan.profile.ref,
        normalization_version=NORMALIZATION_VERSION,
        file_name=plan.file_name and plan.file_name[:100],
        content_sha256=plan.content_sha256,
        content_bytes=plan.content_bytes,
        would_apply=would_apply,
        already_applied_batch=applied.batch_uid if applied else None,
        counts=counts,
        finding_counts=sev,
        findings=[
            PreviewFinding(
                rule_id=f.rule_id,
                severity=f.severity,
                row_index=f.row_index,  # type: ignore[arg-type]
                source_line=f.source_line,
                external_id=f.external_id,
                field=f.field,
                message=f.message,
                suggestion=f.suggestion,
                observed_value=f.observed,
            )
            for f in findings[:PREVIEW_FINDINGS_RETURNED]
        ],
        findings_truncated=len(findings) > PREVIEW_FINDINGS_RETURNED,
        rows=rows,
        rows_truncated=len(plan.rows) > PREVIEW_ROWS_RETURNED,
        columns=plan.parsed.columns,
    )


@router.post(
    "/preview",
    dependencies=EDIT,
    response_model=PreviewResponse,
    responses=ERR,
    summary="Dry run: the exact commit pipeline with nothing persisted",
)
async def preview(
    request: Request,
    session: DbSession,
    source_id: SourceIdQ,
    entity_type: EntityQ,
    policy: PolicyQ = IngestPolicy.PARTIAL,
    mapping_profile_id: ProfileQ = None,
    format: FormatQ = None,
    filename: FilenameQ = None,
    encoding: EncodingQ = None,
    delimiter: DelimiterQ = ",",
) -> PreviewResponse:
    _check_options(encoding, delimiter)
    fmt = _format_of(request, format, filename)
    data = await _read_body(request)
    r = service.IngestRequest(
        source_id, entity_type, fmt, data, mapping_profile_id, policy, encoding, _delim(delimiter), filename
    )
    result = await run_in_threadpool(service.preview, session, r, max_rows=request.app.state.settings.max_ingest_rows)
    return _preview_response(result.plan, result.already_applied)


@router.post(
    "/commit",
    dependencies=EDIT,
    response_model=CommitResponse,
    responses={**ERR, 201: {"model": CommitResponse}},
    summary="Apply a file. Identical input is idempotent; REJECTED/FAILED are never reported as success",
)
async def commit(
    request: Request,
    session: DbSession,
    source_id: SourceIdQ,
    entity_type: EntityQ,
    policy: PolicyQ = IngestPolicy.PARTIAL,
    mapping_profile_id: ProfileQ = None,
    format: FormatQ = None,
    filename: FilenameQ = None,
    encoding: EncodingQ = None,
    delimiter: DelimiterQ = ",",
    expected_plan_digest: Annotated[
        str | None,
        Query(
            min_length=64,
            max_length=64,
            pattern="^[0-9a-f]{64}$",
            description="plan_digest from the preview; stale -> 409.",
        ),
    ] = None,
):
    _require_write(request)
    _check_options(encoding, delimiter)
    fmt = _format_of(request, format, filename)
    data = await _read_body(request)
    r = service.IngestRequest(
        source_id, entity_type, fmt, data, mapping_profile_id, policy, encoding, _delim(delimiter), filename
    )
    result = await run_in_threadpool(
        service.commit,
        session,
        r,
        max_rows=request.app.state.settings.max_ingest_rows,
        expected_digest=expected_plan_digest,
    )
    applied = result.batch.status in (BatchStatus.COMMITTED, BatchStatus.COMPLETED_WITH_ERRORS)
    if applied and not result.replay:
        await run_in_threadpool(quality_hooks.reevaluate_batch, request, result.batch.id, "import")
        await run_in_threadpool(risk_hooks.reassess_batch, request, result.batch.id, "import")
    body = CommitResponse(ok=applied, replay=result.replay, batch=batch_read(result.batch))
    if applied:
        code = 200 if result.replay else 201
    else:
        code = 422 if result.batch.status is BatchStatus.REJECTED else 409
    return JSONResponse(status_code=code, content=body.model_dump(mode="json"))


# ----------------------------------------------------------------------------- batches


@router.get("/batches", dependencies=VIEW, response_model=Page[BatchRead], summary="Ingestion history")
def list_batches(
    session: DbSession,
    limit: Limit = 25,
    offset: Offset = 0,
    status: BatchStatus | None = None,
    source_id: Annotated[int | None, Query(ge=1)] = None,
    entity_type: EntityKind | None = None,
) -> Page[BatchRead]:
    stmt = select(IngestionBatch)
    if status:
        stmt = stmt.where(IngestionBatch.status == status)
    if source_id:
        stmt = stmt.where(IngestionBatch.source_id == source_id)
    if entity_type:
        stmt = stmt.where(IngestionBatch.entity_type == entity_type)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(IngestionBatch.id.desc()).limit(limit).offset(offset))
    return Page(items=[batch_read(b) for b in rows], total=total, limit=limit, offset=offset)


@router.get(
    "/batches/{batch_id}",
    dependencies=VIEW,
    response_model=BatchRead,
    responses=ERR,
    summary="Batch / job status and summary",
)
def get_batch(batch_id: IdPath, session: DbSession) -> BatchRead:
    return batch_read(service.get_batch(session, batch_id))


@router.get(
    "/batches/{batch_id}/rows",
    dependencies=VIEW,
    response_model=Page[RowResultRead],
    responses=ERR,
    summary="Per-row outcomes with original row numbers",
)
def batch_rows(
    batch_id: IdPath, session: DbSession, limit: Limit = 50, offset: Offset = 0, outcome: RowOutcome | None = None
) -> Page[RowResultRead]:
    service.get_batch(session, batch_id)
    stmt = select(IngestionRowResult).where(IngestionRowResult.batch_id == batch_id)
    if outcome:
        stmt = stmt.where(IngestionRowResult.outcome == outcome)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(IngestionRowResult.row_index).limit(limit).offset(offset))
    return Page(items=[RowResultRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


# ----------------------------------------------------------------------------- findings


def _finding_stmt(batch_id, rule_id, severity, resolution_status, entity_type, source_id):
    stmt = select(DataQualityFinding)
    if batch_id:
        stmt = stmt.where(DataQualityFinding.batch_id == batch_id)
    if rule_id:
        stmt = stmt.where(DataQualityFinding.rule_id == rule_id)
    if severity:
        stmt = stmt.where(DataQualityFinding.severity == severity)
    if resolution_status:
        stmt = stmt.where(DataQualityFinding.resolution_status == resolution_status)
    if entity_type:
        stmt = stmt.where(DataQualityFinding.entity_type == entity_type)
    if source_id:
        stmt = stmt.where(DataQualityFinding.source_id == source_id)
    return stmt


RuleQ = Annotated[str | None, Query(pattern=r"^DQ\d{3}$")]
BatchQ = Annotated[int | None, Query(ge=1, le=2**31 - 1)]


@router.get(
    "/findings", dependencies=VIEW, response_model=Page[FindingRead], summary="Data-quality findings (filterable)"
)
def list_findings(
    session: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
    batch_id: BatchQ = None,
    rule_id: RuleQ = None,
    severity: Severity | None = None,
    resolution_status: ResolutionStatus | None = None,
    entity_type: EntityKind | None = None,
    source_id: BatchQ = None,
) -> Page[FindingRead]:
    stmt = _finding_stmt(batch_id, rule_id, severity, resolution_status, entity_type, source_id)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(DataQualityFinding.id.desc()).limit(limit).offset(offset))
    return Page(items=[FindingRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


@router.get(
    "/findings/export.csv",
    dependencies=VIEW,
    summary="Export findings as CSV (formula-injection safe)",
    responses={200: {"content": {"text/csv": {}}}},
)
def export_findings(
    request: Request,
    session: DbSession,
    actor: CurrentActor,
    batch_id: BatchQ = None,
    rule_id: RuleQ = None,
    severity: Severity | None = None,
    resolution_status: ResolutionStatus | None = None,
    entity_type: EntityKind | None = None,
    source_id: BatchQ = None,
) -> Response:
    stmt = _finding_stmt(batch_id, rule_id, severity, resolution_status, entity_type, source_id)
    rows = list(session.scalars(stmt.order_by(DataQualityFinding.id).limit(10_000)))
    audit.record(  # a bulk export of findings is a sensitive read: who, when, how many rows, which filters
        session,
        actor,
        "export.created",
        target_type="findings_csv",
        details={
            "rows": len(rows),
            "filters": {
                k: str(getattr(v, "value", v))
                for k, v in {
                    "batch_id": batch_id,
                    "rule_id": rule_id,
                    "severity": severity,
                    "resolution_status": resolution_status,
                    "entity_type": entity_type,
                    "source_id": source_id,
                }.items()
                if v is not None
            },
        },
        request_id=request_id_of(request),
    )
    session.commit()
    header = [
        "finding_id",
        "batch_id",
        "rule_id",
        "severity",
        "entity_type",
        "external_id",
        "field",
        "row_index",
        "source_line",
        "message",
        "suggestion",
        "observed_value",
        "resolution_status",
        "resolution_note",
    ]
    body = export.to_csv(
        header,
        (
            [
                f.id,
                f.batch_id,
                f.rule_id,
                f.severity.value,
                f.entity_type.value if f.entity_type else "",
                f.external_id,
                f.field,
                f.row_index,
                f.source_line,
                f.message,
                f.suggestion,
                f.observed_value,
                f.resolution_status.value,
                f.resolution_note,
            ]
            for f in rows
        ),
    )
    return Response(
        body,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="atlas-findings.csv"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/findings/{finding_id}", dependencies=VIEW, response_model=FindingRead, responses=ERR, summary="Get one finding"
)
def get_finding(finding_id: IdPath, session: DbSession) -> FindingRead:
    return FindingRead.model_validate(service.get_finding(session, finding_id))


@router.put(
    "/findings/{finding_id}/resolution",
    dependencies=EDIT,
    response_model=FindingRead,
    responses=ERR,
    summary="Review a finding (acknowledge, resolve, won't fix, reopen). The finding is never deleted",
)
def resolve_finding(finding_id: IdPath, body: FindingResolution, request: Request, session: DbSession) -> FindingRead:
    _require_write(request)
    return FindingRead.model_validate(service.resolve_finding(session, finding_id, body.resolution_status, body.note))


# ----------------------------------------------------------------------------- lineage


def _lineage_read(session: Session, rows) -> list[LineageRead]:
    out: list[LineageRead] = []
    batches: dict[int, IngestionBatch] = {}
    for r in rows:
        b = batches.get(r.batch_id) or session.get(IngestionBatch, r.batch_id)
        batches[r.batch_id] = b  # type: ignore[assignment]
        item = LineageRead.model_validate(r)
        item.batch_uid = b.batch_uid if b else None
        item.file_name = b.file_name if b else None
        item.source_namespace = b.source.namespace if b else None
        out.append(item)
    return out


@lineage_router.get(
    "/lineage", dependencies=VIEW, response_model=Page[LineageRead], summary="Record lineage (filterable)"
)
def lineage(
    session: DbSession,
    limit: Limit = 50,
    offset: Offset = 0,
    entity_type: EntityKind | None = None,
    entity_id: BatchQ = None,
    batch_id: BatchQ = None,
    source_id: BatchQ = None,
) -> Page[LineageRead]:
    stmt = select(RecordLineage)
    if entity_type:
        stmt = stmt.where(RecordLineage.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(RecordLineage.entity_id == entity_id)
    if batch_id:
        stmt = stmt.where(RecordLineage.batch_id == batch_id)
    if source_id:
        stmt = stmt.where(RecordLineage.source_id == source_id)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(RecordLineage.id.desc()).limit(limit).offset(offset)).all()
    return Page(items=_lineage_read(session, rows), total=total, limit=limit, offset=offset)


@lineage_router.get(
    "/lineage/{entity_type}/{entity_id}",
    dependencies=VIEW,
    response_model=Page[LineageRead],
    responses=ERR,
    summary="Full history of where one record's values came from",
)
def entity_lineage(
    entity_type: EntityKind, entity_id: IdPath, session: DbSession, limit: Limit = 50, offset: Offset = 0
) -> Page[LineageRead]:
    return lineage(session, limit, offset, entity_type, entity_id, None, None)
