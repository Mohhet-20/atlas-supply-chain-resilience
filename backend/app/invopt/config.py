"""Inventory optimisation (``io-1``): request schema, limits, vocabulary and the standing statements.

Everything here is decision support. A run never creates a purchase order, never changes an inventory record or a stored policy,
and a simulation is conditional on its stated inputs; it is not a forecast of what will happen.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM_VERSION = "io-1"
POLICY_TYPES = ("S_Q", "S_UPTO", "R_S")
SS_METHODS = ("AUTO", "FIXED", "NORMAL", "NORMAL_LT", "BOOTSTRAP")
SERVICE_TYPES = ("CYCLE", "FILL_RATE")

STATEMENTS = {
    "decision_support": "A replenishment recommendation is a modelled proposal over the inventory, demand, lead-time and cost data registered "
    "in ATLAS. It is not a purchase order. ATLAS never places an order, and an approval recorded here is an internal decision record only.",
    "service": "Cycle service level is the probability of NO stockout during a replenishment cycle. Fill rate is the share of demand units "
    "served from stock. They are different measures: a 95% cycle service level does not mean 95% of units are served immediately, and a "
    "run states which one it targets and reports both where it can.",
    "safety_stock": "Normal-based safety stock assumes demand per day is independent and roughly stationary and that lead time is independent "
    "of demand. It is not used for intermittent demand. The bootstrap method resamples observed days independently, so it inherits the "
    "independence assumption and cannot create demand patterns that were never observed.",
    "simulation": "A simulation applies a fixed set of rules to one stated demand path (a historical backtest) or to many sampled paths "
    "(SYNTHETIC Monte Carlo). It shows how a policy would have behaved on those inputs; it is not a guarantee of future results.",
    "optimal": "'Best' always means: lowest simulated relevant cost among the candidate policies listed in the run that satisfy every hard "
    "constraint and the stated service target, on the stated demand paths. It is not a proof of global optimality.",
    "cost": "Holding cost, ordering cost, shortage cost and purchase spend are kept apart and only the components with supplied inputs are "
    "summed. A missing cost input is never read as zero; the omitted components are listed. Inventory value is not holding cost.",
    "missing": "Missing inventory, demand, lead-time or cost data is never read as zero or as favourable. A known zero is distinguished from "
    "an unknown value, and a stale observation is flagged.",
    "tenancy": "Data is separated by organisation: every query is limited to the caller's organisation; within it, records are visible to every user at or above the endpoint "
    "role, and costs are reserved for editors and above.",
}


@dataclass(frozen=True)
class Limits:
    max_active_runs: int = 4
    time_budget_seconds: float = 30.0
    max_sim_steps: int = 3_000_000  # days x replications x candidate policies
    max_replications: int = 1000
    max_sim_days: int = 730
    min_demand_days: int = 14
    min_bootstrap_days: int = 28
    min_lead_time_sample: int = 8
    max_delivery_rows: int = 50_000
    max_pairs_overview: int = 2000
    stale_lock_seconds: float = 300.0


class Costs(BaseModel):
    """Analyst-supplied cost inputs. Any left empty stays unknown (never zero)."""

    model_config = ConfigDict(extra="forbid")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    holding_cost_per_unit_day: float | None = Field(default=None, ge=0, le=1e9)
    ordering_cost: float | None = Field(default=None, ge=0, le=1e12)
    shortage_cost_per_unit: float | None = Field(default=None, ge=0, le=1e12)
    unit_cost: float | None = Field(default=None, ge=0, le=1e12)

    @model_validator(mode="after")
    def _check(self) -> Costs:
        vals = [self.holding_cost_per_unit_day, self.ordering_cost, self.shortage_cost_per_unit, self.unit_cost]
        if any(v is not None and not math.isfinite(v) for v in vals):
            raise ValueError("costs must be finite")
        if any(v is not None for v in vals) and not self.currency:
            raise ValueError("currency is required when any cost is given")
        return self


class Constraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    storage_capacity: float | None = Field(default=None, gt=0, le=1e12)
    budget_per_order: float | None = Field(default=None, gt=0, le=1e15)
    max_order_quantity: float | None = Field(default=None, gt=0, le=1e12)
    min_order_quantity: float | None = Field(default=None, ge=0, le=1e12)
    order_increment: float | None = Field(default=None, gt=0, le=1e12)
    required_within_days: float | None = Field(
        default=None, gt=0, le=3650
    )  # the replenishment must arrive within this many days


class WhatIf(BaseModel):
    """Hypothetical changes applied to COPIES of the inputs. Stored records are never changed."""

    model_config = ConfigDict(extra="forbid")
    demand_multiplier: float = Field(default=1.0, ge=0.1, le=10)
    extra_lead_time_days: float = Field(default=0.0, ge=0, le=365)
    lead_time_multiplier: float = Field(default=1.0, ge=1, le=10)
    supplier_blocked_days: int = Field(
        default=0, ge=0, le=365
    )  # no orders can be placed and open receipts slip by this long
    supplier_blocked_start_day: int = Field(default=0, ge=0, le=365)

    def is_neutral(self) -> bool:
        return (
            self.demand_multiplier == 1.0
            and self.extra_lead_time_days == 0
            and self.lead_time_multiplier == 1.0
            and self.supplier_blocked_days == 0
        )


class ProposedPolicy(BaseModel):
    """An analyst's own policy parameters, to be evaluated (not searched for) by a SIMULATE run."""

    model_config = ConfigDict(extra="forbid")
    reorder_point: float | None = Field(default=None, ge=0, le=1e12)
    order_quantity: float | None = Field(default=None, gt=0, le=1e12)
    order_up_to: float | None = Field(default=None, gt=0, le=1e12)


class ReplenishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(gt=0, le=2**31 - 1)
    facility_id: int = Field(gt=0, le=2**31 - 1)
    policy_type: Literal["S_Q", "S_UPTO", "R_S"] = "S_UPTO"
    service_type: Literal["CYCLE", "FILL_RATE"] = "CYCLE"
    service_target: float = Field(default=0.95, ge=0.5, le=0.9999)
    safety_stock_method: Literal["AUTO", "FIXED", "NORMAL", "NORMAL_LT", "BOOTSTRAP"] = "AUTO"
    order_quantity_method: Literal["AUTO", "EOQ", "FIXED"] = "AUTO"
    fixed_order_quantity: float | None = Field(default=None, gt=0, le=1e12)
    review_period_days: int = Field(default=7, ge=1, le=90)  # used by R_S; continuous review otherwise
    objective: Literal["SERVICE_TARGET", "MIN_TOTAL_COST"] = "SERVICE_TARGET"
    horizon_days: int = Field(default=60, ge=1, le=365)
    demand_window_days: int = Field(default=90, ge=14, le=365)
    demand_basis: Literal["AUTO", "CONSUMPTION", "DEMAND", "SALES"] = "AUTO"
    forecast_run_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    forecast_conversion_factor: float | None = Field(default=None, gt=0, le=1e6)
    supplier_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    lead_time_days: float | None = Field(default=None, gt=0, le=3650)  # analyst-supplied, labelled USER_SUPPLIED
    lead_time_std_days: float | None = Field(default=None, ge=0, le=3650)
    max_inventory_age_days: int = Field(default=7, ge=1, le=365)
    allocated_overlap: Literal["UNKNOWN", "DISTINCT", "INCLUDED_IN_RESERVED"] = "UNKNOWN"
    include_planned_receipts: bool = False
    costs: Costs | None = None
    use_stored_costs: bool = True
    constraints: Constraints = Field(default_factory=Constraints)
    evaluation: Literal["AUTO", "BACKTEST", "MONTE_CARLO", "NONE"] = "AUTO"
    simulation_days: int = Field(default=180, ge=14, le=730)
    replications: int = Field(default=100, ge=1, le=1000)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    backorders: bool = True  # unmet demand is backordered (True) or lost (False)
    scenario_id: int | None = Field(default=None, gt=0, le=2**31 - 1)
    what_if: WhatIf = Field(default_factory=WhatIf)
    proposed: ProposedPolicy | None = None
    as_of: datetime | None = None
    note: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _check(self) -> ReplenishRequest:
        if self.safety_stock_method == "FIXED":
            raise ValueError(
                "FIXED is the current-policy baseline; it is evaluated automatically and cannot be requested as the method"
            )
        if self.order_quantity_method == "FIXED" and self.fixed_order_quantity is None:
            raise ValueError("fixed_order_quantity is required for order_quantity_method FIXED")
        if self.objective == "MIN_TOTAL_COST" and self.policy_type == "R_S" and self.review_period_days < 1:
            raise ValueError("review_period_days must be at least 1")
        if (
            self.constraints.min_order_quantity
            and self.constraints.max_order_quantity
            and self.constraints.min_order_quantity > self.constraints.max_order_quantity
        ):
            raise ValueError("min_order_quantity cannot exceed max_order_quantity")
        pr = self.proposed
        if pr is not None:
            need = {
                "S_Q": (pr.reorder_point, pr.order_quantity),
                "S_UPTO": (pr.reorder_point, pr.order_up_to),
                "R_S": (pr.order_up_to,),
            }[self.policy_type]
            if any(v is None for v in need):
                raise ValueError(f"A proposed {self.policy_type} policy needs " + {"S_Q": "reorder_point and order_quantity", "S_UPTO": "reorder_point and order_up_to", "R_S": "order_up_to"}[self.policy_type])  # fmt: skip
            if self.policy_type == "S_UPTO" and pr.order_up_to <= pr.reorder_point:  # type: ignore[operator]
                raise ValueError("order_up_to must be above reorder_point")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=False)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


