"""Typed contracts for the supplier-risk API. The score is a rule-based indicator, never a probability."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import ReviewAction, RiskAlertStatus, RiskClass, RiskRunScope


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceOut(BaseModel):
    evidence_type: str
    ref: str
    summary: str
    observed_at: datetime | None = None
    provenance: str | None = None
    source_namespace: str | None = None
    reasoning: str


class FactorOut(BaseModel):
    id: int
    category: str
    label: str
    role: str
    state: str
    basis: str
    score: float | None = None
    severity: str | None = None
    weight: float
    explanation: str
    unavailable_reason: str | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    evidence_observed_at: datetime | None = None
    freshness: str
    confidence: float | None = None
    quality_context: dict[str, Any] = Field(default_factory=dict)
    requires_review: bool
    details: dict[str, Any] = Field(default_factory=dict)
    method_version: str
    data_available: bool
    evidence: list[EvidenceOut] | None = None


class AssessmentOut(BaseModel):
    id: int
    assessment_uid: str
    supplier_id: int
    run_id: int
    computed_at: datetime
    status: str
    methodology_version: str
    methodology_hash: str
    risk_score: float | None = None
    classification: str | None = None
    evidence_coverage: float
    effective_weight: float
    reliability: float | None = None
    reliability_band: str | None = None
    scored_factors: int
    contributions: list[dict[str, Any]] = Field(default_factory=list)
    missing: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    data_basis: dict[str, Any] = Field(default_factory=dict)
    evidence_latest_at: datetime | None = None
    evidence_earliest_at: datetime | None = None
    requires_review: bool
    review_state: str
    is_current: bool
    superseded_at: datetime | None = None
    supersedes_id: int | None = None
    last_confirmed_at: datetime
    rank_comparable: bool
    interpretation: dict[str, Any]


class AssessmentDetail(AssessmentOut):
    factors: list[FactorOut]


class ReviewOut(BaseModel):
    id: int
    supplier_id: int
    assessment_id: int
    action: str
    factor_category: str | None = None
    actor: str
    actor_role: str
    created_at: datetime
    note: str | None = None
    override_classification: str | None = None
    override_expires_at: datetime | None = None
    target_review_id: int | None = None
    original_status: str
    original_classification: str | None = None
    original_score: float | None = None


class OverrideOut(ReviewOut):
    expired: bool
    stale: bool
    effective: bool
    note_on_staleness: str | None = None


class AlertOut(BaseModel):
    id: int
    supplier_id: int
    supplier_name: str | None = None
    assessment_id: int | None = None
    alert_type: str
    severity: str
    status: str
    reason: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    run_id: int | None = None
    created_at: datetime
    updated_at: datetime
    last_seen_at: datetime
    occurrences: int
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    version: int
    delivery: str


class SupplierBrief(BaseModel):
    id: int
    supplier_code: str
    name: str
    country_or_region: str
    category: str
    status: str
    provenance: str


class SupplierRiskOut(BaseModel):
    supplier: SupplierBrief
    assessed: bool
    assessment: AssessmentDetail | None = None
    override: OverrideOut | None = None
    reviews: list[ReviewOut] = Field(default_factory=list)
    alerts: list[AlertOut] = Field(default_factory=list)
    unassessed_note: str | None = None


class HistoryPage(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    note: str


class RunOut(BaseModel):
    id: int
    run_uid: str
    scope_type: str
    scope_ref: dict[str, Any]
    status: str
    trigger: str
    reason: str | None = None
    requested_by: str
    idempotency_key: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    methodology_version: str | None = None
    methodology_hash: str | None = None
    graph_snapshot: dict[str, Any] = Field(default_factory=dict)
    examined: int
    completed_assessments: int
    assessed: int
    partial: int
    insufficient_evidence: int
    created: int
    unchanged: int
    failed: int
    skipped: int
    alerts_created: int
    alerts_updated: int
    failures: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool
    truncation_reasons: list[str] = Field(default_factory=list)
    error: str | None = None


class RunPage(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


class RunCreate(_Body):
    scope: RiskRunScope
    supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    supplier_ids: list[int] | None = Field(default=None, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    reason: str | None = Field(default=None, max_length=200)

    @field_validator("supplier_ids")
    @classmethod
    def _ids(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(i < 1 or i > 2**31 - 1 for i in v):
            raise ValueError("supplier ids must be positive integers")
        return v


class ReviewCreate(_Body):
    assessment_id: int = Field(
        ge=1, le=2**31 - 1, description="The assessment being reviewed; must be the current one."
    )
    action: ReviewAction
    note: str | None = Field(default=None, max_length=1000)
    factor_category: str | None = Field(default=None, max_length=32)
    override_classification: RiskClass | None = None
    override_expires_at: datetime | None = None
    target_review_id: int | None = Field(default=None, ge=1, le=2**31 - 1)


class AlertTransition(_Body):
    to: RiskAlertStatus
    note: str | None = Field(default=None, max_length=400)
    version: int = Field(ge=1)


class SupplierPage(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int
    sort: str
    ordering_note: str


class AlertPage(BaseModel):
    items: list[AlertOut]
    total: int
    limit: int
    offset: int
