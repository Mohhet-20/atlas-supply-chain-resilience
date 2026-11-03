"""Read models for the Data Quality workspace: findings, overview, entity quality, analytics."""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.enums import EntityKind, EvalScope, EvalStatus, FindingOrigin, ResolutionStatus, ScoreState, Severity
from app.models.facility import Facility
from app.models.ingestion import DataQualityFinding, IngestionBatch, RecordLineage
from app.models.product import Product, ProductLink
from app.models.quality import QualityEvalRun, QualityScore
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.quality import scoring
from app.quality.engine import enabled_map
from app.quality.findings import EXCEPTION_LIKE, OPEN_LIKE
from app.quality.framework import catalog
from app.registry.config import QualityConfig
from app.registry.entities import MODELS

# Dimension of the ingestion rules (DQ001-DQ020). Fixed mapping, documented in the rule catalog doc.
LEGACY_DIMENSION = {
    "DQ001": "completeness", "DQ002": "validity", "DQ003": "validity", "DQ004": "validity", "DQ005": "validity",
    "DQ006": "validity", "DQ007": "validity", "DQ008": "completeness", "DQ009": "referential_integrity",
    "DQ010": "uniqueness", "DQ011": "uniqueness", "DQ012": "referential_integrity", "DQ013": "validity",
    "DQ014": "validity", "DQ015": "uniqueness", "DQ016": "validity", "DQ017": "consistency", "DQ018": "validity",
    "DQ019": "validity", "DQ020": "completeness",
}  # fmt: skip
SEV_ORDER = case(
    (DataQualityFinding.severity == Severity.CRITICAL, 4), (DataQualityFinding.severity == Severity.ERROR, 3),
    (DataQualityFinding.severity == Severity.WARNING, 2), else_=1,
)  # fmt: skip
SEV_RANK = {"CRITICAL": 4, "ERROR": 3, "WARNING": 2, "INFO": 1}
ENTITY_TABLE = {EntityKind.SUPPLIER: Supplier, EntityKind.FACILITY: Facility, EntityKind.PRODUCT: Product}
STATUS_GROUPS = {
    "OPEN_ANY": list(OPEN_LIKE),
    "CLOSED_ANY": [ResolutionStatus.RESOLVED],
    "EXCEPTION_ANY": list(EXCEPTION_LIKE),
}


def _dimension_of(f: DataQualityFinding) -> str | None:
    return f.dimension or LEGACY_DIMENSION.get(f.rule_id)


def disabled_rule_ids(session: Session) -> list[str]:
    return [rid for rid, on in enabled_map(session).items() if not on]


def base_where(session: Session, include_disabled: bool = False) -> list[Any]:
    """Findings of rules an operator disabled are excluded from counts and lists unless asked for."""
    if include_disabled:
        return []
    dis = disabled_rule_ids(session)
    return (
        [or_(DataQualityFinding.origin == FindingOrigin.INGESTION, DataQualityFinding.rule_id.not_in(dis))]
        if dis
        else []
    )


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def finding_out(
    f: DataQualityFinding, names: dict[tuple[str, int], str] | None = None, source_names: dict[int, str] | None = None
) -> dict[str, Any]:
    rule = catalog().get(f.rule_id)
    return {
        "id": f.id, "origin": f.origin.value, "rule_id": f.rule_id, "rule_name": rule.name if rule else None, "rule_version": f.rule_version,
        "dimension": _dimension_of(f), "severity": f.severity.value, "status": f.resolution_status.value,
        "entity_type": f.entity_type.value if f.entity_type else None, "entity_id": f.entity_id,
        "entity_name": (names or {}).get((f.entity_type.value, f.entity_id)) if f.entity_type and f.entity_id else None,
        "external_id": f.external_id, "field": f.field, "message": f.message, "suggestion": f.suggestion, "evidence": f.evidence,
        "requires_review": bool(f.requires_review), "batch_id": f.batch_id, "row_index": f.row_index, "source_id": f.source_id,
        "source_namespace": (source_names or {}).get(f.source_id) if f.source_id else None, "run_id": f.run_id,
        "first_detected_at": f.first_detected_at or f.created_at, "last_evaluated_at": f.last_evaluated_at, "resolved_at": f.resolved_at,
        "resolved_by": f.resolved_by, "resolution_note": f.resolution_note, "assigned_to": f.assigned_to, "reopened_count": f.reopened_count,
        "version": f.version, "fix_action": rule.fix_action if rule else None,
    }  # fmt: skip


