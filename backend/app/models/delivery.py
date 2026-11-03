"""Lead-time storage: delivery snapshots and events, imports, model runs and delivery-risk assessments.

Snapshots, events, import rows and assessments are insert-only: a changed order is a new snapshot, never an edit, so the
original promised date and every issued prediction remain exactly as they were recorded. Nothing here is synthesised: a row
exists only because a source supplied it (or, for assessments, because ATLAS issued and recorded it).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
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
)  # fmt: skip
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance, SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow


class DeliveryImportKind(str, Enum):
    DELIVERIES = "DELIVERIES"
    EVENTS = "EVENTS"


class DeliveryImportStatus(str, Enum):
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"


class DeliveryRowOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class DeliveryImportBatch(TenantOwned, Base):
    __tablename__ = "delivery_import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_uid: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[DeliveryImportKind] = mapped_column(enum_column(DeliveryImportKind, "delivery_import_kind"))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[DeliveryImportStatus] = mapped_column(enum_column(DeliveryImportStatus, "delivery_import_status"))
    source_label: Mapped[str] = mapped_column(String(200))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "delivery_import_provenance"))
    file_name: Mapped[str | None] = mapped_column(String(255))
    file_format: Mapped[str] = mapped_column(String(8))
    requested_by: Mapped[str] = mapped_column(String(64))
    time_zone: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    mapping: Mapped[Any] = mapped_column(JSON, nullable=True)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_accepted: Mapped[int] = mapped_column(Integer, default=0)
    rows_duplicate: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "kind", "fingerprint", name="uq_delivery_import_batches_fingerprint"),
        Index("ix_delivery_import_batches_created", "created_at"),
    )


class DeliveryImportRow(TenantOwned, Base):
    __tablename__ = "delivery_import_rows"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("delivery_import_batches.id", ondelete="RESTRICT"))
    row_number: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[DeliveryRowOutcome] = mapped_column(enum_column(DeliveryRowOutcome, "delivery_row_outcome"))
    errors: Mapped[Any] = mapped_column(JSON, nullable=True)
    raw: Mapped[Any] = mapped_column(JSON, nullable=True)
    record_id: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (Index("ix_delivery_import_rows_batch", "batch_id", "row_number"),)


class DeliverySnapshot(TenantOwned, Base):
    """The state of one order line as a source reported it at ``observed_at``. Never edited; the latest snapshot is current."""

    __tablename__ = "delivery_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("delivery_import_batches.id", ondelete="RESTRICT"))
    delivery_ref: Mapped[str] = mapped_column(String(128))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    destination_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    origin_facility_id: Mapped[int | None] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    order_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    promised_dispatch_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    original_promised_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    revised_promised_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    arrived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    received_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reported_status: Mapped[str | None] = mapped_column(String(32))
    received_quantity: Mapped[float | None] = mapped_column(Float)
    transport_mode: Mapped[str | None] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    observed_at_defaulted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    exception_note: Mapped[str | None] = mapped_column(String(300))
    row_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "delivery_snapshot_provenance"))
    source_label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("delivery_ref", "product_id", "row_hash", name="uq_delivery_snapshot_version"),
        CheckConstraint("quantity IS NULL OR quantity > 0", name="quantity_positive"),
        CheckConstraint("received_quantity IS NULL OR received_quantity >= 0", name="received_nonneg"),
        Index("ix_delivery_snapshots_ref", "delivery_ref", "product_id", "observed_at"),
        Index("ix_delivery_snapshots_supplier", "supplier_id", "destination_facility_id"),
    )


class DeliveryEvent(TenantOwned, Base):
    """A timestamped event for an order (optionally one product line). Insert-only; never fabricated from a snapshot."""

    __tablename__ = "delivery_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("delivery_import_batches.id", ondelete="RESTRICT"))
    delivery_ref: Mapped[str] = mapped_column(String(128))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    event_type: Mapped[str] = mapped_column(String(24))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    value_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # PROMISE_REVISED: the newly committed time
    quantity: Mapped[float | None] = mapped_column(Float)  # PARTIAL_RECEIPT / RECEIVED
    source_event_id: Mapped[str | None] = mapped_column(String(128))
    row_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "delivery_event_provenance"))
    source_label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "row_hash", name="uq_delivery_event_hash"),
        Index("ix_delivery_events_ref", "delivery_ref", "occurred_at"),
    )


class LeadTimeRun(TenantOwned, Base):
    """One bounded training/evaluation run. Terminal runs are immutable and never deleted; they are the model versions."""

    __tablename__ = "leadtime_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "leadtime_run_status"))
    result_status: Mapped[str | None] = mapped_column(String(40))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    target: Mapped[str] = mapped_column(String(12))
    config: Mapped[Any] = mapped_column(JSON)
    config_hash: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(16))
    data_snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    selected_model: Mapped[str | None] = mapped_column(String(40))
    artifact: Mapped[Any] = mapped_column(JSON, nullable=True)  # fitted parameters of the selected model (no code)
    preparation: Mapped[Any] = mapped_column(JSON, nullable=True)
    splits: Mapped[Any] = mapped_column(JSON, nullable=True)
    candidates: Mapped[Any] = mapped_column(JSON, nullable=True)
    evaluation: Mapped[Any] = mapped_column(JSON, nullable=True)
    selection: Mapped[Any] = mapped_column(JSON, nullable=True)
    calibration: Mapped[Any] = mapped_column(JSON, nullable=True)
    intervals: Mapped[Any] = mapped_column(JSON, nullable=True)
    explanation: Mapped[Any] = mapped_column(JSON, nullable=True)
    leakage_controls: Mapped[Any] = mapped_column(JSON, nullable=True)
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
        UniqueConstraint("organization_id", "idempotency_key", name="uq_leadtime_runs_idempotency_key"),
        Index("ix_leadtime_runs_status", "status"),
        Index("ix_leadtime_runs_target", "target", "queued_at"),
    )


class DeliveryAssessment(TenantOwned, Base):
    """An issued lead-time estimate / late-delivery risk assessment. Insert-only: it is never rewritten."""

    __tablename__ = "delivery_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_uid: Mapped[str] = mapped_column(String(40), unique=True)
    subject: Mapped[str] = mapped_column(String(10))  # DELIVERY | LANE
    delivery_ref: Mapped[str | None] = mapped_column(String(128))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"))
    destination_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    prediction_point: Mapped[str] = mapped_column(String(20))
    reference_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(24))  # ASSESSED | UNAVAILABLE
    unavailable_reason: Mapped[str | None] = mapped_column(String(300))
    commitment: Mapped[str] = mapped_column(String(10))
    promised_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    unit: Mapped[str | None] = mapped_column(String(16))
    leadtime_run_id: Mapped[int | None] = mapped_column(ForeignKey("leadtime_runs.id", ondelete="RESTRICT"))
    risk_run_id: Mapped[int | None] = mapped_column(ForeignKey("leadtime_runs.id", ondelete="RESTRICT"))
    estimate: Mapped[Any] = mapped_column(JSON, nullable=True)
    arrival: Mapped[Any] = mapped_column(JSON, nullable=True)
    risk: Mapped[Any] = mapped_column(JSON, nullable=True)
    history: Mapped[Any] = mapped_column(JSON, nullable=True)
    factors: Mapped[Any] = mapped_column(JSON, nullable=True)
    evidence: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_delivery_assessments_idempotency_key"),
        Index("ix_delivery_assessments_subject", "delivery_ref", "product_id", "created_at"),
        Index("ix_delivery_assessments_lane", "supplier_id", "destination_facility_id", "created_at"),
    )
