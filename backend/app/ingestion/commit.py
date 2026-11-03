"""Applies a plan to the registry. Services own transactions; this module is called inside one."""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.ingestion import entities as ent
from app.ingestion.constants import MAX_FINDINGS_PERSISTED, NORMALIZATION_VERSION
from app.ingestion.planner import Plan, PlanRow
from app.ingestion.rules import Finding
from app.models.enums import (
    BatchStatus,
    EntityKind,
    EntityStatus,
    IngestPolicy,
    LocationStatus,
    ResolutionStatus,
    RowOutcome,
    Severity,
    SourceFormat,
)
from app.models.ingestion import DataQualityFinding, IngestionBatch, IngestionRowResult, RecordLineage
from app.models.types import utcnow

logger = logging.getLogger("atlas.ingestion")

APPLIED = (BatchStatus.COMMITTED, BatchStatus.COMPLETED_WITH_ERRORS)


@dataclass
class _Applied:
    outcome: RowOutcome
    entity_id: int | None
    changed: list[str] | None = None


def new_batch_uid() -> str:
    return "BATCH-" + uuid.uuid4().hex[:16].upper()


def sanitize_file_name(name: str | None) -> str | None:
    """Display-only name. Never used as a filesystem path: directories, control chars and dots stripped."""
    if not name:
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip(" .")
    cleaned = cleaned[:100]
    return cleaned or None


def make_batch(plan: Plan) -> IngestionBatch:
    return IngestionBatch(
        batch_uid=new_batch_uid(),
        fingerprint=plan.fingerprint,
        source_id=plan.source.id,
        entity_type=plan.entity,
        source_format=SourceFormat(plan.fmt),
        mapping_profile_id=plan.profile.profile_id,
        mapping_profile_ref=plan.profile.ref,
        policy=plan.policy,
        status=BatchStatus.PROCESSING,
        file_name=sanitize_file_name(plan.file_name),
        content_sha256=plan.content_sha256,
        content_bytes=plan.content_bytes,
        normalization_version=NORMALIZATION_VERSION,
        total_rows=len(plan.rows),
    )


def _set_value(obj: Any, key: str, value: Any) -> None:
    setattr(obj, key, value)


def _create(session: Session, plan: Plan, row: PlanRow) -> _Applied:
    entity = plan.entity
    model = ent.MODEL[entity]
    kwargs: dict[str, Any] = {
        ent.CODE_ATTR[entity]: ent.external_code(plan.source.namespace, row.external_id or ""),
        "source_id": plan.source.id,
        "external_id": row.external_id,
        "provenance": plan.source.provenance,
    }
    for key in ent.APPLY_FIELDS[entity]:
        v = row.values.get(key)
        if v is not None:
            kwargs[key] = v
    if entity in (EntityKind.SUPPLIER, EntityKind.FACILITY, EntityKind.PRODUCT):
        kwargs.setdefault("status", EntityStatus.ACTIVE)
    if entity is EntityKind.FACILITY:
        if "latitude" in kwargs and "location_status" not in kwargs:
            kwargs["location_status"] = LocationStatus.UNVERIFIED  # coordinates came from a source, not verified
        kwargs.setdefault("location_status", LocationStatus.UNKNOWN)
    for ref_name, attr in ent.REF_ATTR.get(entity, {}).items():
        if ref_name in row.refs:
            kwargs[attr] = row.refs[ref_name]
    obj = model(**kwargs)
    session.add(obj)
    session.flush()
    return _Applied(
        RowOutcome.CREATED, obj.id, sorted(k for k in row.values if row.values[k] is not None and k != "external_id")
    )


def _update(session: Session, plan: Plan, row: PlanRow) -> _Applied:
    model = ent.MODEL[plan.entity]
    obj = session.get(model, row.existing_id)
    if obj is None:  # deleted between plan and apply: cannot happen in one transaction, but never assume
        raise IntegrityError("update", {}, Exception("record vanished"))
    for key in row.changed:
        attr = ent.REF_ATTR.get(plan.entity, {}).get(key)
        if attr:
            setattr(obj, attr, row.refs[key])
        else:
            _set_value(obj, key, row.values[key])
    if plan.entity is EntityKind.FACILITY and "latitude" in row.changed and "location_status" not in row.changed:
        obj.location_status = LocationStatus.UNVERIFIED
    session.flush()
    return _Applied(RowOutcome.UPDATED, obj.id, list(row.changed))


