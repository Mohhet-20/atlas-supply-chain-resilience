"""Supplier-risk storage: assessment runs, immutable assessments, factors, evidence, reviews and alerts.

Assessments are never overwritten: a recalculation that changes anything inserts a new row and marks the previous one
superseded. Reviews (acknowledgements, notes, overrides) are append-only and never touch the automated result.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import (
    AssessmentStatus,
    EvalStatus,
    EvidenceType,
    FactorBasis,
    FactorState,
    Freshness,
    ReliabilityBand,
    ReviewAction,
    RiskAlertStatus,
    RiskAlertType,
    RiskCategory,
    RiskClass,
    RiskRunScope,
    Severity,
)
from app.models.types import UTCDateTime, enum_column, utcnow


class RiskRun(TenantOwned, Base):
    """One bounded assessment run. Outcomes are counted per supplier; nothing skipped or failed is hidden."""

    __tablename__ = "risk_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    scope_type: Mapped[RiskRunScope] = mapped_column(enum_column(RiskRunScope, "risk_run_scope"))
    scope_ref: Mapped[Any] = mapped_column(JSON, nullable=True)
    scope_key: Mapped[str] = mapped_column(String(120))
    status: Mapped[EvalStatus] = mapped_column(enum_column(EvalStatus, "risk_run_status"))
    trigger: Mapped[str] = mapped_column(String(16), default="API")  # API | AUTO | METHODOLOGY
    reason: Mapped[str | None] = mapped_column(String(200))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    methodology_version: Mapped[str | None] = mapped_column(String(16))
    methodology_hash: Mapped[str | None] = mapped_column(String(64))
    graph_snapshot: Mapped[Any] = mapped_column(JSON, nullable=True)
    examined: Mapped[int] = mapped_column(Integer, default=0)
    assessed: Mapped[int] = mapped_column(Integer, default=0)  # status ASSESSED
    partial: Mapped[int] = mapped_column(Integer, default=0)  # status PARTIAL
    insufficient: Mapped[int] = mapped_column(Integer, default=0)  # status INSUFFICIENT_EVIDENCE
    created: Mapped[int] = mapped_column(Integer, default=0)  # new assessment rows written
    unchanged: Mapped[int] = mapped_column(Integer, default=0)  # recomputed identically; no new row
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    alerts_created: Mapped[int] = mapped_column(Integer, default=0)
    alerts_updated: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[Any] = mapped_column(JSON, nullable=True)  # at most 50 entries
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    truncation_reasons: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_risk_runs_idempotency_key"),
        Index("ix_risk_runs_status", "status"),
        Index("ix_risk_runs_scope", "scope_type", "id"),
        Index(
            "uq_risk_runs_active_scope",
            "organization_id",
            "scope_key",
            unique=True,
            sqlite_where=text("status IN ('QUEUED','RUNNING')"),
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
    )


class RiskAssessment(TenantOwned, Base):
    __tablename__ = "risk_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_uid: Mapped[str] = mapped_column(String(40), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    run_id: Mapped[int] = mapped_column(ForeignKey("risk_runs.id", ondelete="RESTRICT"))
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime)  # when the assessment was computed
    status: Mapped[AssessmentStatus] = mapped_column(enum_column(AssessmentStatus, "assessment_status"))
    methodology_version: Mapped[str] = mapped_column(String(16))
    methodology_hash: Mapped[str] = mapped_column(String(64))
    risk_score: Mapped[float | None] = mapped_column(Float)  # NULL unless a score is justified; never 0 for "unknown"
    classification: Mapped[RiskClass | None] = mapped_column(enum_column(RiskClass, "assessment_classification"))
    evidence_coverage: Mapped[float] = mapped_column(Float)
    effective_weight: Mapped[float] = mapped_column(Float)
    reliability: Mapped[float | None] = mapped_column(Float)
    reliability_band: Mapped[ReliabilityBand | None] = mapped_column(
        enum_column(ReliabilityBand, "assessment_reliability")
    )
    scored_factors: Mapped[int] = mapped_column(Integer)
    contributions: Mapped[Any] = mapped_column(JSON, nullable=True)  # key contributing factors
    missing: Mapped[Any] = mapped_column(JSON, nullable=True)  # key missing-data indicators
    actions: Mapped[Any] = mapped_column(JSON, nullable=True)  # recommended analyst review actions
    data_basis: Mapped[Any] = mapped_column(JSON, nullable=True)  # provenance mix, synthetic flag, edge snapshot
    fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_latest_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # when the newest evidence was observed
    evidence_earliest_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    review_state: Mapped[str] = mapped_column(String(24), default="UNREVIEWED", server_default="UNREVIEWED")
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    superseded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("risk_assessments.id", ondelete="RESTRICT"))
    last_confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_confirmed_run_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "supplier_id", name="uq_risk_assessments_run_supplier"),
        CheckConstraint("risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)", name="score_range"),
        CheckConstraint("evidence_coverage >= 0 AND evidence_coverage <= 1", name="coverage_range"),
        CheckConstraint("reliability IS NULL OR (reliability >= 0 AND reliability <= 1)", name="reliability_range"),
        # An unknown risk can never be stored as a number or a classification.
        CheckConstraint(
            "status <> 'INSUFFICIENT_EVIDENCE' OR (risk_score IS NULL AND classification IS NULL)",
            name="insufficient_has_no_score",
        ),
        CheckConstraint("status = 'INSUFFICIENT_EVIDENCE' OR risk_score IS NOT NULL", name="scored_has_score"),
        CheckConstraint("classification IS NULL OR status = 'ASSESSED'", name="classified_only_when_assessed"),
        Index("ix_risk_assessments_supplier", "supplier_id", "id"),
        Index("ix_risk_assessments_status", "status"),
        Index("ix_risk_assessments_classification", "classification"),
        Index("ix_risk_assessments_current", "is_current", "risk_score"),
        Index("ix_risk_assessments_run", "run_id"),
        Index(
            "uq_risk_assessments_current_supplier",
            "supplier_id",
            unique=True,
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current"),
        ),
    )


class RiskFactor(TenantOwned, Base):
    __tablename__ = "risk_factors"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("risk_assessments.id", ondelete="CASCADE"))
    category: Mapped[RiskCategory] = mapped_column(enum_column(RiskCategory, "factor_category"))
    role: Mapped[str] = mapped_column(String(12))  # risk | reliability
    state: Mapped[FactorState] = mapped_column(enum_column(FactorState, "factor_state"))
    basis: Mapped[FactorBasis] = mapped_column(enum_column(FactorBasis, "factor_basis"))
    score: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[RiskClass | None] = mapped_column(enum_column(RiskClass, "factor_severity"))
    weight: Mapped[float] = mapped_column(Float)
    explanation: Mapped[str] = mapped_column(String(800))
    unavailable_reason: Mapped[str | None] = mapped_column(String(400))
    missing_inputs: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    freshness: Mapped[Freshness] = mapped_column(enum_column(Freshness, "factor_freshness"))
    confidence: Mapped[float | None] = mapped_column(Float)
    quality_context: Mapped[Any] = mapped_column(JSON, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    details: Mapped[Any] = mapped_column(JSON, nullable=True)
    method_version: Mapped[str] = mapped_column(String(16))

    __table_args__ = (
        UniqueConstraint("assessment_id", "category", name="uq_risk_factors_assessment_category"),
        CheckConstraint("(state = 'SCORED') = (score IS NOT NULL)", name="score_iff_scored"),
        CheckConstraint("score IS NULL OR (score >= 0 AND score <= 100)", name="score_range"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
        Index("ix_risk_factors_assessment", "assessment_id"),
        Index("ix_risk_factors_category_state", "category", "state"),
    )


class RiskEvidence(TenantOwned, Base):
    """A traceable reference from a factor to the record that supports it, with the reasoning that connects them."""

    __tablename__ = "risk_evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("risk_assessments.id", ondelete="CASCADE"))
    factor_id: Mapped[int] = mapped_column(ForeignKey("risk_factors.id", ondelete="CASCADE"))
    evidence_type: Mapped[EvidenceType] = mapped_column(enum_column(EvidenceType, "evidence_type"))
    ref: Mapped[str] = mapped_column(String(80))
    summary: Mapped[str] = mapped_column(String(400))
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    provenance: Mapped[str | None] = mapped_column(String(24))
    source_namespace: Mapped[str | None] = mapped_column(String(80))
    reasoning: Mapped[str] = mapped_column(String(400))

    __table_args__ = (
        UniqueConstraint("factor_id", "evidence_type", "ref", name="uq_risk_evidence_factor_ref"),
        Index("ix_risk_evidence_assessment", "assessment_id"),
        Index("ix_risk_evidence_ref", "evidence_type", "ref"),
    )


class RiskReview(TenantOwned, Base):
    """Append-only analyst decision. An override never replaces the automated result stored on the assessment."""

    __tablename__ = "risk_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    assessment_id: Mapped[int] = mapped_column(ForeignKey("risk_assessments.id", ondelete="RESTRICT"))
    action: Mapped[ReviewAction] = mapped_column(enum_column(ReviewAction, "review_action"))
    factor_category: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    note: Mapped[str | None] = mapped_column(String(1000))
    override_classification: Mapped[RiskClass | None] = mapped_column(enum_column(RiskClass, "review_override_class"))
    override_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    target_review_id: Mapped[int | None] = mapped_column(Integer)
    # The automated result at the moment of the review, copied so it can be shown next to the decision forever.
    original_status: Mapped[str] = mapped_column(String(24))
    original_classification: Mapped[str | None] = mapped_column(String(16))
    original_score: Mapped[float | None] = mapped_column(Float)
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint(
            "action <> 'OVERRIDE' OR (override_classification IS NOT NULL AND note IS NOT NULL AND length(trim(note)) > 0)",
            name="override_needs_class_and_note",
        ),
        CheckConstraint("action <> 'REVOKE_OVERRIDE' OR target_review_id IS NOT NULL", name="revoke_needs_target"),
        CheckConstraint("action = 'OVERRIDE' OR override_classification IS NULL", name="class_only_on_override"),
        Index("ix_risk_reviews_supplier", "supplier_id", "id"),
        Index("ix_risk_reviews_assessment", "assessment_id", "id"),
    )


class RiskAlert(TenantOwned, Base):
    """In-application review queue entry. No external notification is sent by ATLAS."""

    __tablename__ = "risk_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_key: Mapped[str] = mapped_column(String(120))
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    assessment_id: Mapped[int | None] = mapped_column(ForeignKey("risk_assessments.id", ondelete="RESTRICT"))
    alert_type: Mapped[RiskAlertType] = mapped_column(enum_column(RiskAlertType, "risk_alert_type"))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, "risk_alert_severity"))
    status: Mapped[RiskAlertStatus] = mapped_column(enum_column(RiskAlertStatus, "risk_alert_status"))
    reason: Mapped[str] = mapped_column(String(400))
    evidence: Mapped[Any] = mapped_column(JSON, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolution_note: Mapped[str | None] = mapped_column(String(400))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        Index("ix_risk_alerts_supplier", "supplier_id", "id"),
        Index("ix_risk_alerts_status", "status", "alert_type"),
        # Deduplication: one live alert per key; repeats bump ``occurrences`` instead of inserting.
        Index(
            "uq_risk_alerts_active_key",
            "organization_id",
            "alert_key",
            unique=True,
            sqlite_where=text("status IN ('OPEN','ACKNOWLEDGED')"),
            postgresql_where=text("status IN ('OPEN','ACKNOWLEDGED')"),
        ),
    )
