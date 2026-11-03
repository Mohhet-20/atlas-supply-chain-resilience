"""Sourcing configuration: request schema, limits, objectives and the standing statements.

Hard constraints (``Constraints``) decide whether a supplier may be used and what an allocation may look like; the objective and
the score weights only order what already satisfies them. Unknown fields (including any organisation id) are rejected.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM_VERSION = "sc-1"
OBJECTIVES = ("LOWEST_COST", "LOWEST_LANDED_COST", "RELIABILITY", "LOWEST_RISK", "BALANCED", "RESILIENCE")
STRATEGIES = ("AUTO", "SINGLE", "DUAL", "MULTI")
METRICS = ("price", "lead_time", "reliability", "quality", "capacity", "risk")
# direction of improvement of every scored metric
DIRECTION = {
    "price": "lower",
    "lead_time": "lower",
    "reliability": "higher",
    "quality": "lower",
    "capacity": "higher",
    "risk": "lower",
}
DEFAULT_WEIGHTS = {"price": 0.35, "lead_time": 0.15, "reliability": 0.20, "quality": 0.20, "capacity": 0.10, "risk": 0.0}  # fmt: skip
STATUS_ORDER = (
    "ELIGIBLE",
    "ELIGIBLE_AFTER_QUALIFICATION",
    "MISSING_INFORMATION",
    "UNAVAILABLE",
    "EXCLUDED",
    "DISQUALIFIED",
)

STATEMENTS = {
    "decision_support": "A sourcing plan is a modelled proposal over the offers registered in ATLAS. It is not a purchase order, a contract "
    "or an approved procurement decision; ATLAS never places an order, and an approval recorded here is an internal decision record only.",
    "score": "The suitability score (0-100) is a weighted comparison of normalised metrics among the eligible candidates of this requirement. "
    "It is not a probability of supplier success and it does not change any stored supplier record.",
    "cost": "Purchase cost is quantity x unit price in one currency. Freight is included only where an offer states it explicitly; other "
    "landed-cost components (duties, handling, insurance, carrying cost) are excluded. Currencies are never mixed and no exchange rate is applied.",
    "missing": "A missing value is never treated as zero or as favourable: a missing metric scores 0 in the composite and the ranking is "
    "flagged incomplete; a missing hard-constraint input makes the supplier unverifiable, not eligible.",
    "concentration": "HHI is computed on supplier shares of this one requirement (product at destination). It is not enterprise-wide "
    "concentration, and suppliers in the same country may still share a regional or upstream exposure that this measure cannot see.",
    "resilience": "A multi-supplier plan is not called resilient on the count of suppliers alone. Resilience findings come only from an "
    "explicit scenario or what-if adjustment, or from stated shares and country grouping.",
    "tenancy": "Data is separated by organisation: every query is limited to the caller's organisation; within it, sourcing records are visible to every user at or above the "
    "endpoint role, and unit prices are reserved for editors and above.",
}


@dataclass(frozen=True)
class Limits:
    max_candidates: int = 40
    max_active_runs: int = 4
    time_budget_seconds: float = 30.0
    max_nodes: int = 200_000
    max_grid_units: int = 20_000
    max_dp_operations: int = 3_000_000
    min_reliability_sample: int = 20
    max_delivery_rows: int = 50_000
    stale_days: int = 365
    stale_lock_seconds: float = 300.0


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: float = Field(default=DEFAULT_WEIGHTS["price"], ge=0, le=1000)
    lead_time: float = Field(default=DEFAULT_WEIGHTS["lead_time"], ge=0, le=1000)
    reliability: float = Field(default=DEFAULT_WEIGHTS["reliability"], ge=0, le=1000)
    quality: float = Field(default=DEFAULT_WEIGHTS["quality"], ge=0, le=1000)
    capacity: float = Field(default=DEFAULT_WEIGHTS["capacity"], ge=0, le=1000)
    risk: float = Field(default=DEFAULT_WEIGHTS["risk"], ge=0, le=1000)

    @model_validator(mode="after")
    def _check(self) -> Weights:
        vals = self.model_dump()
        if any(not math.isfinite(v) for v in vals.values()):
            raise ValueError("weights must be finite numbers")
        if sum(vals.values()) <= 0:
            raise ValueError("at least one weight must be positive")
        return self

    def normalised(self) -> dict[str, float]:
        v = self.model_dump()
        t = sum(v.values())
        return {k: x / t for k, x in v.items()}


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_lead_time_days: float | None = Field(default=None, gt=0, le=3650)
    required_by: datetime | None = None
    max_defect_rate: float | None = Field(default=None, ge=0, le=1)
    max_risk: float | None = Field(default=None, ge=0, le=1)
    min_reliability: float | None = Field(default=None, ge=0, le=1)
    allowed_supplier_ids: list[int] = Field(default_factory=list, max_length=200)
    excluded_supplier_ids: list[int] = Field(default_factory=list, max_length=200)
    excluded_countries: list[str] = Field(default_factory=list, max_length=100)
    max_supplier_share: float | None = Field(default=None, gt=0, le=1)
    min_supplier_share: float | None = Field(default=None, gt=0, le=1)
    min_suppliers: int | None = Field(default=None, ge=1, le=50)
    max_suppliers: int | None = Field(default=None, ge=1, le=50)
    overage_pct: float = Field(default=0.0, ge=0, le=100)
    max_data_age_days: float | None = Field(default=None, gt=0, le=36_500)
    require_transport_route: bool = False

    @model_validator(mode="after")
    def _check(self) -> Constraints:
        if self.min_suppliers and self.max_suppliers and self.min_suppliers > self.max_suppliers:
            raise ValueError("min_suppliers cannot exceed max_suppliers")
        if self.min_supplier_share and self.max_supplier_share and self.min_supplier_share > self.max_supplier_share:
            raise ValueError("min_supplier_share cannot exceed max_supplier_share")
        if set(self.allowed_supplier_ids) & set(self.excluded_supplier_ids):
            raise ValueError("a supplier cannot be both allowed and excluded")
        return self


class SupplierAdjustment(BaseModel):
    """A hypothetical what-if applied to a copy of one supplier's candidate terms. Stored records are never changed."""

    model_config = ConfigDict(extra="forbid")
    supplier_id: int = Field(gt=0)
    unavailable: bool = False
    price_multiplier: float = Field(default=1.0, gt=0, le=100)
    capacity_multiplier: float = Field(default=1.0, ge=0, le=100)
    add_lead_days: float = Field(default=0.0, ge=0, le=3650)
    risk_override: float | None = Field(default=None, ge=0, le=1)


class SourcingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(gt=0)
    destination_facility_id: int = Field(gt=0)
    quantity: float = Field(gt=0, le=1e12)
    unit: str = Field(min_length=1, max_length=32)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    objective: Literal["LOWEST_COST", "LOWEST_LANDED_COST", "RELIABILITY", "LOWEST_RISK", "BALANCED", "RESILIENCE"] = (
        "LOWEST_COST"
    )
    strategy: Literal["AUTO", "SINGLE", "DUAL", "MULTI"] = "AUTO"
    fulfillment: Literal["FULL", "PARTIAL"] = "FULL"
    constraints: Constraints = Field(default_factory=Constraints)
    weights: Weights = Field(default_factory=Weights)
    scenario_id: int | None = Field(default=None, gt=0)
    adjustments: list[SupplierAdjustment] = Field(default_factory=list, max_length=50)
    include_transport: bool = False
    use_measured_reliability: bool = True
    period_start: date | None = None
    period_end: date | None = None
    requirement_source: Literal["USER_SUPPLIED"] = "USER_SUPPLIED"
    stockout_finding_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=300)
    as_of: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> SourcingRequest:
        if not math.isfinite(self.quantity):
            raise ValueError("quantity must be finite")
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValueError("period_end cannot be before period_start")
        if len({a.supplier_id for a in self.adjustments}) != len(self.adjustments):
            raise ValueError("at most one adjustment per supplier")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


def objective_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "id": "LOWEST_COST",
            "label": "Lowest purchase cost",
            "minimises": "sum(quantity x unit price)",
            "needs": ["unit_price"],
        },
        {
            "id": "LOWEST_LANDED_COST",
            "label": "Lowest landed sourcing cost",
            "minimises": "sum(quantity x (unit price + stated freight per unit))",
            "needs": ["unit_price", "freight_per_unit"],
            "note": "Offers without an explicit freight figure are set aside for this objective, never costed at zero.",
        },  # fmt: skip
        {
            "id": "RELIABILITY",
            "label": "Reliability-oriented",
            "minimises": "expected late quantity = sum(quantity x (1 - measured on-time rate))",
            "needs": ["measured on-time rate"],
            "note": "Only offers with enough measured deliveries take part.",
        },  # fmt: skip
        {
            "id": "LOWEST_RISK",
            "label": "Risk-aware",
            "minimises": "sum(quantity x current supplier risk score)",
            "needs": ["current supplier risk assessment"],
        },  # fmt: skip
        {
            "id": "BALANCED",
            "label": "Balanced",
            "minimises": "sum(quantity x (1 - suitability score/100)) with a negligible price tie-break",
            "needs": ["scoring weights"],
        },  # fmt: skip
        {
            "id": "RESILIENCE",
            "label": "Resilience-oriented",
            "minimises": "the largest single-supplier share, then purchase cost",
            "needs": ["unit_price"],
            "note": "Reduces dependence on one supplier; it does not model shared regional or upstream exposure.",
        },  # fmt: skip
    ]


def constraint_catalogue() -> list[dict[str, Any]]:
    return [
        {"id": c, "kind": k, "description": d}
        for c, k, d in (
            (
                "demand",
                "hard",
                "Allocated quantity equals the demand (or lies within the stated overage); never negative.",
            ),
            ("capacity", "hard", "No supplier exceeds its stated capacity or contractual maximum allocation."),
            ("minimum_order_quantity", "hard", "A used supplier receives at least its minimum order quantity."),
            ("lot_size", "hard", "A used supplier receives a multiple of its order increment."),
            ("approval", "hard", "Only APPROVED offers with a valid contract window are allocated."),
            ("unit_currency", "hard", "Unit and currency must match the requirement; there is no conversion."),
            (
                "max_lead_time_days / required_by",
                "hard",
                "Offers whose lead time exceeds the limit or the deadline are excluded.",
            ),
            (
                "max_defect_rate / max_risk / min_reliability",
                "hard",
                "Quality, risk and reliability thresholds; an unknown value makes the offer unverifiable.",
            ),  # fmt: skip
            (
                "allowed/excluded suppliers, excluded countries",
                "hard",
                "Approved-supplier lists and geographic restrictions.",
            ),
            ("min/max_supplier_share, min/max_suppliers", "hard", "Diversification and concentration limits."),
            (
                "require_transport_route",
                "hard",
                "Requires a registered route from a supplier facility to the destination (network).",
            ),
        )
    ]