def _apply_row(session: Session, plan: Plan, row: PlanRow) -> _Applied:
    if row.action == "CREATE":
        return _create(session, plan, row)
    if row.action == "UPDATE":
        return _update(session, plan, row)
    return _Applied(RowOutcome.UNCHANGED, row.existing_id)


def _db_failure_finding(row: PlanRow, exc: Exception) -> Finding:
    return Finding(
        "DQ014",
        "The database rejected this record (constraint violation or concurrent change). It was not stored.",
        row_index=row.index,
        source_line=row.line,
        external_id=row.external_id,
        suggestion="Retry the import; if it persists, check for conflicting records.",
    )


def apply_plan(session: Session, plan: Plan, batch: IngestionBatch) -> None:
    """Mutates `batch` with final status/counters and persists row results, lineage and findings.

    ATOMIC: all valid rows apply or none do. PARTIAL: each valid row applies independently.
    """
    now = utcnow()
    extra_findings: list[Finding] = []
    results: dict[int, _Applied | RowOutcome] = {}
    failure: str | None = None
    applicable = [r for r in plan.rows if r.action in ("CREATE", "UPDATE", "UNCHANGED")]

    file_blocked = any(f.blocking for f in plan.file_findings)
    if plan.parsed.fatal or file_blocked:
        for r in plan.rows:
            results[r.index] = (
                RowOutcome.DUPLICATE_IN_BATCH
                if r.action == "DUPLICATE"
                else RowOutcome.SKIPPED
                if not r.blocked
                else RowOutcome.REJECTED
            )
        batch.status = BatchStatus.REJECTED
        failure = "File-level problems prevented processing."
    elif plan.policy is IngestPolicy.ATOMIC:
        if plan.has_blocking:
            for r in plan.rows:
                results[r.index] = (
                    RowOutcome.DUPLICATE_IN_BATCH
                    if r.action == "DUPLICATE"
                    else RowOutcome.REJECTED
                    if r.blocked
                    else RowOutcome.SKIPPED
                )
            batch.status = BatchStatus.REJECTED
            failure = "Atomic policy: at least one row has blocking findings, so nothing was applied."
        else:
            try:
                with session.begin_nested():
                    for r in applicable:
                        results[r.index] = _apply_row(session, plan, r)
            except (IntegrityError, DBAPIError) as exc:
                logger.warning("ingestion_atomic_failed", extra={"batch": batch.batch_uid, "error": type(exc).__name__})
                results = {r.index: RowOutcome.SKIPPED for r in plan.rows}
                batch.status = BatchStatus.FAILED
                failure = "Database rejected the batch; every change was rolled back."
                extra_findings.append(
                    Finding(
                        "DQ014",
                        "The database rejected the batch (constraint violation or concurrent "
                        "change). All changes were rolled back; retry is safe.",
                    )
                )
            else:
                batch.status = BatchStatus.COMMITTED
    else:  # PARTIAL
        for r in plan.rows:
            if r.action == "DUPLICATE":
                results[r.index] = RowOutcome.DUPLICATE_IN_BATCH
            elif r.blocked or r.action == "REJECT":
                results[r.index] = RowOutcome.REJECTED
        for r in applicable:
            try:
                with session.begin_nested():
                    results[r.index] = _apply_row(session, plan, r)
            except (IntegrityError, DBAPIError) as exc:
                results[r.index] = RowOutcome.REJECTED
                extra_findings.append(_db_failure_finding(r, exc))
        applied = sum(1 for v in results.values() if isinstance(v, _Applied))
        bad = sum(1 for v in results.values() if not isinstance(v, _Applied))
        if applied == 0:
            batch.status = BatchStatus.REJECTED
            failure = "No row could be applied."
        else:
            batch.status = BatchStatus.COMPLETED_WITH_ERRORS if bad else BatchStatus.COMMITTED

    # ---- counters (committed / rejected / skipped / duplicate are kept separate)
    tally: Counter[str] = Counter()
    for r in plan.rows:
        res = results.get(r.index, RowOutcome.SKIPPED)
        outcome = res.outcome if isinstance(res, _Applied) else res
        tally[outcome.value] += 1
    batch.created_count = tally[RowOutcome.CREATED.value]
    batch.updated_count = tally[RowOutcome.UPDATED.value]
    batch.unchanged_count = tally[RowOutcome.UNCHANGED.value]
    batch.rejected_count = tally[RowOutcome.REJECTED.value]
    batch.skipped_count = tally[RowOutcome.SKIPPED.value]
    batch.duplicate_count = tally[RowOutcome.DUPLICATE_IN_BATCH.value]
    batch.failure_reason = failure
    batch.finished_at = now

    # ---- row results + lineage
    by_index = {r.index: r for r in plan.rows}
    for idx, res in sorted(results.items()):
        r = by_index[idx]
        outcome = res.outcome if isinstance(res, _Applied) else res
        entity_id = res.entity_id if isinstance(res, _Applied) else None
        session.add(
            IngestionRowResult(
                batch_id=batch.id,
                row_index=idx,
                source_line=r.line,
                external_id=r.external_id,
                outcome=outcome,
                entity_id=entity_id,
                record_fingerprint=r.fingerprint,
                message=_row_message(r, outcome),
            )
        )
        if isinstance(res, _Applied) and outcome in (RowOutcome.CREATED, RowOutcome.UPDATED) and entity_id is not None:
            session.add(
                RecordLineage(
                    entity_type=plan.entity,
                    entity_id=entity_id,
                    batch_id=batch.id,
                    source_id=plan.source.id,
                    external_id=r.external_id or "",
                    row_index=idx,
                    source_line=r.line,
                    mapping_profile_ref=plan.profile.ref,
                    normalization_version=NORMALIZATION_VERSION,
                    record_fingerprint=r.fingerprint or "",
                    outcome=outcome,
                    changed_fields=res.changed,
                    ingested_at=now,
                )
            )

    # ---- findings (history is preserved; never deleted)
    all_findings = plan.all_findings() + extra_findings
    batch.finding_count = len(all_findings)
    rank = {Severity.CRITICAL: 0, Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFO: 3}
    all_findings.sort(key=lambda f: (rank[f.severity], f.row_index or 0, f.rule_id))  # type: ignore[index]
    keys: set[str] = set()
    for f in all_findings[:MAX_FINDINGS_PERSISTED]:
        key = f.key(batch.batch_uid)
        n = 1
        while key in keys:
            n += 1
            key = f.key(batch.batch_uid + f"#{n}")
        keys.add(key)
        session.add(
            DataQualityFinding(
                finding_key=key,
                rule_id=f.rule_id,
                severity=f.severity,
                batch_id=batch.id,
                source_id=plan.source.id,
                entity_type=plan.entity,
                external_id=f.external_id,
                field=f.field,
                row_index=f.row_index,
                source_line=f.source_line,
                message=f.message[:400],
                suggestion=(f.suggestion or None) and f.suggestion[:400],
                observed_value=f.observed,
                resolution_status=ResolutionStatus.OPEN,
            )
        )
    session.flush()


def _row_message(r: PlanRow, outcome: RowOutcome) -> str | None:
    if outcome in (RowOutcome.REJECTED, RowOutcome.DUPLICATE_IN_BATCH):
        blocking = [f for f in r.findings if f.blocking]
        if blocking:
            return f"{blocking[0].rule_id}: {blocking[0].message}"[:300]
        return "Rejected."
    if outcome is RowOutcome.SKIPPED:
        return "Valid but not applied because the atomic batch was not committed."
    return None


def find_applied_batch(session: Session, fingerprint: str) -> IngestionBatch | None:
    return session.scalar(
        select(IngestionBatch).where(IngestionBatch.fingerprint == fingerprint, IngestionBatch.status.in_(APPLIED))
    )
