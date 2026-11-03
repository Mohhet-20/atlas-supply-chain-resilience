"""Forecasting storage: forecast runs, their values, forecast-vs-actual evaluations and monitoring alerts.

A run is immutable once it reaches a terminal status; values, evaluations and alerts are insert-only; runs are never
deleted. A forecast is a MODEL_ESTIMATE, never an observation. Series are derived views over ``demand_observations``
(there is no second demand store).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
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
from app.models.enums import SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow


class ForecastRun(TenantOwned, Base):
    __tablename__ = "forecast_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "forecast_run_status"))
    result_status: Mapped[str | None] = mapped_column(
        String(40)
    )  # completed | completed_with_fallback | insufficient_data | ...
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    frequency: Mapped[str] = mapped_column(String(10))
    horizon: Mapped[int] = mapped_column(Integer)
    unit: Mapped[str | None] = mapped_column(String(32))
    origin: Mapped[date | None] = mapped_column(Date)
    config: Mapped[Any] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(16))
    data_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    selected_model: Mapped[str | None] = mapped_column(String(40))
    model_version: Mapped[str | None] = mapped_column(String(16))
    model_params: Mapped[Any] = mapped_column(JSON, nullable=True)
    training_interval: Mapped[Any] = mapped_column(JSON, nullable=True)
    preparation: Mapped[Any] = mapped_column(JSON, nullable=True)
    diagnostics: Mapped[Any] = mapped_column(JSON, nullable=True)
    patterns: Mapped[Any] = mapped_column(JSON, nullable=True)
    candidates: Mapped[Any] = mapped_column(JSON, nullable=True)
    validation: Mapped[Any] = mapped_column(JSON, nullable=True)
    evaluation: Mapped[Any] = mapped_column(JSON, nullable=True)
    selection: Mapped[Any] = mapped_column(JSON, nullable=True)
    interval_calibration: Mapped[Any] = mapped_column(JSON, nullable=True)
    fallback: Mapped[Any] = mapped_column(JSON, nullable=True)
    clamping: Mapped[Any] = mapped_column(JSON, nullable=True)
    reproducibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    timings: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    failures: Mapped[Any] = mapped_column(JSON, nullable=True)
    provenance: Mapped[str] = mapped_column(String(20), default="MODEL_ESTIMATE", server_default="MODEL_ESTIMATE")
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_forecast_runs_idempotency_key"),
        Index("ix_forecast_runs_status", "status"),
        Index("ix_forecast_runs_series", "product_id", "facility_id", "queued_at"),
        CheckConstraint("horizon >= 1", name="horizon_positive"),
    )


class ForecastValue(TenantOwned, Base):
    """One forecast period. Unrounded; ``point`` is after non-negative clamping, ``point_raw`` before."""

    __tablename__ = "forecast_values"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id", ondelete="RESTRICT"))
    step: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)  # exclusive
    point: Mapped[float] = mapped_column(Float)
    point_raw: Mapped[float] = mapped_column(Float)
    intervals: Mapped[Any] = mapped_column(JSON)  # [{level, lower, upper, status, n}] — bounds only where supported

    __table_args__ = (
        UniqueConstraint("run_id", "step", name="uq_forecast_values_step"),
        Index("ix_forecast_values_period", "run_id", "period_start"),
    )


class ForecastEvaluation(TenantOwned, Base):
    """A snapshot of forecast-versus-actual matching at a point in time. The issued forecast is never rewritten."""

    __tablename__ = "forecast_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id", ondelete="RESTRICT"))
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    requested_by: Mapped[str] = mapped_column(String(64))
    actuals_hash: Mapped[str] = mapped_column(String(64))
    n_matched: Mapped[int] = mapped_column(Integer)
    n_final: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))  # evaluated | no_actuals | insufficient_evidence
    metrics: Mapped[Any] = mapped_column(JSON, nullable=True)
    matches: Mapped[Any] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "actuals_hash", name="uq_forecast_evaluations_snapshot"),
        Index("ix_forecast_evaluations_run", "run_id", "evaluated_at"),
    )


class ForecastAlert(TenantOwned, Base):
    """A monitoring signal. Insert-only and de-duplicated by fingerprint; nothing here retrains or changes a forecast."""

    __tablename__ = "forecast_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(
        String(30)
    )  # deterioration | sustained_bias | stale_forecast | input_quality_change
    severity: Mapped[str] = mapped_column(String(10))  # INFO | WARNING
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id", ondelete="RESTRICT"))
    message: Mapped[str] = mapped_column(String(500))
    evidence: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "fingerprint", name="uq_forecast_alerts_org_fingerprint"),
        Index("ix_forecast_alerts_series", "product_id", "facility_id", "created_at"),
    )
