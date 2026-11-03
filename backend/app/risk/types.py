"""Plain data carriers for the risk engine. The engine is pure: no database, clock or randomness inside."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.graph.store import NodeKey
from app.models.enums import (
    EdgeStatus,
    EvidenceType,
    FactorBasis,
    FactorState,
    Freshness,
    RiskCategory,
)


@dataclass(frozen=True)
class EdgeFact:
    """One dependency relationship as the risk engine sees it (supplier -> facility, or supplier -> product)."""

    ref: str  # SUPPLY:12 or PRODUCT:7
    code: str
    type: str  # SUPPLIES | SUPPLIES_PRODUCT
    supplier_id: int
    node: NodeKey
    status: str  # EdgeStatus value
    criticality: str | None
    share_pct: float | None
    confidence: float | None
    provenance: str
    source_namespace: str | None
    effective_from: date | None
    effective_to: date | None
    updated_at: datetime | None

    def timing(self, today: date) -> str:
        if self.effective_from is not None and self.effective_from > today:
            return "FUTURE"
        if self.effective_to is not None and self.effective_to < today:
            return "EXPIRED"
        return "CURRENT"

    def counts(self, today: date) -> bool:
        """Only confirmed, currently effective relationships enter any risk metric."""
        return self.status == EdgeStatus.CONFIRMED.value and self.timing(today) == "CURRENT"


@dataclass(frozen=True)
class SupplierInfo:
    id: int
    code: str
    name: str
    country: str
    status: str
    provenance: str
    source_namespace: str | None
    updated_at: datetime | None
    merged_into_id: int | None = None


@dataclass(frozen=True)
class FacilityInfo:
    id: int
    code: str
    name: str
    country: str
    latitude: float | None
    longitude: float | None
    location_status: str
    status: str
    provenance: str
    updated_at: datetime | None


@dataclass(frozen=True)
class NodeInfo:
    key: NodeKey
    code: str
    name: str
    lifecycle: str


@dataclass(frozen=True)
class ObsFact:
    code: str
    metric: str
    value: float
    unit: str
    observed_at: datetime
    provenance: str
    source_label: str


@dataclass(frozen=True)
class QualityFact:
    subject: str  # SUPPLIER:3 | RELATIONSHIP:12 | PRODUCT_LINK:7
    state: str  # SCORED | INSUFFICIENT_EVIDENCE
    score: float | None
    evaluated_at: datetime | None


@dataclass(frozen=True)
class FindingFact:
    id: int
    rule_id: str
    severity: str
    subject: str
    message: str
    first_detected_at: datetime | None


@dataclass
class SupplierFacts:
    info: SupplierInfo
    owned_facilities: list[FacilityInfo] = field(default_factory=list)
    edges: list[EdgeFact] = field(default_factory=list)  # own edges, every status
    observations: list[ObsFact] = field(default_factory=list)
    quality: QualityFact | None = None
    edge_quality: dict[str, QualityFact] = field(default_factory=dict)
    findings: list[FindingFact] = field(default_factory=list)


@dataclass
class NetworkFacts:
    node_edges: dict[NodeKey, list[EdgeFact]] = field(default_factory=dict)  # all suppliers' edges onto each node
    nodes: dict[NodeKey, NodeInfo] = field(default_factory=dict)
    suppliers: dict[int, SupplierInfo] = field(default_factory=dict)
    regions: dict[int, frozenset[str]] = field(default_factory=dict)  # normalised region labels per supplier


@dataclass(frozen=True)
class ReachFact:
    """Bounded downstream traversal summary (context only; not scored)."""

    depth: int
    nodes_by_type: dict[str, int]
    edges: int
    truncated: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    etype: EvidenceType
    ref: str
    summary: str
    reasoning: str
    observed_at: datetime | None = None
    provenance: str | None = None
    source_namespace: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.etype.value, self.ref)


@dataclass
class FactorResult:
    category: RiskCategory
    role: str
    state: FactorState
    basis: FactorBasis
    weight: float
    explanation: str
    score: float | None = None
    unavailable_reason: str | None = None
    missing_inputs: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    evidence_observed_at: datetime | None = None
    freshness: Freshness = Freshness.UNKNOWN
    confidence: float | None = None
    quality_context: dict[str, Any] = field(default_factory=dict)
    requires_review: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    severity: str | None = None  # filled by scoring from the thresholds


@dataclass
class AssessmentResult:
    status: str
    score: float | None
    classification: str | None
    coverage: float
    effective_weight: float
    reliability: float | None
    reliability_band: str | None
    scored_factors: int
    factors: list[FactorResult]
    contributions: list[dict[str, Any]]
    missing: list[dict[str, Any]]
    actions: list[str]
    data_basis: dict[str, Any]
    fingerprint: str
    evidence_latest_at: datetime | None
    evidence_earliest_at: datetime | None
    requires_review: bool
    methodology_version: str
    methodology_hash: str
