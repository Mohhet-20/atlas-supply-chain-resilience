"""Registry intelligence tables: aliases, identifiers, duplicate candidates, reviews, merges, quality, audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import (
    AliasKind,
    AuditOutcome,
    CandidateStatus,
    CandidateTier,
    MergeStatus,
    RegistryType,
    RunStatus,
)
from app.models.types import UTCDateTime, enum_column, utcnow


class ResolutionRun(TenantOwned, Base):
    __tablename__ = "resolution_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "resolution_run_entity_type"))
    config_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus, "resolution_run_status"))
    actor: Mapped[str] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    entities_considered: Mapped[int] = mapped_column(Integer, default=0)
    blocks: Mapped[int] = mapped_column(Integer, default=0)
    pairs_compared: Mapped[int] = mapped_column(Integer, default=0)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0)
    candidates_new: Mapped[int] = mapped_column(Integer, default=0)
    truncated_reason: Mapped[str | None] = mapped_column(String(200))


class EntityAlias(TenantOwned, Base):
    __tablename__ = "entity_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "alias_entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer)
    alias: Mapped[str] = mapped_column(String(200))
    alias_normalized: Mapped[str] = mapped_column(String(255))
    kind: Mapped[AliasKind] = mapped_column(enum_column(AliasKind, "alias_kind"))
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    origin_merge_id: Mapped[int | None] = mapped_column(ForeignKey("merge_records.id", ondelete="RESTRICT"))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "alias_normalized", "kind", name="uq_entity_aliases_entity_alias"),
        CheckConstraint("length(trim(alias)) > 0", name="alias_not_blank"),
        Index("ix_entity_aliases_lookup", "entity_type", "alias_normalized"),
    )


class EntityIdentifier(TenantOwned, Base):
    """Additional identifiers of a canonical record, scoped by the issuing source and an identifier scheme."""

    __tablename__ = "entity_identifiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "identifier_entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    scheme: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(64))
    authoritative: Mapped[bool] = mapped_column(Boolean, default=False)
    origin_merge_id: Mapped[int | None] = mapped_column(ForeignKey("merge_records.id", ondelete="RESTRICT"))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("entity_type", "source_id", "scheme", "value", name="uq_entity_identifiers_scoped_value"),
        CheckConstraint("length(trim(value)) > 0", name="value_not_blank"),
        Index("ix_entity_identifiers_entity", "entity_type", "entity_id"),
        Index("ix_entity_identifiers_value", "value"),
    )


class DuplicateCandidate(TenantOwned, Base):
    __tablename__ = "duplicate_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "candidate_entity_type"))
    left_id: Mapped[int] = mapped_column(Integer)
    right_id: Mapped[int] = mapped_column(Integer)
    rule_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float)
    tier: Mapped[CandidateTier] = mapped_column(enum_column(CandidateTier, "candidate_tier"))
    hard_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[Any] = mapped_column(JSON)
    evidence_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[CandidateStatus] = mapped_column(enum_column(CandidateStatus, "candidate_status"))
    left_version: Mapped[int] = mapped_column(Integer)
    right_version: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("resolution_runs.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("entity_type", "left_id", "right_id", name="uq_duplicate_candidates_pair"),
        CheckConstraint("left_id < right_id", name="ordered_pair"),
        Index("ix_duplicate_candidates_status", "status"),
        Index("ix_duplicate_candidates_tier", "tier"),
        Index("ix_duplicate_candidates_left", "entity_type", "left_id"),
        Index("ix_duplicate_candidates_right", "entity_type", "right_id"),
    )


class CandidateReview(TenantOwned, Base):
    """Append-only review decision."""

    __tablename__ = "candidate_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("duplicate_candidates.id", ondelete="RESTRICT"))
    from_status: Mapped[CandidateStatus | None] = mapped_column(enum_column(CandidateStatus, "review_from_status"))
    to_status: Mapped[CandidateStatus] = mapped_column(enum_column(CandidateStatus, "review_to_status"))
    actor: Mapped[str] = mapped_column(String(64))
    authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(1000))
    evidence_snapshot: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_candidate_reviews_candidate", "candidate_id"),)


class MergeRecord(TenantOwned, Base):
    __tablename__ = "merge_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    merge_uid: Mapped[str] = mapped_column(String(40), unique=True)
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "merge_entity_type"))
    survivor_id: Mapped[int] = mapped_column(Integer)
    merged_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[MergeStatus] = mapped_column(enum_column(MergeStatus, "merge_status"))
    actor: Mapped[str] = mapped_column(String(64))
    authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(1000))
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("duplicate_candidates.id", ondelete="SET NULL"))
    survivor_version_before: Mapped[int] = mapped_column(Integer)
    survivor_version_after: Mapped[int] = mapped_column(Integer)
    merged_version_after: Mapped[int] = mapped_column(Integer)
    survivor_before: Mapped[Any] = mapped_column(JSON)
    merged_before: Mapped[Any] = mapped_column(JSON)
    field_decisions: Mapped[Any] = mapped_column(JSON)
    reassignments: Mapped[Any] = mapped_column(JSON)
    removed_duplicates: Mapped[Any] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    compensated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    compensated_by: Mapped[str | None] = mapped_column(String(64))
    compensation_reason: Mapped[str | None] = mapped_column(String(1000))

    __table_args__ = (
        CheckConstraint("survivor_id <> merged_id", name="distinct_entities"),
        Index("ix_merge_records_survivor", "entity_type", "survivor_id"),
        Index("ix_merge_records_merged", "entity_type", "merged_id"),
    )


class QualityRun(TenantOwned, Base):
    __tablename__ = "quality_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    config_version: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    entity_types: Mapped[Any] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    entities_assessed: Mapped[int] = mapped_column(Integer, default=0)
    average_overall: Mapped[float | None] = mapped_column(Float)
    dimension_averages: Mapped[Any] = mapped_column(JSON)


class QualityAssessment(TenantOwned, Base):
    __tablename__ = "quality_assessments"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("quality_runs.id", ondelete="CASCADE"))
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "assessment_entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer)
    entity_version: Mapped[int] = mapped_column(Integer)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("data_sources.id", ondelete="SET NULL"))
    config_version: Mapped[str] = mapped_column(String(32))
    overall: Mapped[float | None] = mapped_column(Float)
    dimensions: Mapped[Any] = mapped_column(JSON)
    issues: Mapped[Any] = mapped_column(JSON)
    issue_count: Mapped[int] = mapped_column(Integer, default=0)
    assessed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("run_id", "entity_type", "entity_id", name="uq_quality_assessments_run_entity"),
        Index("ix_quality_assessments_entity", "entity_type", "entity_id"),
        Index("ix_quality_assessments_source", "source_id"),
    )


class AuditEvent(Base):
    """Append-only, hash-chained audit record. Updates and deletes are rejected (ORM guard + DB triggers)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(16))
    authenticated: Mapped[bool] = mapped_column(Boolean, default=False)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[AuditOutcome] = mapped_column(enum_column(AuditOutcome, "audit_outcome"))
    request_id: Mapped[str | None] = mapped_column(String(64))
    organization_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL: before tenancy
    details: Mapped[Any] = mapped_column(JSON)
    prev_hash: Mapped[str] = mapped_column(String(64))
    event_hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (
        Index("ix_audit_events_action", "action"),
        Index("ix_audit_events_org", "organization_id"),
        Index("ix_audit_events_target", "target_type", "target_id"),
        Index("ix_audit_events_occurred", "occurred_at"),
    )