def _names(session: Session, rows: list[DataQualityFinding]) -> tuple[dict[tuple[str, int], str], dict[int, str]]:
    names: dict[tuple[str, int], str] = {}
    by: dict[EntityKind, set[int]] = defaultdict(set)
    for f in rows:
        if f.entity_type and f.entity_id:
            by[f.entity_type].add(f.entity_id)
    for kind, ids in by.items():
        if kind in ENTITY_TABLE:
            m = ENTITY_TABLE[kind]
            for i, n in session.execute(select(m.id, m.name).where(m.id.in_(ids))):
                names[(kind.value, i)] = n
        elif kind is EntityKind.RELATIONSHIP:
            for i, c in session.execute(
                select(SupplyRelationship.id, SupplyRelationship.relationship_code).where(
                    SupplyRelationship.id.in_(ids)
                )
            ):
                names[(kind.value, i)] = c
        else:
            for i, c in session.execute(select(ProductLink.id, ProductLink.link_code).where(ProductLink.id.in_(ids))):
                names[(kind.value, i)] = c
    srcs = {i: n for i, n in session.execute(select(DataSource.id, DataSource.namespace))}
    return names, srcs


def list_findings(
    session: Session, *, entity_type: EntityKind | None = None, entity_id: int | None = None, severity: Severity | None = None,
    min_severity: Severity | None = None, dimension: str | None = None, status: str | None = None, rule_id: str | None = None,
    source_id: int | None = None, origin: FindingOrigin | None = None, batch_id: int | None = None, q: str | None = None,
    assigned_to: str | None = None, requires_review: bool | None = None, sort: str = "severity", limit: int = 25, offset: int = 0,
    include_disabled: bool = False,
) -> dict[str, Any]:  # fmt: skip
    where = base_where(session, include_disabled)
    if entity_type:
        where.append(DataQualityFinding.entity_type == entity_type)
    if entity_id:
        where.append(DataQualityFinding.entity_id == entity_id)
    if severity:
        where.append(DataQualityFinding.severity == severity)
    if min_severity:
        where.append(SEV_ORDER >= SEV_RANK[min_severity.value])
    if dimension:
        legacy = [r for r, d in LEGACY_DIMENSION.items() if d == dimension]
        where.append(
            or_(
                DataQualityFinding.dimension == dimension,
                and_(DataQualityFinding.dimension.is_(None), DataQualityFinding.rule_id.in_(legacy)),
            )
        )
    if status:
        group = STATUS_GROUPS.get(status)
        where.append(
            DataQualityFinding.resolution_status.in_(group)
            if group
            else DataQualityFinding.resolution_status == ResolutionStatus(status)
        )
    if rule_id:
        where.append(DataQualityFinding.rule_id == rule_id)
    if source_id:
        where.append(DataQualityFinding.source_id == source_id)
    if origin:
        where.append(DataQualityFinding.origin == origin)
    if batch_id:
        where.append(DataQualityFinding.batch_id == batch_id)
    if assigned_to:
        where.append(DataQualityFinding.assigned_to == assigned_to)
    if requires_review is not None:
        where.append(DataQualityFinding.requires_review.is_(requires_review))
    if q:
        pat = _like(q.strip()[:80])
        where.append(
            or_(
                DataQualityFinding.message.ilike(pat, escape="\\"),
                DataQualityFinding.external_id.ilike(pat, escape="\\"),
                DataQualityFinding.rule_id.ilike(pat, escape="\\"),
            )
        )
    stmt = select(DataQualityFinding).where(*where)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = {
        "severity": (SEV_ORDER.desc(), DataQualityFinding.id.desc()), "newest": (DataQualityFinding.id.desc(),), "oldest": (DataQualityFinding.id.asc(),),
        "entity": (DataQualityFinding.entity_type.asc(), DataQualityFinding.entity_id.asc(), DataQualityFinding.id.asc()),
        "rule": (DataQualityFinding.rule_id.asc(), DataQualityFinding.id.asc()),
    }[sort]  # fmt: skip
    rows = list(session.scalars(stmt.order_by(*order).limit(limit).offset(offset)))
    names, srcs = _names(session, rows)
    return {"items": [finding_out(f, names, srcs) for f in rows], "total": total, "limit": limit, "offset": offset}


