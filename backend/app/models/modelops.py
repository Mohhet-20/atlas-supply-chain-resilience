"""Model evaluation storage: unified model registry, lifecycle history, evaluation runs, explanations and drift diagnostics.

A model version points at its domain source (a forecast run, a lead-time run or a rule-based methodology) by a validated
reference; it never stores code, pickles or filesystem paths. Versions, lifecycle events, explanations and diagnostics are
insert-only (only the lifecycle state and its stamp on a version may change, through the registry). Evaluations are frozen
once terminal, so a correction is a new evaluation and history is never rewritten.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow


class ModelTask(str, Enum):
    FORECASTING = "FORECASTING"
    REGRESSION = "REGRESSION"
    CLASSIFICATION = "CLASSIFICATION"
    RULE_BASED_INDICATOR = "RULE_BASED_INDICATOR"


class ModelState(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    EVALUATED = "EVALUATED"
    APPROVED = "APPROVED"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"


class ModelVersion(TenantOwned, Base):
    __tablename__ = "model_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_uid: Mapped[str] = mapped_column(String(40), unique=True)
    model_key: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(64))
    family: Mapped[str] = mapped_column(String(40))
    task: Mapped[ModelTask] = mapped_column(enum_column(ModelTask, "model_task"))
    domain: Mapped[str] = mapped_column(String(24))  # DEMAND | LEAD_TIME | DELIVERY_RISK | SUPPLIER_RISK
    target: Mapped[str] = mapped_column(String(300))
    lifecycle_state: Mapped[ModelState] = mapped_column(
        enum_column(ModelState, "model_state"), default=ModelState.DEVELOPMENT
    )
    state_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    source_type: Mapped[str] = mapped_column(String(24))  # FORECAST_RUN | LEADTIME_RUN | RISK_METHODOLOGY
    source_id: Mapped[int | None] = mapped_column(Integer)
    source_ref: Mapped[str] = mapped_column(String(64))
    intended_use: Mapped[Any] = mapped_column(JSON)
    prohibited_use: Mapped[Any] = mapped_column(JSON)
    limitations: Mapped[Any] = mapped_column(JSON)
    operating_conditions: Mapped[Any] = mapped_column(JSON)
    feature_schema: Mapped[Any] = mapped_column(JSON, nullable=True)
    preprocessing_version: Mapped[str | None] = mapped_column(String(40))
    data_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    training_window: Mapped[Any] = mapped_column(JSON, nullable=True)
    evaluation_window: Mapped[Any] = mapped_column(JSON, nullable=True)
    algorithm: Mapped[str] = mapped_column(String(60))
    hyperparameters: Mapped[Any] = mapped_column(JSON, nullable=True)
    seed: Mapped[int | None] = mapped_column(Integer)
    artifact_ref: Mapped[str] = mapped_column(String(120))
    artifact_checksum: Mapped[str | None] = mapped_column(String(64))
    code_revision: Mapped[str] = mapped_column(String(64))
    dependency_versions: Mapped[Any] = mapped_column(JSON)
    fallback_behavior: Mapped[str | None] = mapped_column(String(500))
    known_issues: Mapped[Any] = mapped_column(JSON)
    monitoring_requirements: Mapped[Any] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "model_key", "version", name="uq_model_versions_key_version"),
        UniqueConstraint("source_type", "source_ref", name="uq_model_versions_source"),
        Index("ix_model_versions_domain_state", "domain", "lifecycle_state"),
    )


class ModelLifecycleEvent(TenantOwned, Base):
    __tablename__ = "model_lifecycle_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id", ondelete="RESTRICT"))
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(500))
    evidence: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_model_lifecycle_model", "model_version_id", "id"),)


class ModelEvaluation(TenantOwned, Base):
    __tablename__ = "model_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    eval_uid: Mapped[str] = mapped_column(String(40), unique=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(24))  # RECORDED_BACKTEST | PROSPECTIVE
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "model_eval_status"))
    result_status: Mapped[str | None] = mapped_column(
        String(32)
    )  # completed | unsupported | insufficient_data | invalid
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[Any] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(16))
    dataset_ref: Mapped[Any] = mapped_column(JSON, nullable=True)
    split: Mapped[Any] = mapped_column(JSON, nullable=True)
    sample: Mapped[Any] = mapped_column(JSON, nullable=True)
    metrics: Mapped[Any] = mapped_column(JSON, nullable=True)
    baselines: Mapped[Any] = mapped_column(JSON, nullable=True)
    folds: Mapped[Any] = mapped_column(JSON, nullable=True)
    segments: Mapped[Any] = mapped_column(JSON, nullable=True)
    calibration: Mapped[Any] = mapped_column(JSON, nullable=True)
    intervals: Mapped[Any] = mapped_column(JSON, nullable=True)
    validity: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    limitations: Mapped[Any] = mapped_column(JSON, nullable=True)
    reproducibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    software: Mapped[Any] = mapped_column(JSON, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_model_evaluations_idempotency_key"),
        CheckConstraint("kind in ('RECORDED_BACKTEST','PROSPECTIVE')", name="ck_model_evaluations_kind"),
        Index("ix_model_evaluations_model", "model_version_id", "id"),
    )


class ModelExplanation(TenantOwned, Base):
    __tablename__ = "model_explanations"

    id: Mapped[int] = mapped_column(primary_key=True)
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id", ondelete="RESTRICT"))
    scope: Mapped[str] = mapped_column(String(8))  # GLOBAL | LOCAL
    subject_ref: Mapped[str | None] = mapped_column(String(80))
    method: Mapped[str] = mapped_column(String(60))
    method_version: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))  # ok | unsupported
    cache_key: Mapped[str] = mapped_column(String(64))
    result: Mapped[Any] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("model_version_id", "cache_key", name="uq_model_explanations_cache"),
        CheckConstraint("scope in ('GLOBAL','LOCAL')", name="ck_model_explanations_scope"),
    )


class ModelDiagnostic(TenantOwned, Base):
    """One monitoring diagnostic. A drift signal is an observation, never a statement that the model has failed."""

    __tablename__ = "model_diagnostics"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_uid: Mapped[str] = mapped_column(String(40))  # groups the diagnostics of one monitoring check
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(
        String(24)
    )  # DATA_QUALITY | COVARIATE_DRIFT | PREDICTION_DRIFT | TARGET_DRIFT | PERFORMANCE
    feature: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(20))  # ok | warning | alert | insufficient_data | not_applicable
    severity: Mapped[str] = mapped_column(String(10))  # info | warning | critical
    statistic: Mapped[Any] = mapped_column(JSON, nullable=True)
    reference: Mapped[Any] = mapped_column(JSON, nullable=True)
    comparison: Mapped[Any] = mapped_column(JSON, nullable=True)
    explanation: Mapped[str] = mapped_column(String(600))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_model_diagnostics_model", "model_version_id", "id"),)
