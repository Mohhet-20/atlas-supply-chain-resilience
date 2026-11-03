"""Replenishment storage: inventory cost profiles, replenishment/optimisation runs and the insert-only record of decisions about a run.

A cost profile never invents a value: every cost is nullable, and a profile row is immutable except for ``retired_at`` (a changed
cost is a new row, so an old run can always be traced to the exact costs it used). A run stores its request, an input snapshot and
the snapshot hash, and is frozen once terminal. Approval is a separate insert-only row; ATLAS never places an order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance, SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow

REPLENISHMENT_DECISIONS = ("APPROVED", "REJECTED")


class InventoryCostProfile(TenantOwned, Base):
    __tablename__ = "inventory_cost_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_uid: Mapped[str] = mapped_column(String(40), unique=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    currency: Mapped[str] = mapped_column(String(3))
    holding_cost_per_unit_day: Mapped[float | None] = mapped_column(Float)
    ordering_cost: Mapped[float | None] = mapped_column(Float)
    shortage_cost_per_unit: Mapped[float | None] = mapped_column(Float)
    unit_cost: Mapped[float | None] = mapped_column(Float)
    storage_capacity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))  # the unit the per-unit costs and capacity refer to
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "inv_cost_provenance"))
    source_note: Mapped[str | None] = mapped_column(String(300))
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(
            "holding_cost_per_unit_day is null or holding_cost_per_unit_day >= 0", name="ck_invcost_holding"
        ),
        CheckConstraint("ordering_cost is null or ordering_cost >= 0", name="ck_invcost_ordering"),
        CheckConstraint("shortage_cost_per_unit is null or shortage_cost_per_unit >= 0", name="ck_invcost_shortage"),
        CheckConstraint("unit_cost is null or unit_cost >= 0", name="ck_invcost_unit"),
        CheckConstraint("storage_capacity is null or storage_capacity > 0", name="ck_invcost_storage"),
        Index("ix_inv_cost_profiles_pair", "product_id", "facility_id", "effective_from"),
    )


class ReplenishmentRun(TenantOwned, Base):
    __tablename__ = "replenishment_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # OPTIMIZE | SIMULATE
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "replenishment_run_status"))
    result_status: Mapped[str | None] = mapped_column(
        String(24)
    )  # recommended | no_order_needed | infeasible | incomplete | simulated | not_solved
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    product_id: Mapped[int] = mapped_column(Integer)
    facility_id: Mapped[int] = mapped_column(Integer)
    scenario_id: Mapped[int | None] = mapped_column(Integer)
    scenario_hash: Mapped[str | None] = mapped_column(String(64))
    forecast_run_id: Mapped[int | None] = mapped_column(Integer)
    request: Mapped[Any] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # exact inventory, demand, lead-time, cost and constraint inputs used
    algorithm_version: Mapped[str] = mapped_column(String(16))
    position: Mapped[Any] = mapped_column(JSON, nullable=True)
    demand: Mapped[Any] = mapped_column(JSON, nullable=True)
    lead_time: Mapped[Any] = mapped_column(JSON, nullable=True)
    safety_stock: Mapped[Any] = mapped_column(JSON, nullable=True)
    recommendation: Mapped[Any] = mapped_column(JSON, nullable=True)
    projection: Mapped[Any] = mapped_column(JSON, nullable=True)
    evaluation: Mapped[Any] = mapped_column(JSON, nullable=True)  # policy comparison by simulation / backtest
    scenario_results: Mapped[Any] = mapped_column(JSON, nullable=True)
    mitigations: Mapped[Any] = mapped_column(JSON, nullable=True)
    infeasibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    costs: Mapped[Any] = mapped_column(JSON, nullable=True)
    explanation: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
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
        UniqueConstraint("organization_id", "idempotency_key", name="uq_replenishment_runs_idempotency_key"),
        CheckConstraint("kind in ('OPTIMIZE','SIMULATE')", name="ck_replenishment_runs_kind"),
        Index("ix_replenishment_runs_pair", "product_id", "facility_id", "id"),
        Index("ix_replenishment_runs_scenario", "scenario_id", "id"),
        Index("ix_replenishment_runs_status", "status", "id"),
    )


class ReplenishmentDecision(TenantOwned, Base):
    """Insert-only. The latest row for a run is its approval state; ATLAS records the decision and never executes it."""

    __tablename__ = "replenishment_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("replenishment_runs.id", ondelete="RESTRICT"))
    decision: Mapped[str] = mapped_column(String(10))
    rationale: Mapped[str] = mapped_column(String(1000))
    decided_by: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    recommendation_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("decision in ('APPROVED','REJECTED')", name="ck_replenishment_decisions_decision"),
        Index("ix_replenishment_decisions_run", "run_id", "id"),
    )