def finding_counts(session: Session, extra: list[Any] | None = None) -> dict[str, Any]:
    where = base_where(session) + (extra or [])
    open_where = [*where, DataQualityFinding.resolution_status.in_(OPEN_LIKE)]
    total = session.scalar(select(func.count()).select_from(DataQualityFinding).where(*where)) or 0

    def grouped(col, w):
        return {
            (k.value if hasattr(k, "value") else k): n
            for k, n in session.execute(select(col, func.count()).where(*w).group_by(col))
        }

    dim_case = func.coalesce(DataQualityFinding.dimension, "legacy")
    by_dim: dict[str, int] = defaultdict(int)
    for rid, dim, n in session.execute(
        select(DataQualityFinding.rule_id, DataQualityFinding.dimension, func.count())
        .where(*open_where)
        .group_by(DataQualityFinding.rule_id, DataQualityFinding.dimension)
    ):
        by_dim[dim or LEGACY_DIMENSION.get(rid, "validity")] += n
    del dim_case
    sev = grouped(DataQualityFinding.severity, open_where)
    return {
        "total": total,
        "open": session.scalar(select(func.count()).select_from(DataQualityFinding).where(*open_where)) or 0,
        "by_status": grouped(DataQualityFinding.resolution_status, where),
        "open_by_severity": sev,
        "open_by_dimension": dict(by_dim),
        "open_by_entity_type": grouped(DataQualityFinding.entity_type, open_where),
        "critical_or_high_open": sev.get("CRITICAL", 0) + sev.get("ERROR", 0),
        "open_requires_review": session.scalar(select(func.count()).select_from(DataQualityFinding).where(*open_where, DataQualityFinding.requires_review.is_(True))) or 0,
        "accepted_exceptions": session.scalar(select(func.count()).select_from(DataQualityFinding).where(*where, DataQualityFinding.resolution_status.in_(EXCEPTION_LIKE))) or 0,
    }  # fmt: skip


# ------------------------------------------------------------------------------------------------------ scores


def _latest_score_ids(extra: list[Any] | None = None):
    stmt = select(func.max(QualityScore.id)).group_by(QualityScore.subject_type, QualityScore.subject_id)
    return stmt


def latest_scores_query(
    session: Session, kinds: list[EntityKind] | None = None, source_id: int | None = None
) -> list[QualityScore]:
    ids = select(func.max(QualityScore.id)).group_by(QualityScore.subject_type, QualityScore.subject_id)
    stmt = select(QualityScore).where(QualityScore.id.in_(ids))
    if kinds:
        stmt = stmt.where(QualityScore.subject_type.in_(kinds))
    if source_id is not None:
        stmt = stmt.where(QualityScore.source_id == source_id)
    return list(session.scalars(stmt))


def score_out(sc: QualityScore | None) -> dict[str, Any]:
    if sc is None:
        return {"state": "NOT_EVALUATED", "score": None, "message": "No evaluation has covered this record yet."}
    return {
        "state": sc.state.value, "score": sc.score, "checks_total": sc.checks_total, "checks_failed": sc.checks_failed,
        "exceptions_excluded": sc.exceptions_excluded, "applicable_weight": sc.applicable_weight, "failed_weight": sc.failed_weight,
        "breakdown": sc.breakdown, "run_id": sc.run_id, "catalog_hash": sc.catalog_hash, "formula_version": sc.formula_version,
        "evaluated_at": sc.evaluated_at,
        "message": "Not enough applicable checks to calculate a score." if sc.state is ScoreState.INSUFFICIENT_EVIDENCE else None,
    }  # fmt: skip


