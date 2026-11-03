"""Risk methodology: categories, documented requirements, weights and thresholds (``rm-1``).

Everything that can change a score lives here and is hashed with the methodology version into every assessment.
Parameters are *analytical choices*, not measurements: they are documented in docs/methodology/risk-scoring.md.
Weights are never editable through the API. An operator may supply a JSON override file
(``ATLAS_RISK_CONFIG_PATH``) but must give it a new ``version`` label if any value differs from the built-in defaults,
so two different parameter sets can never be stored under the same version.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.errors import DomainValidationError
from app.models.enums import RiskCategory as C

METHODOLOGY_VERSION = "rm-1"
SCORE_MIN, SCORE_MAX = 0.0, 100.0
RELIABILITY_ROLE_CATEGORIES = (C.DATA_QUALITY, C.RELATIONSHIP_UNCERTAINTY)
# Correlated risk categories share underlying evidence (the same supply relationships). Within a group the group
# score is the HIGHEST available member score, so the same relationships are not added up several times.
CORRELATION_GROUPS: dict[str, tuple[C, ...]] = {"supply_structure": (C.CONCENTRATION, C.DEPENDENCY, C.SINGLE_SOURCE)}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Freshness(_Strict):
    fresh_days: int = Field(90, ge=1, le=3650)
    aging_days: int = Field(365, ge=2, le=7300)  # older than this is STALE and is not scored

    @model_validator(mode="after")
    def _order(self) -> Freshness:
        if self.aging_days <= self.fresh_days:
            raise ValueError("aging_days must exceed fresh_days")
        return self


class Thresholds(_Strict):
    moderate: float = Field(30.0, gt=0, lt=100)
    high: float = Field(55.0, gt=0, lt=100)
    critical: float = Field(75.0, gt=0, lt=100)

    @model_validator(mode="after")
    def _order(self) -> Thresholds:
        if not self.moderate < self.high < self.critical:
            raise ValueError("thresholds must satisfy moderate < high < critical")
        return self


class Delivery(_Strict):
    metric: str = "on_time_delivery_rate"
    target_pct: float = Field(98.0, gt=0, le=100)  # at or above: delivery risk 0
    floor_pct: float = Field(70.0, ge=0, lt=100)  # at or below: delivery risk 100
    min_observations: int = Field(1, ge=1, le=50)

    @model_validator(mode="after")
    def _order(self) -> Delivery:
        if self.floor_pct >= self.target_pct:
            raise ValueError("floor_pct must be below target_pct")
        return self


class Traversal(_Strict):
    depth: int = Field(2, ge=1, le=6)
    max_nodes: int = Field(200, ge=10, le=500)
    max_edges: int = Field(400, ge=10, le=1000)


class Compare(_Strict):
    max_coverage_spread: float = Field(0.15, ge=0, le=1)  # wider spread: scores are not presented as a ranking


class AlertRules(_Strict):
    score_increase_points: float = Field(10.0, ge=1, le=100)
    max_coverage_delta: float = Field(0.10, ge=0, le=1)  # larger coverage change: scores are not comparable, no alert
    methodology_change_points: float = Field(15.0, ge=1, le=100)
    uncertainty_increase_points: float = Field(20.0, ge=1, le=100)
    critical_edge_levels: list[str] = Field(default_factory=lambda: ["HIGH", "CRITICAL"])
    max_alerts_per_run: int = Field(200, ge=1, le=5000)


class RiskConfig(_Strict):
    version: str = Field(METHODOLOGY_VERSION, min_length=1, max_length=16)
    # Weights of RISK-role categories (reliability-role categories never change the score). Must sum to 1.
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "CONCENTRATION": 0.15, "DEPENDENCY": 0.15, "SINGLE_SOURCE": 0.10, "GEOGRAPHIC": 0.10,
            "OPERATIONAL": 0.10, "DELIVERY": 0.15, "FINANCIAL": 0.15, "COMPLIANCE": 0.10,
        }
    )  # fmt: skip
    min_coverage_for_score: float = Field(0.30, ge=0, le=1)
    min_scored_factors: int = Field(2, ge=1, le=10)
    min_coverage_for_classification: float = Field(0.50, ge=0, le=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    criticality_weights: dict[str, float] = Field(
        default_factory=lambda: {"LOW": 0.25, "MEDIUM": 0.5, "HIGH": 0.75, "CRITICAL": 1.0}
    )
    unknown_criticality_weight: float = Field(0.5, ge=0, le=1)  # labelled approximation; lowers factor confidence
    dependency_saturation: float = Field(6.0, gt=0)  # criticality-weighted dependents at which importance scores 100
    single_source_saturation: float = Field(2.0, gt=0)
    share_sum_tolerance_pct: float = Field(2.0, ge=0, le=10)
    count_based_equal_share_approximation: bool = True  # labelled INFERRED; set false to require real share data
    freshness: Freshness = Field(default_factory=Freshness)
    freshness_confidence: dict[str, float] = Field(default_factory=lambda: {"FRESH": 1.0, "AGING": 0.7, "UNKNOWN": 0.5})
    provenance_reliability: dict[str, float] = Field(
        default_factory=lambda: {
            "USER_PROVIDED": 0.85, "IMPORTED_EXTERNAL": 0.80, "MODEL_ESTIMATE": 0.50,
            "SYNTHETIC_DEMO": 0.40, "SIMULATED": 0.30,
        }
    )  # fmt: skip
    basis_confidence: dict[str, float] = Field(default_factory=lambda: {"OBSERVED": 1.0, "INFERRED": 0.6})
    unknown_quality_factor: float = Field(0.5, ge=0, le=1)  # used in place of a quality score that does not exist
    reliability_high: float = Field(0.75, gt=0, le=1)
    reliability_moderate: float = Field(0.50, gt=0, le=1)
    relationship_low_confidence: float = Field(0.5, ge=0, le=1)
    uncertainty_reliability_penalty: float = Field(0.5, ge=0, le=1)
    delivery: Delivery = Field(default_factory=Delivery)
    traversal: Traversal = Field(default_factory=Traversal)
    alerts: AlertRules = Field(default_factory=AlertRules)
    compare: Compare = Field(default_factory=Compare)
    assessment_stale_days: int = Field(30, ge=1, le=3650)  # an unchanged assessment older than this is "aging"

    @model_validator(mode="after")
    def _check(self) -> RiskConfig:
        valid = {c.value for c in C if c not in RELIABILITY_ROLE_CATEGORIES}
        if set(self.weights) != valid:
            raise ValueError("weights must name exactly the risk-role categories: " + ", ".join(sorted(valid)))
        if any(w < 0 for w in self.weights.values()) or abs(sum(self.weights.values()) - 1.0) > 1e-9:
            raise ValueError("weights must be non-negative and sum to 1.0")
        if set(self.criticality_weights) != {"LOW", "MEDIUM", "HIGH", "CRITICAL"}:
            raise ValueError("criticality_weights must name LOW, MEDIUM, HIGH and CRITICAL")
        if not 0 <= min(self.criticality_weights.values()) <= max(self.criticality_weights.values()) <= 1:
            raise ValueError("criticality weights must be within 0..1")
        if self.reliability_moderate >= self.reliability_high:
            raise ValueError("reliability_moderate must be below reliability_high")
        if self.min_coverage_for_classification < self.min_coverage_for_score:
            raise ValueError("min_coverage_for_classification must not be below min_coverage_for_score")
        return self


def canonical_json(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def config_hash(cfg: RiskConfig) -> str:
    return hashlib.sha256(canonical_json(cfg).encode("utf-8")).hexdigest()


DEFAULT_HASH = config_hash(RiskConfig())


@lru_cache(maxsize=8)
def _load(path_text: str | None, mtime: float | None) -> RiskConfig:
    if path_text is None:
        return RiskConfig()
    try:
        raw: Any = json.loads(Path(path_text).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object")
        cfg = RiskConfig.model_validate(raw)
    except (OSError, ValueError) as exc:
        raise DomainValidationError(f"Invalid risk methodology file: {str(exc)[:200]}") from None
    if cfg.version == METHODOLOGY_VERSION and config_hash(cfg) != DEFAULT_HASH:
        raise DomainValidationError(
            f"Parameters differ from the built-in {METHODOLOGY_VERSION}; give the override a new version label."
        )
    return cfg


def risk_config(path: Path | None = None) -> RiskConfig:
    if path is None:
        return _load(None, None)
    p = Path(path)
    return _load(str(p), p.stat().st_mtime if p.exists() else None)


# ------------------------------------------------------------------------------------------ category documentation


@dataclass(frozen=True)
class CategoryDoc:
    category: C
    label: str
    role: str  # "risk" (changes the score) | "reliability" (lowers reliability; never changes the score)
    data_available: bool  # does ATLAS hold the evidence this category needs?
    required_inputs: tuple[str, ...]
    method: str
    evidence_requirements: str
    freshness_policy: str
    uncertainty_treatment: str
    unavailable_when: str
    data_dependency: str | None  # what is missing when ``data_available`` is False
    limitations: tuple[str, ...] = field(default_factory=tuple)
    score_range: tuple[float, float] = (SCORE_MIN, SCORE_MAX)


_FRESH_REGISTRY = "Registry and relationship records have no observation date; their record update time is shown but not used to age a score."

CATEGORY_DOCS: dict[C, CategoryDoc] = {
    C.CONCENTRATION: CategoryDoc(
        C.CONCENTRATION,
        "Concentration risk",
        "risk",
        True,
        ("Confirmed SUPPLIES_PRODUCT and SUPPLIES relationships", "supply_share_pct (optional)"),
        "For each product or facility the supplier serves that has at least two confirmed suppliers: the supplier's share "
        "of the known supplier set (documented shares when every supplier has one and they total 100% within tolerance, "
        "otherwise 1/n as a labelled equal-share approximation). Score = 100 x criticality-weighted mean share.",
        "At least one served node with two or more confirmed suppliers. Sole-sourced nodes are scored under SINGLE_SOURCE.",
        _FRESH_REGISTRY,
        "Count-based shares are marked INFERRED and halve the basis confidence; the equal-share approximation can be switched off.",
        "No served node has a second confirmed supplier (NOT_APPLICABLE when all are sole-sourced), the supplier serves nothing "
        "(UNAVAILABLE), or share data is missing and the approximation is disabled.",
        None,
        (
            "Shares reflect recorded relationships only; unrecorded suppliers are invisible.",
            "HHI is reported only when documented shares are complete.",
        ),
    ),
    C.DEPENDENCY: CategoryDoc(
        C.DEPENDENCY,
        "Dependency risk",
        "risk",
        True,
        ("Confirmed SUPPLIES_PRODUCT and SUPPLIES relationships", "relationship criticality (optional)"),
        "Importance within the downstream network: the criticality-weighted count of products and facilities that depend on "
        "the supplier, divided by a saturation constant and capped at 100. Downstream reach (bounded traversal) is shown as "
        "context and is not scored.",
        "At least one confirmed dependent node.",
        _FRESH_REGISTRY,
        "Edges without a criticality use a labelled default weight and lower the factor confidence.",
        "The supplier has no confirmed dependents (UNAVAILABLE: absence of records is not absence of dependency).",
        None,
        (
            "Having many relationships does not make a supplier critical: only criticality-weighted dependents count.",
            "Downstream reach is bounded and may be truncated.",
        ),
    ),
    C.GEOGRAPHIC: CategoryDoc(
        C.GEOGRAPHIC,
        "Geographic exposure",
        "risk",
        True,
        ("Supplier country_or_region", "owned facilities' country_or_region", "other suppliers of the same products"),
        "Share of the served products' other confirmed suppliers that operate in a region shared with this supplier "
        "(registered region plus owned-facility regions), averaged over products with at least two confirmed suppliers.",
        "A recorded supplier region and at least one served product with two or more confirmed suppliers.",
        "Region labels carry no observation date.",
        "Regions are compared as normalised labels (no country-to-region mapping is invented). A registered region is "
        "not assumed to be where production happens.",
        "Supplier region missing, or no served product has two confirmed suppliers.",
        None,
        (
            "No hazard (earthquake, flood, conflict, sanctions) exposure is claimed: no authorised hazard source exists.",
            "Coordinates are summarised for quality only and do not drive the score.",
        ),
    ),
    C.OPERATIONAL: CategoryDoc(
        C.OPERATIONAL,
        "Operational exposure",
        "risk",
        False,
        ("Verified operational disruption records", "plant status feeds"),
        "Not evaluated: no operational disruption source exists.",
        "Verified, dated disruption observations.",
        "Would require dated observations.",
        "n/a",
        "Always UNAVAILABLE in rm-1.",
        "A connector for verified supplier or site disruption data (outages, incidents, force-majeure notices).",
        ("Capacity utilisation and inventory observations are not disruption evidence and are not used.",),
    ),
    C.DELIVERY: CategoryDoc(
        C.DELIVERY,
        "Delivery and lead-time risk",
        "risk",
        True,
        ("Supplier observations named on_time_delivery_rate (percent)",),
        "Latest non-stale on-time-delivery observation mapped linearly between a floor (risk 100) and a target (risk 0). "
        "Trend and quoted lead time are shown as context and not scored.",
        "At least the configured minimum number of non-stale observations of the metric.",
        "Observations up to fresh_days old are FRESH, up to aging_days AGING (confidence 0.7), older are STALE and not scored.",
        "A single observation is scored but flagged; provenance (e.g. SYNTHETIC_DEMO) scales confidence.",
        "No non-stale observations of the metric (UNAVAILABLE; a missing history is not proof of unreliable delivery).",
        None,
        (
            "The floor and target are analytical settings, not industry facts.",
            "Quoted lead times are promises, not delivery performance.",
        ),
    ),
    C.FINANCIAL: CategoryDoc(
        C.FINANCIAL,
        "Financial risk",
        "risk",
        False,
        ("Verified financial indicators from an authorised source",),
        "Not evaluated: ATLAS holds no financial statements or credit data.",
        "Verified, dated indicators.",
        "Would require dated filings.",
        "n/a",
        "Always UNAVAILABLE in rm-1.",
        "An authorised financial-data connector (filings, credit ratings) with provider terms and credentials.",
        ("A missing financial record is never read as distress.",),
    ),
    C.COMPLIANCE: CategoryDoc(
        C.COMPLIANCE,
        "Compliance risk",
        "risk",
        False,
        ("Verified compliance findings from a supported source",),
        "Not evaluated: ATLAS holds no sanctions, audit or certification data.",
        "Verified, dated findings.",
        "Would require dated findings.",
        "n/a",
        "Always UNAVAILABLE in rm-1.",
        "A legitimate compliance-data connector (sanctions lists, audit results, certifications).",
        ("No sanctions or compliance claim is ever made without a source.",),
    ),
    C.SINGLE_SOURCE: CategoryDoc(
        C.SINGLE_SOURCE,
        "Single-source dependency",
        "risk",
        True,
        ("Confirmed supplier sets of each served product and facility", "relationship criticality (optional)"),
        "Nodes for which this supplier is the only CONFIRMED supplier in the observed dataset. Score = 100 x "
        "criticality-weighted sole-sourced nodes / saturation, capped. Proposed alternatives are reported (not counted).",
        "At least one confirmed dependent node.",
        _FRESH_REGISTRY,
        "'Only supplier in the observed dataset' is not 'exclusive supplier': exclusivity is not modelled and is never claimed.",
        "The supplier has no confirmed dependents (UNAVAILABLE).",
        None,
        ("An incomplete dataset can make a multi-source node look single-sourced.",),
    ),
    C.DATA_QUALITY: CategoryDoc(
        C.DATA_QUALITY,
        "Data-quality concern",
        "reliability",
        True,
        ("quality scores of the supplier and its relationships", "open quality findings"),
        "Concern = 100 - weighted mean of available quality scores. It lowers assessment reliability and never changes the "
        "risk score.",
        "At least one SCORED quality result.",
        "Uses the latest evaluation; its age is shown.",
        "A record never evaluated, or with insufficient evidence, is unknown (not good) and applies a confidence discount.",
        "No quality evaluation exists for the supplier or its relationships.",
        None,
        ("A high quality score does not mean the data is correct.",),
    ),
    C.RELATIONSHIP_UNCERTAINTY: CategoryDoc(
        C.RELATIONSHIP_UNCERTAINTY,
        "Relationship uncertainty",
        "reliability",
        True,
        ("Status, confidence and criticality of the supplier's dependency relationships",),
        "Criticality-weighted share of the supplier's relationships that are proposed, expired, not yet effective or "
        "low-confidence. It lowers reliability and never changes the risk score.",
        "At least one relationship of any status.",
        "Expiry dates are compared with the assessment date.",
        "Unverified relationships are excluded from every risk metric; their effect is shown as a sensitivity.",
        "The supplier has no relationships at all (UNAVAILABLE).",
        None,
        ("Confidence values are only as good as the person or system that recorded them.",),
    ),
}
RISK_CATEGORIES = tuple(c for c in C if c not in RELIABILITY_ROLE_CATEGORIES)


def classify(score: float, t: Thresholds) -> str:
    if score >= t.critical:
        return "CRITICAL"
    if score >= t.high:
        return "HIGH"
    if score >= t.moderate:
        return "MODERATE"
    return "LOW"
