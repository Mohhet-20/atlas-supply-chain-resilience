"""Data-quality storage: evaluation runs, score snapshots, finding history and rule settings.

Findings themselves live in ``data_quality_findings`` (extended, not duplicated): ingestion-time findings and
evaluation findings share one lifecycle, one API and one UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import EntityKind, EvalScope, EvalStatus, ScoreState
from app.models.types import UTCDateTime, enum_column, utcnow


class QualityEvalRun(TenantOwned, Base):
    """One bounded evaluation of a scope. A run never hides partial completion: see ``status``."""

    __tablename__ = "quality_eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    scope_type: Mapped[EvalScope] = mapped_column(enum_column(EvalScope, "eval_scope"))
    scope_ref: Mapped[Any] = mapped_column(JSON, nullable=True)
    scope_key: Mapped[str] = mapped_column(String(120))  # canonical scope identity; one active run per key
    status: Mapped[EvalStatus] = mapped_column(enum_column(EvalStatus, "eval_status"))
    trigger: Mapped[str] = mapped_column(String(16), default="API")  # API | IMPORT | REMEDIATION
    requested_by: Mapped[str] = mapped_column(String(64))
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    catalog_hash: Mapped[str | None] = mapped_column(String(64))
    formula_version: Mapped[str | None] = mapped_column(String(16))
    config_version: Mapped[str | None] = mapped_column(String(32))
    config_hash: Mapped[str | None] = mapped_column(String(64))
    rule_versions: Mapped[Any] = mapped_column(JSON, nullable=True)
    examined: Mapped[Any] = mapped_column(JSON, nullable=True)
    checks_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    findings_created: Mapped[int] = mapped_column(Integer, default=0)
    findings_updated: Mapped[int] = mapped_column(Integer, default=0)
    findings_resolved: Mapped[int] = mapped_column(Integer, default=0)
    findings_reopened: Mapped[int] = mapped_column(Integer, default=0)
    findings_unchanged: Mapped[int] = mapped_column(Integer, default=0)
    subjects_failed: Mapped[int] = mapped_column(Integer, default=0)
    subjects_skipped: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[Any] = mapped_column(JSON, nullable=True)  # at most 50 entries
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    truncation_reasons: Mapped[Any] = mapped_column(JSON, nullable=True)
    aggregate: Mapped[Any] = mapped_column(JSON, nullable=True)  # score summary of the evaluated subjects
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        Index("ix_quality_eval_runs_status", "status"),
        Index("ix_quality_eval_runs_scope", "scope_type", "id"),
        # Concurrency guard: at most one queued/running run per scope identity.
        Index(
            "uq_quality_eval_runs_active_scope",
            "organization_id",
            "scope_key",
            unique=True,
            sqlite_where=text("status IN ('QUEUED','RUNNING')"),
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
    )


class QualityScore(TenantOwned, Base):
    """A reproducible score for one subject at one run. ``score`` is NULL unless ``state`` is SCORED."""

    __tablename__ = "quality_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("quality_eval_runs.id", ondelete="CASCADE"))
    subject_type: Mapped[EntityKind] = mapped_column(enum_column(EntityKind, "score_subject"))
    subject_id: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"))
    state: Mapped[ScoreState] = mapped_column(enum_column(ScoreState, "score_state"))
    score: Mapped[float | None] = mapped_column(Float)
    checks_total: Mapped[int] = mapped_column(Integer)
    checks_failed: Mapped[int] = mapped_column(Integer)
    exceptions_excluded: Mapped[int] = mapped_column(Integer, default=0)
    applicable_weight: Mapped[float] = mapped_column(Float)
    failed_weight: Mapped[float] = mapped_column(Float)
    breakdown: Mapped[Any] = mapped_column(JSON, nullable=True)
    catalog_hash: Mapped[str] = mapped_column(String(64))
    formula_version: Mapped[str] = mapped_column(String(16))
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "subject_type", "subject_id", name="uq_quality_scores_run_subject"),
        Index("ix_quality_scores_subject", "subject_type", "subject_id", "id"),
        Index("ix_quality_scores_source", "source_id"),
    )


class QualityFindingEvent(TenantOwned, Base):
    """Immutable history of a finding (detection, automatic resolution, reviews, remediation, exceptions).

    Rows cannot be updated or deleted (SQLite triggers; migration 0005). This is application-level history, not a
    tamper-proof ledger: privileged database access can still alter it.
    """

    __tablename__ = "quality_finding_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("data_quality_findings.id", ondelete="RESTRICT"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(String(400))
    details: Mapped[Any] = mapped_column(JSON, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_quality_finding_events_finding", "finding_id", "id"),)


class QualityRuleSetting(TenantOwned, Base):
    """Operator override of a catalog rule's enabled flag. Rule logic itself is code, never data."""

    __tablename__ = "quality_rule_settings"

    # Each organisation keeps its own overrides, so the organisation is part of the key.
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id"), primary_key=True, default=1, server_default="1", index=True
    )
    rule_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str | None] = mapped_column(String(300))
    updated_by: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revision: Mapped[int] = mapped_column(Integer, default=1)