def run_out(r: QualityEvalRun, full: bool = True) -> dict[str, Any]:
    out = {
        "id": r.id, "run_uid": r.run_uid, "scope_type": r.scope_type.value, "scope_ref": r.scope_ref, "status": r.status.value,
        "trigger": r.trigger, "requested_by": r.requested_by, "queued_at": r.queued_at, "started_at": r.started_at, "finished_at": r.finished_at,
        "duration_ms": r.duration_ms, "examined": r.examined or {}, "checks_evaluated": r.checks_evaluated,
        "findings": {"created": r.findings_created, "updated": r.findings_updated, "resolved": r.findings_resolved, "reopened": r.findings_reopened, "unchanged": r.findings_unchanged},
        "subjects_failed": r.subjects_failed, "subjects_skipped": r.subjects_skipped, "truncated": r.truncated,
        "truncation_reasons": r.truncation_reasons or [], "aggregate": r.aggregate, "error": r.error,
    }  # fmt: skip
    if full:
        out |= {
            "failures": r.failures or [],
            "catalog_hash": r.catalog_hash,
            "formula_version": r.formula_version,
            "config_version": r.config_version,
            "config_hash": r.config_hash,
            "rule_versions": r.rule_versions or {},
        }
    return out


def list_runs(
    session: Session, scope: EvalScope | None, status: EvalStatus | None, limit: int, offset: int
) -> dict[str, Any]:
    stmt = select(QualityEvalRun)
    if scope:
        stmt = stmt.where(QualityEvalRun.scope_type == scope)
    if status:
        stmt = stmt.where(QualityEvalRun.status == status)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = session.scalars(stmt.order_by(QualityEvalRun.id.desc()).limit(limit).offset(offset))
    return {"items": [run_out(r, full=False) for r in rows], "total": total, "limit": limit, "offset": offset}


# ----------------------------------------------------------------------------------------------------- overview


def subject_totals(session: Session) -> dict[str, int]:
    return {
        "SUPPLIER": session.scalar(select(func.count()).select_from(Supplier).where(Supplier.merged_into_id.is_(None)))
        or 0,
        "FACILITY": session.scalar(select(func.count()).select_from(Facility).where(Facility.merged_into_id.is_(None)))
        or 0,
        "PRODUCT": session.scalar(select(func.count()).select_from(Product).where(Product.merged_into_id.is_(None)))
        or 0,
        "RELATIONSHIP": session.scalar(select(func.count()).select_from(SupplyRelationship)) or 0,
        "PRODUCT_LINK": session.scalar(select(func.count()).select_from(ProductLink)) or 0,
    }


def network_score(session: Session) -> dict[str, Any]:
    totals = subject_totals(session)
    rows = latest_scores_query(session)
    # subjects that were merged away or deleted since their last evaluation are not part of the current network
    agg = scoring.aggregate([r.score for r in rows], sum(totals.values()))
    by_kind: dict[str, Any] = {}
    for kind in EntityKind:
        kr = [r for r in rows if r.subject_type is kind]
        by_kind[kind.value] = scoring.aggregate([r.score for r in kr], totals[kind.value])
    return {"overall": agg, "by_subject_type": by_kind}


def overview(session: Session, qcfg: QualityConfig) -> dict[str, Any]:
    latest = session.scalar(select(QualityEvalRun).order_by(QualityEvalRun.id.desc()).limit(1))
    net_run = session.scalar(
        select(QualityEvalRun)
        .where(
            QualityEvalRun.scope_type == EvalScope.NETWORK,
            QualityEvalRun.status.in_([EvalStatus.COMPLETED, EvalStatus.PARTIAL]),
        )
        .order_by(QualityEvalRun.id.desc())
        .limit(1)
    )
    has_eval = latest is not None
    counts = finding_counts(session)
    edge_dims = [
        "relationship_quality",
        "referential_integrity",
        "provenance_completeness",
        "validity",
        "uniqueness",
        "consistency",
    ]
    rel_where = [
        *base_where(session),
        DataQualityFinding.entity_type.in_([EntityKind.RELATIONSHIP, EntityKind.PRODUCT_LINK]),
        DataQualityFinding.resolution_status.in_(OPEN_LIKE),
        DataQualityFinding.origin == FindingOrigin.EVALUATION,
        DataQualityFinding.dimension.in_(edge_dims),
        SEV_ORDER >= 2,
    ]
    rel_issues = (
        session.scalar(
            select(
                func.count(
                    func.distinct(
                        DataQualityFinding.entity_type.cast(String_())
                        + ":"
                        + DataQualityFinding.entity_id.cast(String_())
                    )
                )
            ).where(*rel_where)
        )
        or 0
    )
    batches = session.execute(
        select(
            IngestionBatch.id,
            IngestionBatch.batch_uid,
            IngestionBatch.file_name,
            IngestionBatch.status,
            IngestionBatch.total_rows,
            IngestionBatch.rejected_count,
            IngestionBatch.finding_count,
            IngestionBatch.started_at,
        )
        .where(or_(IngestionBatch.rejected_count > 0, IngestionBatch.finding_count > 0))
        .order_by(IngestionBatch.id.desc())
        .limit(5)
    ).all()
    return {
        "has_evaluation": has_eval,
        "latest_run": run_out(latest, full=False) if latest else None,
        "latest_network_run": run_out(net_run, full=False) if net_run else None,
        "scores": network_score(session) if has_eval else None,
        "findings": counts,
        "relationships_with_issues": rel_issues,
        "batches_with_warnings": [
            {"id": b.id, "batch_uid": b.batch_uid, "file_name": b.file_name, "status": b.status.value, "total_rows": b.total_rows, "rejected": b.rejected_count, "findings": b.finding_count, "started_at": b.started_at}
            for b in batches
        ],
        "totals": subject_totals(session),
        "disclaimer": "Quality scores summarise the checks that exist. A high score does not prove that a record is factually correct.",
        "empty_message": None if has_eval else "No evaluation has been run yet. Run a network evaluation to produce scores and findings.",
    }  # fmt: skip


