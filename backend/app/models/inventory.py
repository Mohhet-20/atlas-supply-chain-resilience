"""Inventory storage: inventory snapshots, demand observations, replenishments, policies, imports and stockout analyses.

Observations (positions, demand, replenishments) and analysis results are insert-only: a new snapshot adds a row and never
rewrites an earlier one, so a completed analysis can always be traced to the exact records it used. Nothing here is
synthesised: a row exists only because a source supplied it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
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
    Text,
    UniqueConstraint,
    text,
)  # fmt: skip
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance, SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow


class ImportKind(str, Enum):
    INVENTORY = "INVENTORY"
    DEMAND = "DEMAND"
    REPLENISHMENT = "REPLENISHMENT"


class ImportStatus(str, Enum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"  # nothing was valid; no records were written


class InvRowOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"  # identical to a record that already exists: counted once, not again
    REJECTED = "REJECTED"


class DemandKind(str, Enum):
    CONSUMPTION = "CONSUMPTION"
    DEMAND = "DEMAND"
    SALES = "SALES"
    ADJUSTMENT = "ADJUSTMENT"


class ReceiptStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    IN_TRANSIT = "IN_TRANSIT"
    PLANNED = "PLANNED"
    PROPOSED = "PROPOSED"
    DELAYED = "DELAYED"
    UNKNOWN = "UNKNOWN"


class PolicyStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"


class AnalysisKind(str, Enum):
    BASELINE = "BASELINE"
    SCENARIO = "SCENARIO"


class InventoryImportBatch(TenantOwned, Base):
    __tablename__ = "inventory_import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_uid: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[ImportKind] = mapped_column(enum_column(ImportKind, "inv_import_kind"))
    fingerprint: Mapped[str] = mapped_column(
        String(64)
    )  # sha256(kind, mapping, file bytes): a repeat import is detected
    status: Mapped[ImportStatus] = mapped_column(enum_column(ImportStatus, "inv_import_status"))
    source_label: Mapped[str] = mapped_column(String(200))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "inv_import_provenance"))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(8))
    requested_by: Mapped[str] = mapped_column(String(64))
    mapping: Mapped[Any] = mapped_column(JSON, nullable=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_accepted: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "kind", "fingerprint", name="uq_inv_import_batches_fingerprint"),
        Index("ix_inv_import_batches_created", "created_at"),
    )


class InventoryImportRow(TenantOwned, Base):
    __tablename__ = "inventory_import_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("inventory_import_batches.id", ondelete="CASCADE"))
    row_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[InvRowOutcome] = mapped_column(enum_column(InvRowOutcome, "inv_row_outcome"))
    errors: Mapped[Any] = mapped_column(JSON, nullable=True)  # [{code, field, message}]
    raw: Mapped[Any] = mapped_column(JSON, nullable=True)  # the source row (size-capped) for rejected-row inspection
    record_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_inv_import_rows_batch", "batch_id", "outcome", "row_number"),)


def _qty(name: str) -> CheckConstraint:
    return CheckConstraint(f"{name} IS NULL OR {name} >= 0", name=f"{name}_nonneg")


class InventoryPosition(TenantOwned, Base):
    """One inventory observation for a product at a facility. Raw source fields only; derived values are computed later."""

    __tablename__ = "inventory_positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("inventory_import_batches.id", ondelete="RESTRICT"))
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    row_hash: Mapped[str] = mapped_column(String(64))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    unit: Mapped[str] = mapped_column(String(32))
    on_hand: Mapped[float | None] = mapped_column(Float)
    reserved: Mapped[float | None] = mapped_column(Float)
    allocated: Mapped[float | None] = mapped_column(Float)
    quarantined: Mapped[float | None] = mapped_column(Float)
    damaged: Mapped[float | None] = mapped_column(Float)
    usable_source: Mapped[float | None] = mapped_column(Float)
    atp_source: Mapped[float | None] = mapped_column(Float)
    in_transit: Mapped[float | None] = mapped_column(Float)
    backordered: Mapped[float | None] = mapped_column(Float)
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "inv_position_provenance"))
    source_label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("product_id", "facility_id", "observed_at", name="uq_inv_positions_snapshot"),
        CheckConstraint("length(trim(unit)) > 0", name="unit_not_blank"),
        *[_qty(n) for n in ("on_hand", "reserved", "allocated", "quarantined", "damaged", "usable_source", "atp_source", "in_transit", "backordered")],
        Index("ix_inv_positions_pair_time", "product_id", "facility_id", "observed_at"),
        Index("ix_inv_positions_batch", "batch_id"),
    )  # fmt: skip


class DemandObservation(TenantOwned, Base):
    __tablename__ = "demand_observations"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("inventory_import_batches.id", ondelete="RESTRICT"))
    kind: Mapped[DemandKind] = mapped_column(enum_column(DemandKind, "demand_kind"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)  # exclusive
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    source_record_id: Mapped[str | None] = mapped_column(String(128))
    row_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "demand_provenance"))
    source_label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("product_id", "facility_id", "kind", "period_start", "period_end", name="uq_demand_period"),
        CheckConstraint("period_end > period_start", name="period_ordered"),
        CheckConstraint("kind = 'ADJUSTMENT' OR quantity >= 0", name="quantity_nonneg_except_adjustment"),
        CheckConstraint("length(trim(unit)) > 0", name="unit_not_blank"),
        Index("ix_demand_pair_period", "product_id", "facility_id", "period_start"),
    )


class InventoryReplenishment(TenantOwned, Base):
    """An expected receipt as reported by a source. Versioned by row_hash: a changed order is a new row, never an edit."""

    __tablename__ = "inventory_replenishments"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("inventory_import_batches.id", ondelete="RESTRICT"))
    reference: Mapped[str] = mapped_column(String(128))  # purchase order / shipment reference from the source
    status: Mapped[ReceiptStatus] = mapped_column(enum_column(ReceiptStatus, "receipt_status"))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32))
    expected_date: Mapped[date | None] = mapped_column(Date)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    row_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "receipt_provenance"))
    source_label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("product_id", "facility_id", "reference", "row_hash", name="uq_receipt_version"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("length(trim(unit)) > 0", name="unit_not_blank"),
        Index("ix_receipts_pair", "product_id", "facility_id", "expected_date"),
    )


class InventoryPolicy(TenantOwned, Base):
    """A configured policy for one product at one facility. A change creates a new version and supersedes the old one."""

    __tablename__ = "inventory_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[PolicyStatus] = mapped_column(enum_column(PolicyStatus, "inv_policy_status"))
    unit: Mapped[str] = mapped_column(String(32))
    safety_stock: Mapped[float | None] = mapped_column(Float)
    reorder_point: Mapped[float | None] = mapped_column(Float)
    minimum: Mapped[float | None] = mapped_column(Float)
    maximum: Mapped[float | None] = mapped_column(Float)
    target: Mapped[float | None] = mapped_column(Float)
    review_period_days: Mapped[float | None] = mapped_column(Float)
    lead_time_days: Mapped[float | None] = mapped_column(Float)  # normalised to days
    lead_time_input: Mapped[Any] = mapped_column(JSON, nullable=True)  # {value, unit} as entered
    service_level: Mapped[float | None] = mapped_column(Float)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime)
    owner: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "inv_policy_provenance"))
    note: Mapped[str | None] = mapped_column(String(400))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("product_id", "facility_id", "version", name="uq_inv_policy_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        *[_qty(n) for n in ("safety_stock", "reorder_point", "minimum", "maximum", "target", "review_period_days", "lead_time_days")],
        CheckConstraint("service_level IS NULL OR (service_level >= 0.5 AND service_level < 1)", name="service_level_range"),
        Index(
            "uq_inv_policy_active_pair", "product_id", "facility_id", unique=True,
            sqlite_where=text("status = 'ACTIVE'"), postgresql_where=text("status = 'ACTIVE'"),
        ),
    )  # fmt: skip


class StockoutAnalysis(TenantOwned, Base):
    __tablename__ = "stockout_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_uid: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[AnalysisKind] = mapped_column(enum_column(AnalysisKind, "stockout_kind"))
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "stockout_status"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    impact_analysis_id: Mapped[int | None] = mapped_column(ForeignKey("impact_analyses.id", ondelete="RESTRICT"))
    config: Mapped[Any] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(16))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    input_hash: Mapped[str | None] = mapped_column(String(64))  # hash of the records and policies read
    inputs: Mapped[Any] = mapped_column(JSON, nullable=True)  # counts, batch ids, record id ranges, impact analysis ref
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    coverage: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    failures: Mapped[Any] = mapped_column(JSON, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    truncation_reasons: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_stockout_analyses_idempotency_key"),
        Index("ix_stockout_analyses_status", "status"),
        Index("ix_stockout_analyses_queued", "queued_at"),
    )


class StockoutFinding(TenantOwned, Base):
    """One product at one facility in one analysis. Insert-only; every JSON block names the records it came from."""

    __tablename__ = "stockout_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("stockout_analyses.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(Integer)
    facility_id: Mapped[int] = mapped_column(Integer)
    product_code: Mapped[str] = mapped_column(String(128))
    product_name: Mapped[str] = mapped_column(String(200))
    facility_code: Mapped[str] = mapped_column(String(128))
    facility_name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(20)
    )  # current_shortage | projected_stockout | low_coverage | within_policy | unknown | insufficient_data
    severity_rank: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    quantities: Mapped[Any] = mapped_column(JSON)  # derived quantities with formulas and the source fields
    demand: Mapped[Any] = mapped_column(JSON)
    coverage_days: Mapped[Any] = mapped_column(JSON)  # days-of-inventory block
    policy: Mapped[Any] = mapped_column(JSON)
    projection: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # baseline series, receipts counted and excluded, crossings
    scenario: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # scenario series / bounds / effect; NULL for baseline analyses
    change: Mapped[str | None] = mapped_column(String(32))  # scenario vs baseline outcome class
    confidence: Mapped[str] = mapped_column(String(8))  # LOW | MEDIUM | HIGH, from the rules in docs/models.md
    confidence_basis: Mapped[Any] = mapped_column(JSON)
    coverage: Mapped[float] = mapped_column(Float)  # fraction of the inputs this status needs that were present
    coverage_detail: Mapped[Any] = mapped_column(JSON)
    data_quality: Mapped[Any] = mapped_column(JSON)
    assumptions: Mapped[Any] = mapped_column(JSON)
    evidence: Mapped[Any] = mapped_column(JSON)  # ids of the positions, demand rows, receipts and policy used
    explanation: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("analysis_id", "product_id", "facility_id", name="uq_stockout_findings_pair"),
        Index("ix_stockout_findings_status", "analysis_id", "status"),
        Index("ix_stockout_findings_rank", "analysis_id", "severity_rank"),
    )