def stable_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def catalogue() -> dict[str, Any]:
    return {
        "policy_types": [
            {
                "id": "S_Q",
                "label": "(s, Q) continuous review",
                "rule": "when inventory position <= s, order a fixed quantity Q (a multiple of Q if needed to rise above s)",
            },
            {
                "id": "S_UPTO",
                "label": "(s, S) continuous review",
                "rule": "when inventory position <= s, order up to S",
            },
            {"id": "R_S", "label": "(R, S) periodic review", "rule": "every R days order up to S"},
        ],
        "safety_stock_methods": [
            {
                "id": "NORMAL",
                "label": "Normal demand, fixed lead time",
                "formula": "z x sigma_d x sqrt(L [+ R])",
                "needs": "3+ demand periods, not intermittent",
            },
            {
                "id": "NORMAL_LT",
                "label": "Normal demand and lead time",
                "formula": "z x sqrt((L [+ R]) sigma_d^2 + d^2 sigma_L^2)",
                "needs": "measured or supplied lead-time spread",
            },
            {
                "id": "BOOTSTRAP",
                "label": "Empirical (bootstrap) protection-interval demand",
                "formula": "quantile of resampled demand over the protection interval",
                "needs": "28+ days of observed daily demand",
            },
            {
                "id": "AUTO",
                "label": "Choose the most defensible method for the data",
                "formula": "see docs",
                "needs": "-",
            },
        ],
        "service_types": [
            {
                "id": "CYCLE",
                "label": "Cycle service level",
                "meaning": "probability of no stockout in a replenishment cycle",
            },
            {"id": "FILL_RATE", "label": "Fill rate", "meaning": "share of demand units served from stock"},
        ],
        "objectives": [
            {
                "id": "SERVICE_TARGET",
                "label": "Cheapest policy that meets the service target",
                "needs": "holding and/or ordering cost for a cost ranking",
            },
            {
                "id": "MIN_TOTAL_COST",
                "label": "Lowest holding + ordering + shortage cost",
                "needs": "a shortage cost; otherwise rejected",
            },
        ],
    }