def String_():  # noqa: N802 - tiny helper to keep the cast readable
    from sqlalchemy import String

    return String()


# ------------------------------------------------------------------------------------------------ entity detail


def entity_quality(session: Session, kind: EntityKind, entity_id: int) -> dict[str, Any]:
    ident: dict[str, Any]
    if kind in ENTITY_TABLE:
        e = session.get(MODELS[_reg(kind)], entity_id)
        if e is None:
            raise NotFoundError("Record not found.")
        ident = {
            "type": kind.value,
            "id": e.id,
            "name": e.name,
            "status": e.status.value,
            "source_id": e.source_id,
            "external_id": e.external_id,
            "merged_into_id": e.merged_into_id,
            "updated_at": e.updated_at,
        }
    elif kind is EntityKind.RELATIONSHIP:
        r = session.get(SupplyRelationship, entity_id)
        if r is None:
            raise NotFoundError("Relationship not found.")
        ident = {
            "type": kind.value,
            "id": r.id,
            "name": r.relationship_code,
            "status": r.status.value,
            "ref": f"SUPPLY:{r.id}",
            "relationship_type": r.relationship_type.value,
        }
    else:
        link = session.get(ProductLink, entity_id)
        if link is None:
            raise NotFoundError("Product link not found.")
        ident = {
            "type": kind.value,
            "id": link.id,
            "name": link.link_code,
            "status": link.status.value,
            "ref": f"PRODUCT:{link.id}",
            "relationship_type": link.link_type.value,
        }
    base = base_where(session)
    scope = [DataQualityFinding.entity_type == kind, DataQualityFinding.entity_id == entity_id]
    rows = list(
        session.scalars(
            select(DataQualityFinding)
            .where(*base, *scope)
            .order_by(SEV_ORDER.desc(), DataQualityFinding.id.desc())
            .limit(200)
        )
    )
    names, srcs = _names(session, rows)
    history = list(
        session.scalars(
            select(QualityScore)
            .where(QualityScore.subject_type == kind, QualityScore.subject_id == entity_id)
            .order_by(QualityScore.id.desc())
            .limit(10)
        )
    )
    latest = history[0] if history else None
    lineage_rows = list(
        session.scalars(
            select(RecordLineage)
            .where(RecordLineage.entity_type == kind, RecordLineage.entity_id == entity_id)
            .order_by(RecordLineage.id.desc())
            .limit(10)
        )
    )
    out: dict[str, Any] = {
        "entity": ident, "score": score_out(latest),
        "open_findings": [finding_out(f, names, srcs) for f in rows if f.resolution_status in OPEN_LIKE],
        "other_findings": [finding_out(f, names, srcs) for f in rows if f.resolution_status not in OPEN_LIKE][:50],
        "history": [{"run_id": h.run_id, "state": h.state.value, "score": h.score, "evaluated_at": h.evaluated_at, "catalog_hash": h.catalog_hash, "comparable_with_latest": h.catalog_hash == (latest.catalog_hash if latest else None)} for h in history],
        "source_records": [{"batch_id": lr.batch_id, "external_id": lr.external_id, "row_index": lr.row_index, "outcome": lr.outcome.value, "changed_fields": lr.changed_fields, "ingested_at": lr.ingested_at, "mapping_profile_ref": lr.mapping_profile_ref} for lr in lineage_rows],
        "related_relationships": _related(session, kind, entity_id) if kind in ENTITY_TABLE else [],
    }  # fmt: skip
    return out


