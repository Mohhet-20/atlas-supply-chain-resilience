"""Request/response schemas for the Data Quality API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import EntityKind, EvalScope

FindingAction = Literal["acknowledge", "start_remediation", "resolve", "accept_exception", "reopen"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FindingOut(BaseModel):
    id: int
    origin: str
    rule_id: str
    rule_name: str | None = None
    rule_version: int | None = None
    dimension: str | None = None
    severity: str
    status: str
    entity_type: str | None = None
    entity_id: int | None = None
    entity_name: str | None = None
    external_id: str | None = None
    field: str | None = None
    message: str
    suggestion: str | None = None
    evidence: Any | None = None
    requires_review: bool
    batch_id: int | None = None
    row_index: int | None = None
    source_id: int | None = None
    source_namespace: str | None = None
    run_id: int | None = None
    first_detected_at: datetime | None = None
    last_evaluated_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    assigned_to: str | None = None
    reopened_count: int = 0
    version: int
    fix_action: str | None = None


class FindingPage(BaseModel):
    items: list[FindingOut]
    total: int
    limit: int
    offset: int


class FindingEventOut(BaseModel):
    id: int
    occurred_at: datetime
    actor: str
    action: str
    from_status: str | None = None
    to_status: str | None = None
    note: str | None = None
    details: Any | None = None
    run_id: int | None = None


class FindingDetail(BaseModel):
    finding: FindingOut
    rule: dict[str, Any] | None = None
    events: list[FindingEventOut]


class TransitionBody(_Strict):
    action: FindingAction
    note: str | None = Field(default=None, max_length=400)
    expected_version: int = Field(ge=1)


class AssignBody(_Strict):
    assignee: str | None = Field(default=None, min_length=1, max_length=64)
    expected_version: int = Field(ge=1)


class RemediateBody(_Strict):
    expected_version: int = Field(ge=1)
    confirm: bool = False


class BulkItem(_Strict):
    finding_id: int = Field(ge=1, le=2**31 - 1)
    expected_version: int = Field(ge=1)


class BulkRemediateBody(_Strict):
    items: list[BulkItem] = Field(min_length=1, max_length=25)
    confirm: bool = False


class RunCreate(_Strict):
    scope: EvalScope
    entity_type: EntityKind | None = None
    entity_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    batch_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    source_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    refs: list[str] | None = Field(default=None, max_length=200)


class RuleSettingBody(_Strict):
    enabled: bool
    reason: str = Field(min_length=3, max_length=300)


class RunOut(BaseModel):
    id: int
    run_uid: str
    scope_type: str
    scope_ref: Any | None = None
    status: str
    trigger: str
    requested_by: str
    queued_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    examined: dict[str, Any] = {}
    checks_evaluated: int | None = None
    findings: dict[str, int] = {}
    subjects_failed: int | None = None
    subjects_skipped: int | None = None
    truncated: bool | None = None
    truncation_reasons: list[str] = []
    aggregate: Any | None = None
    error: str | None = None
    failures: list[Any] | None = None
    catalog_hash: str | None = None
    formula_version: str | None = None
    config_version: str | None = None
    config_hash: str | None = None
    rule_versions: dict[str, int] | None = None


class RunPage(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int
