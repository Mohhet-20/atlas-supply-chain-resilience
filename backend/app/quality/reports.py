"""Import-batch and source quality reports built from ingestion row results, findings and scores."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.auth import Role
from app.core.errors import NotFoundError
from app.models.enums import BatchStatus, EntityKind, RowOutcome
from app.models.facility import Facility
from app.models.ingestion import DataQualityFinding, IngestionBatch, IngestionRowResult, RecordLineage
from app.models.product import Product
from app.models.source import DataSource
from app.models.supplier import Supplier
from app.quality import scoring
from app.quality.findings import OPEN_LIKE
from app.quality.service import latest_scores_query
from app.registry.config import QualityConfig

SEV_RANK = {"INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
CLASSES = (
    "IMPORTED",
    "IMPORTED_WITH_WARNINGS",
    "REQUIRES_REVIEW",
    "REJECTED",
    "DUPLICATE_SKIPPED",
    "FAILED",
    "NOT_APPLIED",
)
SOURCE_CONCERN_REJECTION_RATE = 0.20  # a source is flagged when at least 20% of its rows were rejected
SOURCE_CONCERN_ERROR_BATCHES = 2  # ... or when 2 or more of its batches ended with errors / were rejected / failed


def classify(outcome: RowOutcome, findings: list[DataQualityFinding]) -> str:
    rules = {f.rule_id for f in findings}
    worst = max((SEV_RANK[f.severity.value] for f in findings), default=0)
    if "DQ014" in rules:
        return "FAILED"  # the database refused the record (constraint or concurrent change)
    if outcome is RowOutcome.DUPLICATE_IN_BATCH:
        return "DUPLICATE_SKIPPED"
    if outcome is RowOutcome.REJECTED:
        return "REJECTED"
    if outcome is RowOutcome.SKIPPED:
        return "NOT_APPLIED"
    if "DQ011" in rules:
        return "REQUIRES_REVIEW"  # imported, but a possible duplicate needs a human decision
    if worst >= SEV_RANK["WARNING"]:
        return "IMPORTED_WITH_WARNINGS"
    return "IMPORTED"


def batch_report(
    session: Session, batch_id: int, role: Role, cls: str | None, limit: int, offset: int
) -> dict[str, Any]:
    b = session.get(IngestionBatch, batch_id)
    if b is None:
        raise NotFoundError("Batch not found.")
    rows = list(
        session.scalars(
            select(IngestionRowResult)
            .where(IngestionRowResult.batch_id == batch_id)
            .order_by(IngestionRowResult.row_index)
        )
    )
    by_row: dict[int, list[DataQualityFinding]] = defaultdict(list)
    file_level: list[DataQualityFinding] = []
    for f in session.scalars(
        select(DataQualityFinding).where(DataQualityFinding.batch_id == batch_id).order_by(DataQualityFinding.id)
    ):
        (by_row[f.row_index] if f.row_index is not None else file_level).append(f)
    classified = [(r, classify(r.outcome, by_row.get(r.row_index, []))) for r in rows]
    counts = {c: 0 for c in CLASSES}
    for _r, c in classified:
        counts[c] += 1
    sel = [(r, c) for r, c in classified if cls is None or c == cls]
    page = sel[offset : offset + limit]
    can_see_values = role >= Role.EDITOR  # observed cell content may be sensitive; viewers see the finding without it

    def finding_view(f: DataQualityFinding) -> dict[str, Any]:
        return {
            "id": f.id,
            "rule_id": f.rule_id,
            "severity": f.severity.value,
            "field": f.field,
            "message": f.message,
            "suggestion": f.suggestion,
            "observed_value": f.observed_value if can_see_values else None,
            "observed_value_redacted": not can_see_values and f.observed_value is not None,
            "status": f.resolution_status.value,
        }

    touched = list(
        session.execute(
            select(RecordLineage.entity_type, RecordLineage.entity_id)
            .where(RecordLineage.batch_id == batch_id)
            .distinct()
        )
    )
    scores = []
    for kind, eid in touched:
        for sc in latest_scores_query(session, [kind]):
            if sc.subject_id == eid:
                scores.append(sc.score)
    return {
        "batch": {"id": b.id, "batch_uid": b.batch_uid, "file_name": b.file_name, "status": b.status.value, "entity_type": b.entity_type.value, "policy": b.policy.value, "total_rows": b.total_rows, "mapping_profile_ref": b.mapping_profile_ref, "normalization_version": b.normalization_version, "started_at": b.started_at, "finished_at": b.finished_at, "source_id": b.source_id, "source_namespace": b.source.namespace if b.source else None, "failure_reason": b.failure_reason},
        "counts": counts,
        "all_rows_accounted_for": sum(counts.values()) == b.total_rows or b.status in (BatchStatus.FAILED, BatchStatus.PROCESSING),
        "file_level_findings": [finding_view(f) for f in file_level],
        "records_evaluated": scoring.aggregate(scores, len(touched)),
        "rows": {"items": [{"row_index": r.row_index, "source_line": r.source_line, "external_id": r.external_id, "outcome": r.outcome.value, "classification": c, "message": r.message, "entity_type": b.entity_type.value, "entity_id": r.entity_id, "findings": [finding_view(f) for f in by_row.get(r.row_index, [])]} for r, c in page], "total": len(sel), "limit": limit, "offset": offset},
        "classification_rules": "REJECTED/DUPLICATE_IN_BATCH/SKIPPED come from the import outcome; FAILED = DQ014 (database refused the record); REQUIRES_REVIEW = applied with a DQ011 possible-duplicate finding; IMPORTED_WITH_WARNINGS = applied with a WARNING or stronger finding.",
        "raw_payload": "Raw source rows are not stored; only a record fingerprint and truncated observed values (editor role and above).",
    }  # fmt: skip


def source_reports(session: Session, qcfg: QualityConfig, only: int | None = None) -> list[dict[str, Any]]:
    sources = list(session.scalars(select(DataSource).order_by(DataSource.id)))
    if only is not None:
        sources = [s for s in sources if s.id == only]
        if not sources:
            raise NotFoundError("Source not found.")
    scored = latest_scores_query(session)
    by_src: dict[int | None, list[float | None]] = defaultdict(list)
    for sc in scored:
        by_src[sc.source_id].append(sc.score)
    out = []
    for s in sources:
        bs = list(session.scalars(select(IngestionBatch).where(IngestionBatch.source_id == s.id)))
        rows = sum(b.total_rows for b in bs)
        rejected = sum(b.rejected_count for b in bs)
        err_batches = sum(
            1 for b in bs if b.status in (BatchStatus.COMPLETED_WITH_ERRORS, BatchStatus.REJECTED, BatchStatus.FAILED)
        )
        open_f = (
            session.scalar(
                select(func.count())
                .select_from(DataQualityFinding)
                .where(DataQualityFinding.source_id == s.id, DataQualityFinding.resolution_status.in_(OPEN_LIKE))
            )
            or 0
        )
        ents = 0
        traced = 0
        for kind, model in (
            (EntityKind.SUPPLIER, Supplier),
            (EntityKind.FACILITY, Facility),
            (EntityKind.PRODUCT, Product),
        ):
            ids = [
                i
                for (i,) in session.execute(
                    select(model.id).where(model.source_id == s.id, model.merged_into_id.is_(None))
                )
            ]
            ents += len(ids)
            if ids:
                traced += (
                    session.scalar(
                        select(func.count(func.distinct(RecordLineage.entity_id))).where(
                            RecordLineage.entity_type == kind, RecordLineage.entity_id.in_(ids)
                        )
                    )
                    or 0
                )
        agg = scoring.aggregate(by_src.get(s.id, []), ents)
        rate = round(rejected / rows, 4) if rows else None
        reasons = []
        if rate is not None and rate >= SOURCE_CONCERN_REJECTION_RATE:
            reasons.append(
                f"{round(rate * 100)}% of imported rows were rejected (threshold {round(SOURCE_CONCERN_REJECTION_RATE * 100)}%)"
            )
        if err_batches >= SOURCE_CONCERN_ERROR_BATCHES:
            reasons.append(
                f"{err_batches} batches ended with errors, rejection or failure (threshold {SOURCE_CONCERN_ERROR_BATCHES})"
            )
        if agg["mean"] is not None and agg["mean"] < qcfg.low_score_threshold * 100:
            reasons.append(f"mean record score {agg['mean']} is below {round(qcfg.low_score_threshold * 100)}")
        out.append({
            "source": {"id": s.id, "namespace": s.namespace, "name": s.name, "type": s.source_type.value, "provenance": s.provenance.value},
            "indicators": {"batches": len(bs), "batches_with_errors": err_batches, "rows_imported": rows, "rows_rejected": rejected, "rejection_rate": rate, "open_findings": open_f, "entities": ents, "entities_with_lineage": traced, "lineage_coverage": round(traced / ents, 4) if ents else None, "mean_entity_score": agg["mean"], "score_coverage": agg["coverage"]},
            "elevated_concern": bool(reasons), "concern_reasons": reasons,
            "method": "Indicators are measured from import outcomes, findings, lineage and scores. No single reliability number is derived; the concern flag uses fixed documented thresholds.",
        })  # fmt: skip
    return out
