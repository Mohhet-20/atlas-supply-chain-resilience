"""Resilience optimisation (``mo-1``): request schema, limits, vocabulary and the standing statements.

Everything here is decision support. A run never places an order, never changes a stored offer, inventory record or scenario, and every
figure is conditional on the stated inputs and assumptions.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.sourcing.config import Constraints

ALGORITHM_VERSION = "mo-1"
OBJECTIVE_IDS = ("COST", "UNMET_DEMAND", "DELIVERY_DELAY", "CONCENTRATION", "DISRUPTION_EXPOSURE", "WORST_CASE_SERVICE")
STRATEGIES = ("WEIGHTED_SUM", "PARETO", "EPSILON_CONSTRAINT")
STRESS_KINDS = ("SUPPLIER_OUTAGE", "CAPACITY_DROP", "LEAD_TIME_INCREASE", "DEMAND_SURGE", "SCENARIO")
RESULT_STATUSES = (
    "completed",
    "completed_with_warnings",
    "infeasible",
    "timed_out",
    "invalid_input",
    "insufficient_data",
    "failed",
)

STATEMENTS = {
    "decision_support": "A resilience plan is a modelled proposal over the supplier offers registered in ATLAS. It is not a purchase order or a "
    "contract; ATLAS never places an order, and an approval recorded here is an internal decision record only.",
    "pareto": "The Pareto set contains the non-dominated alternatives among the candidate plans that were generated and found feasible. The "
    "candidates come from a discrete grid of supplier shares, so the set is exact for that grid and an approximation of the continuous "
    "frontier. It is not proof that no better plan exists between grid points.",
    "weights": "The recommended plan under a weighted sum depends on the weights and on the normalisation ranges (taken from the feasible "
    "candidates of this run). Different weights can recommend a different plan; the weights and ranges used are stored with the run.",
    "scenarios": "Scenario outcomes use a simplified one-period supply model: a supplier delivers min(allocated quantity, remaining capacity) "
    "if it is available and its lead time fits inside the horizon. Event timing, partial recovery curves and transport lanes are not modelled.",
    "adaptive": "A fixed-plan evaluation keeps the allocation as planned. An adaptive evaluation additionally lets spare capacity of other eligible "
    "suppliers cover a shortfall after the stated response time. The two are never compared without saying which was used.",
    "probabilities": "Scenario probabilities are used only when the requester supplied them for every scenario. Otherwise only individual and "
    "worst-case outcomes are reported; no probability is estimated or assumed.",
    "cost": "Cost is purchase price (plus freight only if every eligible offer states it) plus a shortage penalty only if a shortage cost was supplied. "
    "Components without inputs are listed as omitted and are never costed at zero. Currencies are not mixed or converted.",
    "missing": "A missing input makes the objective that needs it unavailable. It is never read as zero or as favourable.",
    "tenancy": "Data is separated by organisation: every query is limited to the caller's organisation; within it, records are visible to every user at or above the endpoint "
    "role, and costs are reserved for editors and above.",
}


@dataclass(frozen=True)
class Limits:
    max_active_runs: int = 3
    time_budget_seconds: float = 40.0
    max_candidates: int = 40_000  # generated plans per solve
    max_eligible_suppliers: int = 8
    max_scenarios: int = 8
    max_sensitivity_variants: int = 24
    max_alternatives: int = 50
    stale_lock_seconds: float = 300.0
    max_stored_candidates: int = 200


class ObjectiveSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Literal["COST", "UNMET_DEMAND", "DELIVERY_DELAY", "CONCENTRATION", "DISRUPTION_EXPOSURE", "WORST_CASE_SERVICE"]
    weight: float = Field(default=1.0, ge=0, le=1000)

    @model_validator(mode="after")
    def _finite(self) -> ObjectiveSetting:
        if not math.isfinite(self.weight):
            raise ValueError("weight must be finite")
        return self


class Stress(BaseModel):
    """A named stress test applied to COPIES of the candidate terms. ``probability`` is only ever user-supplied."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["SUPPLIER_OUTAGE", "CAPACITY_DROP", "LEAD_TIME_INCREASE", "DEMAND_SURGE", "SCENARIO"]
    supplier_id: int | None = Field(
        default=None, gt=0
    )  # None: the largest supplier of the evaluated plan (outage) / all suppliers
    capacity_multiplier: float | None = Field(default=None, ge=0, lt=1)
    add_lead_days: float | None = Field(default=None, gt=0, le=3650)
    demand_multiplier: float | None = Field(default=None, gt=1, le=10)
    scenario_id: int | None = Field(default=None, gt=0)
    probability: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> Stress:
        need = {"CAPACITY_DROP": self.capacity_multiplier, "LEAD_TIME_INCREASE": self.add_lead_days, "DEMAND_SURGE": self.demand_multiplier, "SCENARIO": self.scenario_id}  # fmt: skip
        if self.kind in need and need[self.kind] is None:
            raise ValueError(f"{self.kind} needs its parameter ({ {'CAPACITY_DROP': 'capacity_multiplier', 'LEAD_TIME_INCREASE': 'add_lead_days', 'DEMAND_SURGE': 'demand_multiplier', 'SCENARIO': 'scenario_id'}[self.kind] })")  # fmt: skip
        return self


class BaselineLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_uid: str = Field(min_length=1, max_length=64)
    quantity: float = Field(ge=0, le=1e12)