def _reg(kind: EntityKind):
    from app.models.enums import RegistryType

    return RegistryType(kind.value)


def _related(session: Session, kind: EntityKind, eid: int) -> list[dict[str, Any]]:
    recs: list[tuple[EntityKind, int, str, str, str]] = []
    if kind is EntityKind.SUPPLIER:
        s_where = [SupplyRelationship.supplier_id == eid]
        l_where = [ProductLink.supplier_id == eid]
    elif kind is EntityKind.FACILITY:
        s_where = [or_(SupplyRelationship.source_facility_id == eid, SupplyRelationship.target_facility_id == eid)]
        l_where = [ProductLink.facility_id == eid]
    else:
        s_where = [SupplyRelationship.id < 0]
        l_where = [ProductLink.product_id == eid]
    for r in session.scalars(select(SupplyRelationship).where(*s_where).limit(50)).unique():
        recs.append((EntityKind.RELATIONSHIP, r.id, f"SUPPLY:{r.id}", r.relationship_type.value, r.status.value))
    for link in session.scalars(select(ProductLink).where(*l_where).limit(50)).unique():
        recs.append((EntityKind.PRODUCT_LINK, link.id, f"PRODUCT:{link.id}", link.link_type.value, link.status.value))
    base = base_where(session)
    out = []
    for k, rid, ref, typ, status in recs[:60]:
        sc = session.scalar(
            select(QualityScore)
            .where(QualityScore.subject_type == k, QualityScore.subject_id == rid)
            .order_by(QualityScore.id.desc())
            .limit(1)
        )
        n_open = (
            session.scalar(
                select(func.count())
                .select_from(DataQualityFinding)
                .where(
                    *base,
                    DataQualityFinding.entity_type == k,
                    DataQualityFinding.entity_id == rid,
                    DataQualityFinding.resolution_status.in_(OPEN_LIKE),
                )
            )
            or 0
        )
        out.append(
            {
                "ref": ref,
                "entity_type": k.value,
                "entity_id": rid,
                "relationship_type": typ,
                "status": status,
                "score": score_out(sc),
                "open_findings": n_open,
            }
        )
    return out


# ----------------------------------------------------------------------------------------------------- analytics


