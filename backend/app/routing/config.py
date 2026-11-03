"""Routing configuration: request schema, limits, objective definitions and the standing statements.

Everything an analyst may choose is a field of ``RouteRequest``; unknown fields (including any organisation id) are rejected.
Hard constraints (``Constraints``) are separate from soft preferences (the objective and its weights): a constraint decides
whether a route may be returned at all, a preference only orders the routes that already satisfy every constraint.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.transport import MODES

ALGORITHM_VERSION = "rt-1"
CRITERIA = ("cost", "duration", "reliability", "risk")
DIRECTION = {"cost": "min", "duration": "min", "reliability": "max", "risk": "min"}
OBJECTIVES = ("LOWEST_COST", "FASTEST", "MOST_RELIABLE", "LOWEST_RISK", "BALANCED", "CUSTOM")
SINGLE = {"LOWEST_COST": "cost", "FASTEST": "duration", "MOST_RELIABLE": "reliability", "LOWEST_RISK": "risk"}
# BALANCED deliberately leaves out risk: reliability and assessed risk both describe delivery uncertainty, so including both
# would count the same concern twice. A custom objective may include risk explicitly.
BALANCED_WEIGHTS = {"cost": 1 / 3, "duration": 1 / 3, "reliability": 1 / 3}
MAX_HOPS = 8
Uid = Annotated[str, Field(min_length=4, max_length=40, pattern=r"^[A-Za-z0-9-]+$")]
Mode = Literal["ROAD", "RAIL", "SEA", "AIR", "INLAND_WATERWAY", "PIPELINE", "MULTIMODAL"]

STATEMENTS = {
    "decision_support": "Routes are modelled plans over the lanes registered in ATLAS. They are not bookings, carrier schedules or operational "
    "commitments, and a route that satisfies every constraint is plausible and represented in the network, not operationally validated.",
    "cost": "Cost is the sum of per-shipment transport charges on the listed lanes only. It is not landed cost: duties, handling, insurance, "
    "inventory carrying cost and fuel surcharges are excluded unless a lane's figure already includes them. Costs in different currencies "
    "are never added (no exchange rates are applied or assumed).",
    "duration": "Duration is the sum of lane transit times plus the stated per-transfer wait (an assumption supplied in the request, "
    "default 0). It is travel time between registered facilities, not door-to-door lead time, and ignores schedules and departure windows.",
    "reliability": "Route reliability is the product of lane on-time probabilities, which assumes lane outcomes are independent. A lane value "
    "is measured only when the delivery history has at least the minimum sample; otherwise it is a documented prior, or unknown.",
    "risk": "Route risk is the highest current supplier-risk score among the facilities the route passes through (owner-supplier assessments "
    "from supplier risk), reported with the share of facilities that have a score. It is not a probability of disruption.",
    "scenario": "A scenario or request-level disruption changes an in-memory copy of the network. The registered network is never modified, "
    "and the baseline result is computed from the unmodified network for comparison. Event timing windows are not modelled.",
    "tenancy": "Data is separated by organisation: every query is limited to the caller's organisation; within it, lane data and routing runs are visible to every user at or above the endpoint role.",
    "search": "Candidate routes are all loopless routes up to the hop limit, pruned only by bounds that cannot remove a feasible route. "
    "When the search budget is reached the result says so and is reported as best-found, not optimal.",
}


@dataclass(frozen=True)
class Limits:
    max_lanes: int = 5000
    max_active_runs: int = 4
    time_budget_seconds: float = 30.0
    max_expansions: int = 200_000
    min_reliability_sample: int = 20
    max_delivery_rows: int = 50_000
    stale_days: int = 365
    stale_lock_seconds: float = 300.0
    max_candidates: int = 2000


class LaneAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lane_uid: Uid
    duration_multiplier: float = Field(default=1.0, ge=0, le=100)
    cost_multiplier: float = Field(default=1.0, ge=0, le=100)
    capacity_multiplier: float = Field(default=1.0, ge=0, le=100)


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_transit_hours: float | None = Field(default=None, gt=0, le=100_000)
    depart_at: datetime | None = None
    deadline_at: datetime | None = None
    max_cost: float | None = Field(default=None, ge=0, le=1e12)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    min_capacity: float | None = Field(default=None, gt=0, le=1e12)
    capacity_unit: str | None = Field(default=None, min_length=1, max_length=32)
    allowed_modes: list[Mode] | None = Field(default=None, max_length=len(MODES))
    required_modes: list[Mode] = Field(default_factory=list, max_length=len(MODES))
    prohibited_modes: list[Mode] = Field(default_factory=list, max_length=len(MODES))
    max_transfers: int | None = Field(default=None, ge=0, le=MAX_HOPS)
    excluded_lanes: list[Uid] = Field(default_factory=list, max_length=200)
    excluded_facilities: list[int] = Field(default_factory=list, max_length=200)
    required_facilities: list[int] = Field(default_factory=list, max_length=8)
    min_reliability: float | None = Field(default=None, ge=0, le=1)
    max_risk: float | None = Field(default=None, ge=0, le=1)
    cargo_tags: list[Annotated[str, Field(pattern=r"^[A-Z0-9_]{2,32}$")]] = Field(default_factory=list, max_length=16)
    max_data_age_days: float | None = Field(default=None, gt=0, le=36_500)

    @model_validator(mode="after")
    def _check(self) -> Constraints:
        if (self.max_cost is None) != (self.currency is None):
            raise ValueError(
                "max_cost and currency must be given together (costs in different currencies are never compared)."
            )
        if (self.min_capacity is None) != (self.capacity_unit is None):
            raise ValueError("min_capacity and capacity_unit must be given together.")
        if (self.depart_at is None) != (self.deadline_at is None):
            raise ValueError("depart_at and deadline_at must be given together.")
        for name in ("depart_at", "deadline_at"):
            v = getattr(self, name)
            if v is not None and v.tzinfo is None:
                raise ValueError(f"{name} must include a timezone offset.")
        if self.depart_at is not None and self.deadline_at is not None and self.deadline_at <= self.depart_at:
            raise ValueError("deadline_at must be after depart_at.")
        both = set(self.required_modes) & set(self.prohibited_modes)
        if both:
            raise ValueError(f"Modes cannot be both required and prohibited: {', '.join(sorted(both))}.")
        if self.allowed_modes is not None and (miss := set(self.required_modes) - set(self.allowed_modes)):
            raise ValueError(f"Required modes are not in allowed_modes: {', '.join(sorted(miss))}.")
        return self

    def transit_limit_hours(self) -> float | None:
        """The binding time limit: the smaller of max_transit_hours and the departure-to-deadline window."""
        limits = []
        if self.max_transit_hours is not None:
            limits.append(self.max_transit_hours)
        if self.depart_at is not None and self.deadline_at is not None:
            limits.append((self.deadline_at - self.depart_at).total_seconds() / 3600.0)
        return min(limits) if limits else None


class RouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_facility_id: int = Field(ge=1, le=2**31 - 1)
    destination_facility_id: int = Field(ge=1, le=2**31 - 1)
    objective: Literal["LOWEST_COST", "FASTEST", "MOST_RELIABLE", "LOWEST_RISK", "BALANCED", "CUSTOM"] = "BALANCED"
    weights: dict[str, float] | None = None
    constraints: Constraints = Field(default_factory=Constraints)
    alternatives: int = Field(default=3, ge=1, le=10)
    max_hops: int = Field(default=6, ge=1, le=MAX_HOPS)
    transfer_hours: float = Field(default=0.0, ge=0, le=720)
    as_of: datetime | None = None
    scenario_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    lane_closures: list[Uid] = Field(default_factory=list, max_length=200)
    lane_adjustments: list[LaneAdjustment] = Field(default_factory=list, max_length=200)
    use_measured_reliability: bool = True

    @field_validator("as_of")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("as_of must include a timezone offset.")
        return v

    @model_validator(mode="after")
    def _check(self) -> RouteRequest:
        if self.origin_facility_id == self.destination_facility_id:
            raise ValueError("origin and destination must be different facilities.")
        if self.objective == "CUSTOM":
            if not self.weights:
                raise ValueError("A CUSTOM objective needs weights for at least one criterion.")
            bad = [k for k in self.weights if k not in CRITERIA]
            if bad:
                raise ValueError(f"Unknown weight keys: {', '.join(sorted(bad))}. Use: {', '.join(CRITERIA)}.")
            if any((not math.isfinite(v)) or v < 0 for v in self.weights.values()):
                raise ValueError("Weights must be finite and non-negative.")
            if sum(self.weights.values()) <= 0:
                raise ValueError("At least one weight must be positive.")
        elif self.weights is not None:
            raise ValueError("weights are only accepted with objective CUSTOM.")
        return self

    def effective_weights(self) -> dict[str, float]:
        """Normalised weights (sum 1) over the criteria that count; each criterion appears once (no double counting)."""
        if self.objective in SINGLE:
            return {SINGLE[self.objective]: 1.0}
        raw = (
            BALANCED_WEIGHTS
            if self.objective == "BALANCED"
            else {k: v for k, v in (self.weights or {}).items() if v > 0}
        )
        total = sum(raw.values())
        return {k: v / total for k, v in sorted(raw.items())}

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


class RouteCheck(BaseModel):
    """Validate one proposed route (an ordered list of lane uids) against the constraints."""

    model_config = ConfigDict(extra="forbid")
    lane_uids: list[Uid] = Field(min_length=1, max_length=MAX_HOPS)
    constraints: Constraints = Field(default_factory=Constraints)
    transfer_hours: float = Field(default=0.0, ge=0, le=720)
    as_of: datetime | None = None
    scenario_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    lane_closures: list[Uid] = Field(default_factory=list, max_length=200)
    lane_adjustments: list[LaneAdjustment] = Field(default_factory=list, max_length=200)
    use_measured_reliability: bool = True

    @field_validator("as_of")
    @classmethod
    def _tz(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("as_of must include a timezone offset.")
        return v
