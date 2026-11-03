"""Inventory & stockout intelligence (``inv-1``): versioned configuration, vocabulary and fixed statements.

Everything here is decision support. Analyses never create orders, change allocations or modify inventory, and a
projection is conditional on its stated inventory, demand and replenishment assumptions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALGORITHM_VERSION = "inv-1"
DEMAND_PRIORITY = ("CONSUMPTION", "DEMAND", "SALES")  # one kind per product-facility; never mixed
DEMAND_KINDS = (*DEMAND_PRIORITY, "ADJUSTMENT")
RECEIPT_STATUSES = ("CONFIRMED", "IN_TRANSIT", "PLANNED", "PROPOSED", "DELAYED", "UNKNOWN")
MAX_STORED_SERIES = 366
MAX_ROW_ERRORS_RETURNED = 200

DISCLAIMER = (
    "Decision support only. A projection is conditional on the inventory, demand and replenishment assumptions listed "
    "with it; it is not a forecast, a probability or an instruction. ATLAS places no orders and changes no inventory."
)
CLASSIFICATION_NOTE = (
    "Classifications are deterministic rules over recorded data (see docs/models.md). They are heuristics, not "
    "calibrated probabilities. Unknown and insufficient-data results are never merged with confirmed shortages."
)


class InventoryConfig(BaseModel):
    """Analyst-selectable settings. Every value is bounded; unknown fields are rejected (no organisation id, no code)."""

    model_config = ConfigDict(extra="forbid")

    as_of: datetime | None = None  # resolved to 'now' at creation and stored so reruns reproduce
    horizon_days: int = Field(default=60, ge=1, le=365)
    demand_window_days: int = Field(default=30, ge=7, le=365)
    demand_basis: Literal["AUTO", "CONSUMPTION", "DEMAND", "SALES"] = "AUTO"
    min_demand_observations: int = Field(default=3, ge=2, le=100)
    min_demand_coverage: float = Field(default=0.5, gt=0, le=1)
    max_inventory_age_days: int = Field(default=7, ge=1, le=365)
    low_coverage_days: float = Field(default=7, ge=1, le=90)
    allocated_overlap: Literal["UNKNOWN", "DISTINCT", "INCLUDED_IN_RESERVED"] = "UNKNOWN"
    policy_basis: Literal["AVAILABLE", "USABLE"] = "AVAILABLE"
    include_planned_receipts: bool = False  # PLANNED receipts are excluded unless the analyst opts in
    scenario_start_offset_days: int = Field(default=0, ge=0, le=365)
    forecast_run_id: int | None = Field(default=None, ge=1, le=2**31 - 1)  # drive the projection with a forecast run
    forecast_conversion_factor: float | None = Field(
        default=None, gt=0, le=1e6
    )  # forecast unit -> inventory unit, if they differ
    receipt_delay_days: float | None = Field(default=None, ge=0, le=180)  # explicit delivery-delay scenario
    delivery_assessment_ids: list[int] | None = Field(
        default=None, max_length=200
    )  # use these assessments' estimated arrivals
    delivery_assessment_basis: Literal["POINT", "UPPER_BOUND"] = "POINT"
    product_ids: list[int] | None = Field(default=None, max_length=200)
    facility_ids: list[int] | None = Field(default=None, max_length=200)

    @field_validator("product_ids", "facility_ids")
    @classmethod
    def _ids(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(i < 1 or i > 2**31 - 1 for i in v):
            raise ValueError("ids must be positive integers")
        return sorted(set(v)) if v is not None else None

    @field_validator("delivery_assessment_ids")
    @classmethod
    def _aids(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and any(i < 1 or i > 2**31 - 1 for i in v):
            raise ValueError("ids must be positive integers")
        return sorted(set(v)) if v else None

    @model_validator(mode="after")
    def _one_delay_source(self) -> InventoryConfig:
        if self.receipt_delay_days is not None and self.delivery_assessment_ids:
            raise ValueError("Use either receipt_delay_days or delivery_assessment_ids, not both.")
        return self

    @property
    def eligible_statuses(self) -> tuple[str, ...]:
        return ("CONFIRMED", "IN_TRANSIT", "PLANNED") if self.include_planned_receipts else ("CONFIRMED", "IN_TRANSIT")


def config_hash(conf: InventoryConfig) -> str:
    raw = json.dumps(conf.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
