"""Ingestion orchestration: sources, mapping profiles, inspect, preview, commit and read-side queries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, NotFoundError, StalePreviewError
from app.ingestion import commit as commit_mod
from app.ingestion.constants import NAMESPACE_PATTERN
from app.ingestion.fieldspecs import SPECS
from app.ingestion.parsers import detect_encoding, parse_csv, parse_json
from app.ingestion.planner import Plan, build_plan
from app.ingestion.profile import (
    ProfileDefinition,
    ResolvedProfile,
    builtin_profile,
    definition_hash,
    definition_problems,
)
from app.models.enums import (
    BatchStatus,
    EntityKind,
    IngestPolicy,
    ProfileStatus,
    Provenance,
    ResolutionStatus,
    SourceFormat,
    SourceStatus,
    SourceType,
)
from app.models.ingestion import DataQualityFinding, IngestionBatch, IngestionRowResult, MappingProfile
from app.models.source import DataSource
from app.models.types import utcnow

# ----------------------------------------------------------------------------- sources


def create_source(
    session: Session, *, namespace: str, name: str, source_type: SourceType, description: str | None
) -> DataSource:
    if not NAMESPACE_PATTERN.match(namespace):
        raise DomainValidationError(
            "namespace must be 2-48 characters: lowercase letters, digits and '-', starting with a letter or digit."
        )
    _fresh(session)
    with session.begin():
        if session.scalar(select(DataSource.id).where(DataSource.namespace == namespace)):
            raise ConflictError(f"Namespace '{namespace}' is already registered.")
        src = DataSource(
            namespace=namespace,
            name=name.strip(),
            source_type=source_type,
            provenance=Provenance.IMPORTED_EXTERNAL,
            status=SourceStatus.ACTIVE,
            description=description,
        )
        session.add(src)
        session.flush()
    return src


def get_source(session: Session, source_id: int) -> DataSource:
    src = session.get(DataSource, source_id)
    if src is None:
        raise NotFoundError(f"Data source {source_id} was not found.")
    return src


# ----------------------------------------------------------------------------- mapping profiles


def parse_definition(raw: dict[str, Any]) -> ProfileDefinition:
    try:
        return ProfileDefinition.model_validate(raw)
    except ValidationError as exc:
        msgs = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:10]]
        raise DomainValidationError("Invalid mapping definition: " + "; ".join(msgs)) from exc


def validate_definition(entity: EntityKind, raw: dict[str, Any]) -> list[str]:
    try:
        d = ProfileDefinition.model_validate(raw)
    except ValidationError as exc:
        return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:20]]
    return definition_problems(entity, d)


def create_profile(
    session: Session,
    *,
    name: str,
    entity: EntityKind,
    fmt: SourceFormat,
    source_id: int | None,
    definition: dict[str, Any],
    description: str | None,
) -> MappingProfile:
    d = parse_definition(definition)
    problems = definition_problems(entity, d)
    if problems:
        raise DomainValidationError("Mapping definition is not valid: " + " ".join(problems[:5]))
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]{0,118}$", name):
        raise DomainValidationError("Profile name may contain letters, digits, space, '.', '_' and '-' (max 120).")
    if name.strip().lower() == "canonical":
        raise DomainValidationError("'canonical' is reserved for the built-in profile.")
    _fresh(session)
    with session.begin():
        if source_id is not None:
            get_source(session, source_id)
        latest = session.scalar(
            select(MappingProfile).where(MappingProfile.name == name).order_by(MappingProfile.version.desc()).limit(1)
        )
        if latest is not None and latest.entity_type is not entity:
            raise ConflictError("A profile with this name exists for a different entity type.")
        prof = MappingProfile(
            name=name,
            version=(latest.version + 1) if latest else 1,
            entity_type=entity,
            source_format=fmt,
            source_id=source_id,
            status=ProfileStatus.DRAFT,
            definition=d.model_dump(mode="json"),
            definition_hash=definition_hash(d),
            description=description,
        )
        session.add(prof)
        session.flush()
    return prof


def get_profile(session: Session, profile_id: int) -> MappingProfile:
    p = session.get(MappingProfile, profile_id)
    if p is None:
        raise NotFoundError(f"Mapping profile {profile_id} was not found.")
    return p


def set_profile_status(session: Session, profile_id: int, target: ProfileStatus) -> MappingProfile:
    _fresh(session)
    with session.begin():
        p = get_profile(session, profile_id)
        if target is ProfileStatus.ACTIVE:
            if p.status is not ProfileStatus.DRAFT:
                raise ConflictError("Only a DRAFT profile can be activated. Create a new version to change a profile.")
            for other in session.scalars(
                select(MappingProfile).where(
                    MappingProfile.name == p.name, MappingProfile.status == ProfileStatus.ACTIVE
                )
            ):
                other.status = ProfileStatus.RETIRED
            p.status = ProfileStatus.ACTIVE
            p.activated_at = utcnow()
        elif target is ProfileStatus.RETIRED:
            if p.status is ProfileStatus.RETIRED:
                raise ConflictError("Profile is already retired.")
            p.status = ProfileStatus.RETIRED
        session.flush()
    return p


def resolve_profile(
    session: Session, entity: EntityKind, fmt: str, profile_id: int | None, *, require_active: bool
) -> ResolvedProfile:
    if profile_id is None:
        return builtin_profile(entity)
    p = get_profile(session, profile_id)
    if p.entity_type is not entity:
        raise DomainValidationError(f"Profile '{p.name}' maps {p.entity_type.value}, not {entity.value}.")
    if p.source_format not in (SourceFormat.ANY, SourceFormat(fmt)):
        raise DomainValidationError(f"Profile '{p.name}' is for {p.source_format.value} files, not {fmt}.")
    if p.status is ProfileStatus.RETIRED:
        raise DomainValidationError(f"Profile '{p.name}' v{p.version} is retired.")
    if require_active and p.status is not ProfileStatus.ACTIVE:
        raise DomainValidationError(
            f"Profile '{p.name}' v{p.version} is {p.status.value}; activate it before committing."
        )
    d = ProfileDefinition.model_validate(p.definition)
    return ResolvedProfile(f"{p.name}:v{p.version}", d, p.definition_hash, p.id)


# ----------------------------------------------------------------------------- requests


@dataclass
class IngestRequest:
    source_id: int
    entity: EntityKind
    fmt: str
    data: bytes
    profile_id: int | None
    policy: IngestPolicy
    encoding: str | None
    delimiter: str
    file_name: str | None


def _prepare(session: Session, req: IngestRequest, *, require_active: bool, max_rows: int) -> Plan:
    source = get_source(session, req.source_id)
    if source.status is not SourceStatus.ACTIVE:
        raise ConflictError("The data source is disabled.")
    if not req.data:
        raise DomainValidationError("The request body is empty.")
    profile = resolve_profile(session, req.entity, req.fmt, req.profile_id, require_active=require_active)
    encoding = req.encoding or detect_encoding(req.data)
    return build_plan(
        session,
        source=source,
        entity=req.entity,
        fmt=req.fmt,
        data=req.data,
        profile=profile,
        policy=req.policy,
        encoding=encoding,
        delimiter=req.delimiter,
        file_name=req.file_name,
        max_rows=max_rows,
    )


def _fresh(session: Session) -> None:
    if session.in_transaction():
        session.rollback()


@dataclass
class PreviewResult:
    plan: Plan
    already_applied: IngestionBatch | None


def preview(session: Session, req: IngestRequest, *, max_rows: int) -> PreviewResult:
    """Read-only: runs the exact commit pipeline, then rolls back. Nothing is persisted."""
    _fresh(session)
    try:
        plan = _prepare(session, req, require_active=False, max_rows=max_rows)
        applied = commit_mod.find_applied_batch(session, plan.fingerprint)
    finally:
        session.rollback()
    return PreviewResult(plan, applied)


@dataclass
class CommitResult:
    batch: IngestionBatch
    replay: bool


def commit(session: Session, req: IngestRequest, *, max_rows: int, expected_digest: str | None) -> CommitResult:
    _fresh(session)
    fingerprint: str | None = None
    try:
        with session.begin():
            plan = _prepare(session, req, require_active=True, max_rows=max_rows)
            fingerprint = plan.fingerprint
            existing = commit_mod.find_applied_batch(session, plan.fingerprint)
            if existing is not None:
                return CommitResult(existing, True)  # idempotent: identical input already applied
            if expected_digest and expected_digest != plan.plan_digest:
                raise StalePreviewError(
                    "The data or registry changed since the preview. Run the preview again before committing."
                )
            batch = commit_mod.make_batch(plan)
            session.add(batch)
            session.flush()
            commit_mod.apply_plan(session, plan, batch)
        return CommitResult(batch, False)
    except IntegrityError:
        # Lost a race: a concurrent commit of the same fingerprint (or unique key) won. Nothing of ours persisted.
        session.rollback()
        if fingerprint:
            winner = commit_mod.find_applied_batch(session, fingerprint)
            if winner is not None:
                session.rollback()
                return CommitResult(winner, True)
        raise ConflictError(
            "The batch conflicted with a concurrent change and was rolled back. Retrying is safe."
        ) from None


# ----------------------------------------------------------------------------- inspect (wizard step)

_SYNONYMS = {
    "id": "external_id",
    "code": "external_id",
    "supplier_id": "external_id",
    "facility_id": "external_id",
    "product_id": "external_id",
    "sku": "external_id",
    "supplier_name": "name",
    "facility_name": "name",
    "product_name": "name",
    "country": "country_or_region",
    "region": "country_or_region",
    "type": "facility_type",
}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")


def inspect(
    data: bytes, *, entity: EntityKind, fmt: str, encoding: str | None, delimiter: str, max_rows: int
) -> dict[str, Any]:
    if not data:
        raise DomainValidationError("The request body is empty.")
    enc = encoding or detect_encoding(data)
    parsed = (
        parse_csv(data, encoding=enc, delimiter=delimiter, max_rows=max_rows)
        if fmt == "CSV"
        else parse_json(data, encoding=enc, max_rows=max_rows)
    )
    canonical = {s.name for s in SPECS[entity]}
    taken: set[str] = set()
    suggestion: dict[str, dict[str, str]] = {}
    for col in parsed.columns:
        n = _norm_header(col)
        target = n if n in canonical else _SYNONYMS.get(n)
        if target in canonical and target not in taken:
            suggestion[target] = {"source": col}
            taken.add(target)
    sample = [
        {k: (str(v)[:80] if v is not None else None) for k, v in list(r.values.items())[:30]}
        for r in parsed.rows[:5]
        if r.error is None
    ]
    return {
        "format": fmt,
        "encoding": enc,
        "delimiter": parsed.delimiter,
        "columns": parsed.columns,
        "row_count": len(parsed.rows),
        "sample_rows": sample,
        "file_problems": [
            {"rule_id": f.rule_id, "severity": f.severity.value, "message": f.message, "suggestion": f.suggestion}
            for f in parsed.file_findings
        ],
        "suggested_columns": suggestion,
        "unmapped_columns": [c for c in parsed.columns if not any(v["source"] == c for v in suggestion.values())],
        "canonical_fields": [
            {"name": s.name, "kind": s.kind, "required": s.required, "description": s.description or ""}
            for s in SPECS[entity]
        ],
    }


# ----------------------------------------------------------------------------- read side


def page_of(session: Session, stmt, count_stmt, limit: int, offset: int):
    total = session.scalar(count_stmt) or 0
    items = list(session.scalars(stmt.limit(limit).offset(offset)))
    return items, total


def get_batch(session: Session, batch_id: int) -> IngestionBatch:
    b = session.get(IngestionBatch, batch_id)
    if b is None:
        raise NotFoundError(f"Batch {batch_id} was not found.")
    return b


def get_finding(session: Session, finding_id: int) -> DataQualityFinding:
    f = session.get(DataQualityFinding, finding_id)
    if f is None:
        raise NotFoundError(f"Finding {finding_id} was not found.")
    return f


def resolve_finding(
    session: Session, finding_id: int, status: ResolutionStatus, note: str | None
) -> DataQualityFinding:
    _fresh(session)
    with session.begin():
        f = get_finding(session, finding_id)
        f.resolution_status = status
        f.resolution_note = (note or None) and note[:400]  # type: ignore[index]
        f.resolved_at = utcnow() if status in (ResolutionStatus.RESOLVED, ResolutionStatus.WONT_FIX) else None
        session.flush()
    return f


def summary(session: Session) -> dict[str, Any]:
    def count(model, *where):
        return session.scalar(select(func.count()).select_from(model).where(*where)) or 0

    by_status = dict(session.execute(select(IngestionBatch.status, func.count()).group_by(IngestionBatch.status)).all())
    by_sev = dict(
        session.execute(
            select(DataQualityFinding.severity, func.count())
            .where(DataQualityFinding.resolution_status == ResolutionStatus.OPEN)
            .group_by(DataQualityFinding.severity)
        ).all()
    )
    return {
        "sources": count(DataSource),
        "profiles": count(MappingProfile),
        "active_profiles": count(MappingProfile, MappingProfile.status == ProfileStatus.ACTIVE),
        "batches": count(IngestionBatch),
        "batches_by_status": {k.value: v for k, v in by_status.items()},
        "open_findings": sum(by_sev.values()),
        "open_findings_by_severity": {k.value: v for k, v in by_sev.items()},
        "rows_processed": session.scalar(select(func.coalesce(func.sum(IngestionBatch.total_rows), 0))) or 0,
    }


__all__ = [name for name in dir() if not name.startswith("_")] + ["BatchStatus", "IngestionRowResult"]
