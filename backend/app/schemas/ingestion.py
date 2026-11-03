from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.enums import (
    BatchStatus,
    EntityKind,
    IngestPolicy,
    ProfileStatus,
    Provenance,
    ResolutionStatus,
    RowOutcome,
    Severity,
    SourceFormat,
    SourceStatus,
    SourceType,
)
from app.schemas.common import ORMModel


class SourceCreate(BaseModel):
    namespace: str = Field(min_length=2, max_length=48, description="Lowercase slug that scopes external identifiers.")
    name: str = Field(min_length=1, max_length=200)
    source_type: SourceType = SourceType.CSV_FILE
    description: str | None = Field(default=None, max_length=1000)


class SourceRead(ORMModel):
    id: int
    namespace: str
    name: str
    source_type: SourceType
    provenance: Provenance
    status: SourceStatus
    description: str | None
    created_at: datetime


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    entity_type: EntityKind
    source_format: SourceFormat = SourceFormat.ANY
    source_id: int | None = Field(default=None, ge=1)
    definition: dict[str, Any]
    description: str | None = Field(default=None, max_length=1000)


class ProfileValidateRequest(BaseModel):
    entity_type: EntityKind
    definition: dict[str, Any]


class ProfileValidateResponse(BaseModel):
    valid: bool
    problems: list[str]


class ProfileRead(ORMModel):
    id: int
    name: str
    version: int
    entity_type: EntityKind
    source_format: SourceFormat
    source_id: int | None
    status: ProfileStatus
    definition: dict[str, Any]
    definition_hash: str
    description: str | None
    created_at: datetime
    activated_at: datetime | None


class BatchRead(BaseModel):
    id: int
    batch_uid: str
    status: BatchStatus
    policy: IngestPolicy
    source_id: int
    source_namespace: str
    entity_type: EntityKind
    source_format: SourceFormat
    mapping_profile_ref: str
    mapping_profile_id: int | None
    file_name: str | None
    content_sha256: str
    content_bytes: int
    fingerprint: str
    normalization_version: str
    total_rows: int
    created_count: int
    updated_count: int
    unchanged_count: int
    rejected_count: int
    skipped_count: int
    duplicate_count: int
    finding_count: int
    failure_reason: str | None
    started_at: datetime
    finished_at: datetime | None


class CommitResponse(BaseModel):
    ok: bool = Field(description="True only when the batch was applied (COMMITTED / COMPLETED_WITH_ERRORS).")
    replay: bool = Field(description="True when an identical, already-applied batch was returned (idempotent retry).")
    batch: BatchRead


class RowResultRead(ORMModel):
    id: int
    batch_id: int
    row_index: int
    source_line: int | None
    external_id: str | None
    outcome: RowOutcome
    entity_id: int | None
    record_fingerprint: str | None
    message: str | None


class FindingRead(ORMModel):
    id: int
    finding_key: str
    rule_id: str
    severity: Severity
    batch_id: int | None
    source_id: int | None
    entity_type: EntityKind | None
    entity_id: int | None
    external_id: str | None
    field: str | None
    row_index: int | None
    source_line: int | None
    message: str
    suggestion: str | None
    observed_value: str | None
    resolution_status: ResolutionStatus
    resolution_note: str | None
    resolved_at: datetime | None
    created_at: datetime


class FindingResolution(BaseModel):
    resolution_status: ResolutionStatus
    note: str | None = Field(default=None, max_length=400)


class RuleRead(BaseModel):
    rule_id: str
    name: str
    severity: Severity
    description: str


class LineageRead(ORMModel):
    id: int
    entity_type: EntityKind
    entity_id: int
    batch_id: int
    batch_uid: str | None = None
    source_id: int
    source_namespace: str | None = None
    external_id: str
    row_index: int
    source_line: int | None
    mapping_profile_ref: str
    normalization_version: str
    record_fingerprint: str
    outcome: RowOutcome
    changed_fields: list[str] | None
    file_name: str | None = None
    ingested_at: datetime


class PreviewRow(BaseModel):
    row_index: int
    source_line: int | None
    external_id: str | None
    action: str
    changed_fields: list[str]
    messages: list[str]


class PreviewFinding(BaseModel):
    rule_id: str
    severity: Severity
    row_index: int | None
    source_line: int | None
    external_id: str | None
    field: str | None
    message: str
    suggestion: str | None
    observed_value: str | None


class PreviewResponse(BaseModel):
    persisted: bool = False
    fingerprint: str
    plan_digest: str = Field(description="Send as expected_plan_digest on commit to detect stale previews.")
    policy: IngestPolicy
    entity_type: EntityKind
    source_format: SourceFormat
    source_namespace: str
    mapping_profile_ref: str
    normalization_version: str
    file_name: str | None
    content_sha256: str
    content_bytes: int
    would_apply: bool = Field(description="Whether a commit with this policy would apply any row.")
    already_applied_batch: str | None = Field(description="batch_uid when identical input was already committed.")
    counts: dict[str, int]
    finding_counts: dict[str, int]
    findings: list[PreviewFinding]
    findings_truncated: bool
    rows: list[PreviewRow]
    rows_truncated: bool
    columns: list[str]


class FormatInfo(BaseModel):
    format: str
    content_types: list[str]
    description: str
    options: dict[str, Any]


class FormatsResponse(BaseModel):
    formats: list[FormatInfo]
    entity_types: dict[str, list[dict[str, Any]]]
    limits: dict[str, int]
    encodings: list[str]
    delimiters: list[str]
    policies: list[str]
    normalization_version: str
