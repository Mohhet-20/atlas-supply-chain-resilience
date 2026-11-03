"""Ingestion bookkeeping: mapping profiles, batches, row results, findings, lineage."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TenantOwned
from app.models.enums import (
    BatchStatus,
    EntityKind,
    FindingOrigin,
    IngestPolicy,
    ProfileStatus,
    ResolutionStatus,
    RowOutcome,
    Severity,
    SourceFormat,
)
from app.models.source import DataSource
from app.models.types import UTCDateTime, enum_column, utcnow


class MappingProfile(TenantOwned, Base):
    """Versioned, immutable-once-active mapping from source columns to canonical fields."""

    __tablename__ = "mapping_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    entity_type: Mapped[EntityKind] = mapped_column(enum_column(EntityKind, "mapping_profile_entity"))
    source_format: Mapped[SourceFormat] = mapped_column(enum_column(SourceFormat, "mapping_profile_format"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    status: Mapped[ProfileStatus] = mapped_column(enum_column(ProfileStatus, "mapping_profile_status"))
    definition: Mapped[dict[str, Any]] = mapped_column(JSON)
    definition_hash: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        UniqueConstraint("organization_id", "name", "version", name="uq_mapping_profiles_name_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        Index("ix_mapping_profiles_status", "status"),
        Index("ix_mapping_profiles_entity_type", "entity_type"),
    )


class IngestionBatch(TenantOwned, Base):
    __tablename__ = "ingestion_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_uid: Mapped[str] = mapped_column(String(40), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    entity_type: Mapped[EntityKind] = mapped_column(enum_column(EntityKind, "batch_entity"))
    source_format: Mapped[SourceFormat] = mapped_column(enum_column(SourceFormat, "batch_format"))
    mapping_profile_id: Mapped[int | None] = mapped_column(ForeignKey("mapping_profiles.id", ondelete="RESTRICT"))
    mapping_profile_ref: Mapped[str] = mapped_column(String(160))  # "canonical:1" or "name:vN"
    policy: Mapped[IngestPolicy] = mapped_column(enum_column(IngestPolicy, "batch_policy"))
    status: Mapped[BatchStatus] = mapped_column(enum_column(BatchStatus, "batch_status"))
    file_name: Mapped[str | None] = mapped_column(String(120))  # sanitized, display only
    content_sha256: Mapped[str] = mapped_column(String(64))
    content_bytes: Mapped[int] = mapped_column(Integer)
    normalization_version: Mapped[str] = mapped_column(String(16))
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(String(300))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    source: Mapped[DataSource] = relationship()

    __table_args__ = (
        Index("ix_ingestion_batches_fingerprint", "fingerprint"),
        Index("ix_ingestion_batches_source_id", "source_id"),
        Index("ix_ingestion_batches_status", "status"),
        # Idempotency: at most one effective (applied) batch per fingerprint.
        Index(
            "uq_ingestion_batches_applied_fingerprint",
            "organization_id",
            "fingerprint",
            unique=True,
            sqlite_where=text("status IN ('COMMITTED','COMPLETED_WITH_ERRORS')"),
            postgresql_where=text("status IN ('COMMITTED','COMPLETED_WITH_ERRORS')"),
        ),
    )


class IngestionRowResult(TenantOwned, Base):
    __tablename__ = "ingestion_row_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("ingestion_batches.id", ondelete="CASCADE"))
    row_index: Mapped[int] = mapped_column(Integer)  # original 1-based data row / record index
    source_line: Mapped[int | None] = mapped_column(Integer)  # original file line (CSV)
    external_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[RowOutcome] = mapped_column(enum_column(RowOutcome, "row_outcome"))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    record_fingerprint: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("batch_id", "row_index", name="uq_ingestion_row_results_batch_row"),
        Index("ix_ingestion_row_results_outcome", "outcome"),
    )


class DataQualityFinding(TenantOwned, Base):
    """Persisted, reviewable quality finding. Never deleted; resolution is a status transition."""

    __tablename__ = "data_quality_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    finding_key: Mapped[str] = mapped_column(String(64))  # stable hash: rule+entity+field+row+batch
    rule_id: Mapped[str] = mapped_column(String(16))
    severity: Mapped[Severity] = mapped_column(enum_column(Severity, "finding_severity"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_batches.id", ondelete="CASCADE"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    entity_type: Mapped[EntityKind | None] = mapped_column(enum_column(EntityKind, "finding_entity"))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    external_id: Mapped[str | None] = mapped_column(String(64))
    field: Mapped[str | None] = mapped_column(String(80))
    row_index: Mapped[int | None] = mapped_column(Integer)
    source_line: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(String(400))
    suggestion: Mapped[str | None] = mapped_column(String(400))
    observed_value: Mapped[str | None] = mapped_column(String(120))
    resolution_status: Mapped[ResolutionStatus] = mapped_column(
        enum_column(ResolutionStatus, "finding_resolution"), default=ResolutionStatus.OPEN
    )
    resolution_note: Mapped[str | None] = mapped_column(String(400))
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # --- evaluation findings with a lifecycle (ingestion findings keep these at their defaults) ---
    origin: Mapped[FindingOrigin] = mapped_column(
        enum_column(FindingOrigin, "finding_origin"), default=FindingOrigin.INGESTION, server_default="INGESTION"
    )
    rule_version: Mapped[int | None] = mapped_column(Integer)
    dimension: Mapped[str | None] = mapped_column(String(32))
    evidence: Mapped[Any] = mapped_column(JSON, nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    first_detected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reopened_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    assigned_to: Mapped[str | None] = mapped_column(String(64))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("quality_eval_runs.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    __table_args__ = (
        UniqueConstraint("batch_id", "finding_key", name="uq_data_quality_findings_batch_key"),
        # Repeated evaluations must never duplicate a finding: one evaluation finding per stable key.
        Index(
            "uq_data_quality_findings_eval_key",
            "organization_id",
            "finding_key",
            unique=True,
            sqlite_where=text("origin = 'EVALUATION'"),
            postgresql_where=text("origin = 'EVALUATION'"),
        ),
        Index("ix_data_quality_findings_origin", "origin"),
        Index("ix_data_quality_findings_run_id", "run_id"),
        Index("ix_data_quality_findings_source_id", "source_id"),
        Index("ix_data_quality_findings_rule_id", "rule_id"),
        Index("ix_data_quality_findings_severity", "severity"),
        Index("ix_data_quality_findings_resolution_status", "resolution_status"),
        Index("ix_data_quality_findings_batch_id", "batch_id"),
        Index("ix_data_quality_findings_entity", "entity_type", "entity_id"),
    )


class RecordLineage(TenantOwned, Base):
    """Where a canonical record's current values came from (one row per applied change)."""

    __tablename__ = "record_lineage"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[EntityKind] = mapped_column(enum_column(EntityKind, "lineage_entity"))
    entity_id: Mapped[int] = mapped_column(Integer)
    batch_id: Mapped[int] = mapped_column(ForeignKey("ingestion_batches.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(String(64))
    row_index: Mapped[int] = mapped_column(Integer)
    source_line: Mapped[int | None] = mapped_column(Integer)
    mapping_profile_ref: Mapped[str] = mapped_column(String(160))
    normalization_version: Mapped[str] = mapped_column(String(16))
    record_fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[RowOutcome] = mapped_column(enum_column(RowOutcome, "lineage_outcome"))
    changed_fields: Mapped[list[str] | None] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("ix_record_lineage_entity", "entity_type", "entity_id"),
        Index("ix_record_lineage_batch_id", "batch_id"),
        Index("ix_record_lineage_source_id", "source_id"),
    )