def analytics(session: Session, qcfg: QualityConfig) -> dict[str, Any]:
    base = base_where(session)
    srcs = {i: n for i, n in session.execute(select(DataSource.id, DataSource.namespace))}
    by_source: dict[Any, dict[str, int]] = defaultdict(lambda: {"total": 0, "open": 0})
    for sid, status, n in session.execute(
        select(DataQualityFinding.source_id, DataQualityFinding.resolution_status, func.count())
        .where(*base)
        .group_by(DataQualityFinding.source_id, DataQualityFinding.resolution_status)
    ):
        by_source[sid]["total"] += n
        if status in OPEN_LIKE:
            by_source[sid]["open"] += n
    runs = list(
        session.scalars(
            select(QualityEvalRun)
            .where(
                QualityEvalRun.scope_type == EvalScope.NETWORK,
                QualityEvalRun.status.in_([EvalStatus.COMPLETED, EvalStatus.PARTIAL]),
            )
            .order_by(QualityEvalRun.id.desc())
            .limit(20)
        )
    )[::-1]
    trend, prev = [], None
    for r in runs:
        agg = r.aggregate or {}
        trend.append(
            {
                "run_id": r.id,
                "finished_at": r.finished_at,
                "status": r.status.value,
                "mean": agg.get("mean"),
                "coverage": agg.get("coverage"),
                "subjects": agg.get("subjects"),
                "catalog_hash": r.catalog_hash,
                "comparable_with_previous": None if prev is None else prev == r.catalog_hash,
            }
        )
        prev = r.catalog_hash
    repeat = [
        {
            "finding_id": f.id,
            "rule_id": f.rule_id,
            "entity_type": f.entity_type.value if f.entity_type else None,
            "entity_id": f.entity_id,
            "reopened_count": f.reopened_count,
        }
        for f in session.scalars(
            select(DataQualityFinding)
            .where(*base, DataQualityFinding.reopened_count > 0)
            .order_by(DataQualityFinding.reopened_count.desc(), DataQualityFinding.id)
            .limit(10)
        )
    ]
    top_rules = [
        {"rule_id": rid, "rule_name": (catalog().get(rid).name if rid in catalog() else None), "open_findings": n}
        for rid, n in session.execute(
            select(DataQualityFinding.rule_id, func.count())
            .where(*base, DataQualityFinding.resolution_status.in_(OPEN_LIKE))
            .group_by(DataQualityFinding.rule_id)
            .order_by(func.count().desc(), DataQualityFinding.rule_id)
            .limit(10)
        )
    ]
    now = utcnow()
    fresh_cut, stale_cut = now - timedelta(days=qcfg.fresh_days), now - timedelta(days=qcfg.stale_days)
    stale: dict[str, Any] = {}
    for kind, model in ENTITY_TABLE.items():
        live = model.merged_into_id.is_(None)
        total = session.scalar(select(func.count()).select_from(model).where(live)) or 0
        fresh = session.scalar(select(func.count()).select_from(model).where(live, model.updated_at >= fresh_cut)) or 0
        old = session.scalar(select(func.count()).select_from(model).where(live, model.updated_at < stale_cut)) or 0
        stale[kind.value] = {"total": total, "fresh": fresh, "aging": total - fresh - old, "stale": old}
    resolved = list(
        session.execute(
            select(DataQualityFinding.first_detected_at, DataQualityFinding.resolved_at).where(
                *base,
                DataQualityFinding.origin == FindingOrigin.EVALUATION,
                DataQualityFinding.resolution_status == ResolutionStatus.RESOLVED,
                DataQualityFinding.resolved_at.is_not(None),
                DataQualityFinding.first_detected_at.is_not(None),
            )
        )
    )
    hours = [max(0.0, (b - a).total_seconds() / 3600) for a, b in resolved]
    rel_rows = latest_scores_query(session, [EntityKind.RELATIONSHIP, EntityKind.PRODUCT_LINK])
    rel_total = subject_totals(session)
    from app.quality.reports import source_reports

    sources = source_reports(session, qcfg)
    return {
        "findings_by_source": [{"source_id": sid, "source_namespace": srcs.get(sid) if sid else None, **v} for sid, v in sorted(by_source.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))],
        "findings_by_entity_type": finding_counts(session)["open_by_entity_type"],
        "findings_by_dimension": finding_counts(session)["open_by_dimension"],
        "trend": trend,
        "repeated_failures": {"reopened_findings": repeat, "top_rules_by_open_findings": top_rules},
        "stale_distribution": {"fresh_days": qcfg.fresh_days, "stale_days": qcfg.stale_days, "by_entity_type": stale},
        "relationship_quality_distribution": scoring.aggregate([r.score for r in rel_rows], rel_total["RELATIONSHIP"] + rel_total["PRODUCT_LINK"]),
        "time_to_resolve_hours": {"n": len(hours), "median": round(statistics.median(hours), 2) if hours else None, "mean": round(sum(hours) / len(hours), 2) if hours else None},
        "sources_with_recurring_import_failures": [s for s in sources if s["indicators"]["batches_with_errors"] >= 1],
        "definitions": {
            "open": "Findings in OPEN, ACKNOWLEDGED, IN_REMEDIATION or REOPENED.",
            "trend": "Network-scope runs only, newest 20; comparable_with_previous is false when rules, thresholds or the formula changed between runs.",
            "stale_distribution": "Live registry records by days since last update; denominators are all live records of the type.",
            "time_to_resolve": "Hours from first detection to resolution for evaluation findings with status RESOLVED (exceptions excluded). n=0 means no measurement, not zero hours.",
            "missing_vs_zero": "null means not measured; 0 means measured and none found.",
        },
    }  # fmt: skip
