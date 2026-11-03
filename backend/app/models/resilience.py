"""Resilience storage: multi-objective resilience optimisation runs and the insert-only record of decisions about a run.

A run stores its request, an input snapshot and hash, the objective/constraint/scenario definitions it used and every result block, and is
frozen once terminal. Approval is a separate insert-only row naming the alternative that was chosen; ATLAS never executes it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow

RESILIENCE_DECISIONS = ("APPROVED", "REJECTED")


class ResilienceRun(TenantOwned, Base):
    __tablename__ = "resilience_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "resilience_run_status"))
    # completed | completed_with_warnings | infeasible | timed_out | invalid_input | insufficient_data | failed
    result_status: Mapped[str | None] = mapped_column(String(32))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    product_id: Mapped[int] = mapped_column(Integer)
    destination_facility_id: Mapped[int] = mapped_column(Integer)
    strategy: Mapped[str] = mapped_column(String(24))
    scenario_mode: Mapped[str] = mapped_column(String(10))
    request: Mapped[Any] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[Any] = mapped_column(JSON, nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(16))
    objectives: Mapped[Any] = mapped_column(JSON, nullable=True)
    constraints: Mapped[Any] = mapped_column(JSON, nullable=True)
    scenarios: Mapped[Any] = mapped_column(JSON, nullable=True)
    alternatives: Mapped[Any] = mapped_column(JSON, nullable=True)
    pareto: Mapped[Any] = mapped_column(JSON, nullable=True)
    recommendation: Mapped[Any] = mapped_column(JSON, nullable=True)
    comparison: Mapped[Any] = mapped_column(JSON, nullable=True)
    sensitivity: Mapped[Any] = mapped_column(JSON, nullable=True)
    diagnostics: Mapped[Any] = mapped_column(JSON, nullable=True)
    explanation: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    solver: Mapped[Any] = mapped_column(JSON, nullable=True)
    statistics: Mapped[Any] = mapped_column(JSON, nullable=True)
    provenance: Mapped[Any] = mapped_column(JSON, nullable=True)
    reproducibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    recommendation_hash: Mapped[str | None] = mapped_column(String(64))
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_resilience_runs_idempotency_key"),
        Index("ix_resilience_runs_scope", "product_id", "destination_facility_id", "id"),
        Index("ix_resilience_runs_status", "status", "id"),
    )


class ResilienceDecision(TenantOwned, Base):
    """Insert-only. The latest row for a run is its approval state; ATLAS records the decision and never executes it."""

    __tablename__ = "resilience_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("resilience_runs.id", ondelete="RESTRICT"))
    decision: Mapped[str] = mapped_column(String(10))
    alternative_id: Mapped[str | None] = mapped_column(String(16))
    rationale: Mapped[str] = mapped_column(String(1000))
    decided_by: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    recommendation_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("decision in ('APPROVED','REJECTED')", name="ck_resilience_decisions_decision"),
        Index("ix_resilience_decisions_run", "run_id", "id"),
    )