class PlanConstraints(BaseModel):
    """Plan-level hard limits on top of supplier eligibility. A limit that is not set is not applied."""

    model_config = ConfigDict(extra="forbid")
    budget_ceiling: float | None = Field(default=None, gt=0, le=1e15)
    min_baseline_fulfilment: float | None = Field(default=None, ge=0, le=1)
    min_worst_case_fulfilment: float | None = Field(default=None, ge=0, le=1)
    max_hhi: float | None = Field(default=None, gt=0, le=1)
    max_delay_days: float | None = Field(default=None, gt=0, le=3650)


class SensitivityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    weights: bool = True
    demand_multipliers: list[float] = Field(default_factory=lambda: [0.9, 1.1], max_length=4)
    capacity_multipliers: list[float] = Field(default_factory=lambda: [0.8], max_length=4)
    add_lead_days: list[float] = Field(default_factory=list, max_length=3)
    budget_multipliers: list[float] = Field(default_factory=list, max_length=3)
    service_targets: list[float] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _ranges(self) -> SensitivityConfig:
        for m in self.demand_multipliers + self.capacity_multipliers + self.budget_multipliers:
            if not (0.05 <= m <= 10) or not math.isfinite(m):
                raise ValueError("multipliers must be between 0.05 and 10")
        if any(not (0 <= d <= 365) for d in self.add_lead_days) or any(not (0 < s <= 1) for s in self.service_targets):
            raise ValueError("lead-time additions must be 0-365 days and service targets in (0, 1]")
        return self


class ResilienceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(gt=0)
    destination_facility_id: int = Field(gt=0)
    quantity: float = Field(gt=0, le=1e12)
    unit: str = Field(min_length=1, max_length=32)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    horizon_days: float = Field(default=30.0, gt=0, le=365)
    constraints: Constraints = Field(default_factory=Constraints)
    plan_constraints: PlanConstraints = Field(default_factory=PlanConstraints)
    allow_partial: bool = False
    objectives: list[ObjectiveSetting] = Field(
        default_factory=lambda: [ObjectiveSetting(id="COST"), ObjectiveSetting(id="CONCENTRATION"), ObjectiveSetting(id="DISRUPTION_EXPOSURE")],
        min_length=1, max_length=6,
    )  # fmt: skip
    strategy: Literal["WEIGHTED_SUM", "PARETO", "EPSILON_CONSTRAINT"] = "WEIGHTED_SUM"
    primary_objective: str | None = None
    epsilon_limits: dict[str, float] = Field(default_factory=dict)
    stress: list[Stress] = Field(default_factory=list, max_length=8)
    scenario_mode: Literal["FIXED", "ADAPTIVE"] = "FIXED"
    response_days: float | None = Field(default=None, ge=0, le=365)
    baseline: list[BaselineLine] = Field(default_factory=list, max_length=40)
    opening_stock: float | None = Field(default=None, ge=0, le=1e12)
    use_inventory_position: bool = False
    shortage_cost_per_unit: float | None = Field(default=None, ge=0, le=1e12)
    grid_steps: int = Field(default=10, ge=2, le=20)
    max_alternatives: int = Field(default=12, ge=1, le=50)
    sensitivity: SensitivityConfig = Field(default_factory=SensitivityConfig)
    note: str | None = Field(default=None, max_length=300)
    as_of: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> ResilienceRequest:
        ids = [o.id for o in self.objectives]
        if len(set(ids)) != len(ids):
            raise ValueError("each objective may be listed once")
        if not math.isfinite(self.quantity):
            raise ValueError("quantity must be finite")
        if self.strategy == "WEIGHTED_SUM" and not any(o.weight > 0 for o in self.objectives):
            raise ValueError("at least one objective weight must be positive")
        if self.strategy == "EPSILON_CONSTRAINT":
            if self.primary_objective not in ids:
                raise ValueError("primary_objective must be one of the listed objectives")
        elif self.primary_objective is not None:
            raise ValueError("primary_objective applies only to the EPSILON_CONSTRAINT strategy")
        for k, v in self.epsilon_limits.items():
            if k not in ids or k == self.primary_objective:
                raise ValueError(f"epsilon limit '{k}' must name another listed objective")
            if not math.isfinite(v):
                raise ValueError("epsilon limits must be finite")
        if self.strategy != "EPSILON_CONSTRAINT" and self.epsilon_limits:
            raise ValueError("epsilon_limits apply only to the EPSILON_CONSTRAINT strategy")
        probs = [s.probability for s in self.stress]
        if any(p is not None for p in probs):
            if any(p is None for p in probs):
                raise ValueError("give a probability for every stress scenario or for none; none is ever assumed")
            if abs(sum(p for p in probs if p is not None) - 1.0) > 1e-6:
                raise ValueError(
                    "scenario probabilities must sum to 1 (they are the probabilities of the listed scenarios only)"
                )
        if len({s.name for s in self.stress}) != len(self.stress):
            raise ValueError("scenario names must be unique")
        if self.scenario_mode == "ADAPTIVE" and self.response_days is None:
            raise ValueError(
                "an adaptive evaluation needs response_days (the time before replacement supply can be arranged)"
            )
        if self.response_days is not None and self.scenario_mode == "FIXED":
            raise ValueError("response_days applies only to an ADAPTIVE evaluation")
        if self.plan_constraints.min_worst_case_fulfilment is not None and not self.stress:
            raise ValueError("min_worst_case_fulfilment needs at least one stress scenario")
        if len({b.offer_uid for b in self.baseline}) != len(self.baseline):
            raise ValueError("baseline lists each offer once")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()
