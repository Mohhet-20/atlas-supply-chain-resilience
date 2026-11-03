"""Sourcing storage: supplier offers (the commercial terms behind a supplier-product-destination relationship), sourcing runs
and the insert-only record of human decisions about a run.

An offer never invents a value: price, capacity, lead time, defect rate and freight are nullable and a missing value stays
visibly missing. Offer rows are immutable except for their availability status; a changed price or capacity is a new offer (the
old one is retired), so a past run can always be traced to the exact terms it used. A sourcing run is frozen once terminal.
Approval is a separate insert-only decision row: a run is never edited to become "approved", and ATLAS never places an order.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance, SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow

APPROVALS = ("APPROVED", "PENDING_QUALIFICATION", "DISQUALIFIED", "UNKNOWN")
OFFER_STATUSES = ("ACTIVE", "SUSPENDED", "RETIRED")
QUALITY = ("VERIFIED", "ESTIMATED", "UNKNOWN")
DECISIONS = ("APPROVED", "REJECTED")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} in ({','.join(repr(v) for v in values)})"


class SourcingOffer(TenantOwned, Base):
    __tablename__ = "sourcing_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_uid: Mapped[str] = mapped_column(String(40), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    destination_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    approval_status: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    unit: Mapped[str | None] = mapped_column(String(32))  # unit the quantities below are expressed in
    unit_price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    price_quality: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    freight_per_unit: Mapped[float | None] = mapped_column(Float)  # explicit, same currency; NULL = unknown, never zero
    capacity_amount: Mapped[float | None] = mapped_column(Float)  # per requirement period, in `unit`
    capacity_quality: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    capacity_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    min_order_quantity: Mapped[float | None] = mapped_column(Float)
    lot_size: Mapped[float | None] = mapped_column(Float)  # order increment
    max_allocation: Mapped[float | None] = mapped_column(Float)  # contractual ceiling
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    lead_time_std_days: Mapped[float | None] = mapped_column(Float)
    lead_time_quality: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    defect_rate: Mapped[float | None] = mapped_column(Float)  # fraction 0..1
    quality_quality: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    contract_valid_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    contract_valid_to: Mapped[datetime | None] = mapped_column(UTCDateTime)
    restrictions: Mapped[Any] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "sourcing_offer_provenance"))
    source_note: Mapped[str | None] = mapped_column(String(300))
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint(_in("approval_status", APPROVALS), name="ck_sourcing_offers_approval"),
        CheckConstraint(_in("status", OFFER_STATUSES), name="ck_sourcing_offers_status"),
        CheckConstraint(_in("price_quality", QUALITY), name="ck_sourcing_offers_price_quality"),
        CheckConstraint(_in("capacity_quality", QUALITY), name="ck_sourcing_offers_capacity_quality"),
        CheckConstraint(_in("lead_time_quality", QUALITY), name="ck_sourcing_offers_lead_quality"),
        CheckConstraint(_in("quality_quality", QUALITY), name="ck_sourcing_offers_quality_quality"),
        CheckConstraint("unit_price IS NULL OR unit_price >= 0", name="ck_sourcing_offers_price"),
        CheckConstraint("freight_per_unit IS NULL OR freight_per_unit >= 0", name="ck_sourcing_offers_freight"),
        CheckConstraint("(unit_price IS NULL) = (currency IS NULL)", name="ck_sourcing_offers_price_currency"),
        CheckConstraint("capacity_amount IS NULL OR capacity_amount >= 0", name="ck_sourcing_offers_capacity"),
        CheckConstraint("min_order_quantity IS NULL OR min_order_quantity > 0", name="ck_sourcing_offers_moq"),
        CheckConstraint("lot_size IS NULL OR lot_size > 0", name="ck_sourcing_offers_lot"),
        CheckConstraint("max_allocation IS NULL OR max_allocation >= 0", name="ck_sourcing_offers_max_alloc"),
        CheckConstraint("lead_time_days IS NULL OR lead_time_days >= 0", name="ck_sourcing_offers_lead"),
        CheckConstraint("lead_time_std_days IS NULL OR lead_time_std_days >= 0", name="ck_sourcing_offers_lead_std"),
        CheckConstraint(
            "defect_rate IS NULL OR (defect_rate >= 0 AND defect_rate <= 1)", name="ck_sourcing_offers_defect"
        ),
        CheckConstraint(
            "contract_valid_to IS NULL OR contract_valid_from IS NULL OR contract_valid_to >= contract_valid_from",
            name="ck_sourcing_offers_contract_window",
        ),
        Index("ix_sourcing_offers_pair", "product_id", "destination_facility_id", "status"),
        Index("ix_sourcing_offers_supplier", "supplier_id", "status"),
    )


class SourcingRun(TenantOwned, Base):
    __tablename__ = "sourcing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # OPTIMIZE | COMPARE
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "sourcing_run_status"))
    result_status: Mapped[str | None] = mapped_column(String(24))  # feasible | partial | infeasible | incomplete
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    product_id: Mapped[int] = mapped_column(Integer)
    destination_facility_id: Mapped[int] = mapped_column(Integer)
    scenario_id: Mapped[int | None] = mapped_column(Integer)
    scenario_hash: Mapped[str | None] = mapped_column(String(64))
    request: Mapped[Any] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[Any] = mapped_column(JSON, nullable=True)  # the exact candidate terms the optimizer saw
    algorithm_version: Mapped[str] = mapped_column(String(16))
    candidates: Mapped[Any] = mapped_column(JSON, nullable=True)
    allocations: Mapped[Any] = mapped_column(JSON, nullable=True)
    baseline_allocations: Mapped[Any] = mapped_column(JSON, nullable=True)
    comparison: Mapped[Any] = mapped_column(JSON, nullable=True)
    infeasibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    metrics: Mapped[Any] = mapped_column(JSON, nullable=True)
    disruptions: Mapped[Any] = mapped_column(JSON, nullable=True)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    provenance: Mapped[Any] = mapped_column(JSON, nullable=True)
    reproducibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_sourcing_runs_idempotency_key"),
        CheckConstraint("kind in ('OPTIMIZE','COMPARE')", name="ck_sourcing_runs_kind"),
        Index("ix_sourcing_runs_pair", "product_id", "destination_facility_id", "id"),
        Index("ix_sourcing_runs_scenario", "scenario_id", "id"),
        Index("ix_sourcing_runs_status", "status", "id"),
    )


class SourcingDecision(TenantOwned, Base):
    """Insert-only. The latest row for a run is its approval state; ATLAS records the decision and never executes it."""

    __tablename__ = "sourcing_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sourcing_runs.id", ondelete="RESTRICT"))
    decision: Mapped[str] = mapped_column(String(10))
    rationale: Mapped[str] = mapped_column(String(1000))
    decided_by: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    allocation_hash: Mapped[str] = mapped_column(String(64))  # the exact allocation that was approved or rejected

    __table_args__ = (
        CheckConstraint(_in("decision", DECISIONS), name="ck_sourcing_decisions_decision"),
        Index("ix_sourcing_decisions_run", "run_id", "id"),
    )
