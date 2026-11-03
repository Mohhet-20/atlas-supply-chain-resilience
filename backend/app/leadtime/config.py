"""Lead-time configuration (``lt-1``): versioned, bounded, declarative. No code, expressions or model paths are accepted."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ALGORITHM_VERSION = "lt-1"

DEFINITIONS = ("ORDER_TO_RECEIPT", "DISPATCH_TO_ARRIVAL", "ORDER_TO_DISPATCH", "PROMISE_DEVIATION")
UNITS = ("HOURS", "CALENDAR_DAYS", "BUSINESS_DAYS")
COMMITMENTS = ("ORIGINAL", "LATEST")
POINTS = ("ORDER_PLACEMENT", "CONFIRMATION", "DISPATCH")
TARGETS = ("LEAD_TIME", "LATE_RISK")
LEADTIME_MODELS = ("global_median", "supplier_median", "lane_median", "hierarchical_median", "ridge_leadtime")
LEADTIME_BASELINES = ("global_median", "supplier_median", "lane_median", "hierarchical_median")
RISK_MODELS = ("base_rate", "supplier_rate", "logistic_late")
RISK_BASELINES = ("base_rate", "supplier_rate")
EVENT_TYPES = (
    "ORDER_CREATED",
    "ORDER_CONFIRMED",
    "DISPATCHED",
    "ARRIVED",
    "RECEIVED",
    "PARTIAL_RECEIPT",
    "CANCELLED",
    "PROMISE_REVISED",
)

STATEMENTS = {
    "evidence": "Historical supplier performance is evidence about the past, not a guarantee of future delivery. "
    "A lead-time estimate is a conditional statistical estimate and does not replace supplier confirmation.",
    "score": "A model score is not a probability. It is called a calibrated probability only when held-out chronological "
    "calibration evidence meets the documented criteria; otherwise it is an uncalibrated score.",
    "rate": "A historical late-delivery rate describes past deliveries. It is not the probability that a particular upcoming delivery will be late.",
    "synthetic": "Quality results computed on synthetic data verify the software only; they say nothing about real-world predictive validity.",
    "decision": "Decision support only. ATLAS does not place orders, change sourcing, contact carriers or act on any prediction.",
}


@dataclass(frozen=True)
class LeadTimeLimits:
    max_records: int = 20_000
    max_active_runs: int = 4
    time_budget_seconds: float = 60.0
    max_features: int = 40
    max_levels_per_feature: int = 12


class LeadTimeConfig(BaseModel):
    """Everything an analyst may choose for a model run. Unknown fields are rejected (no organisation id, no code)."""

    model_config = ConfigDict(extra="forbid")

    target: Literal["LEAD_TIME", "LATE_RISK"] = "LEAD_TIME"
    definition: Literal["ORDER_TO_RECEIPT", "DISPATCH_TO_ARRIVAL", "ORDER_TO_DISPATCH", "PROMISE_DEVIATION"] = (
        "ORDER_TO_RECEIPT"
    )
    start_event: Literal["ORDER", "CONFIRMATION"] = "ORDER"  # for ORDER_TO_RECEIPT / ORDER_TO_DISPATCH
    unit: Literal["HOURS", "CALENDAR_DAYS", "BUSINESS_DAYS"] = "CALENDAR_DAYS"
    commitment: Literal["ORIGINAL", "LATEST"] = "ORIGINAL"
    prediction_point: Literal["ORDER_PLACEMENT", "CONFIRMATION", "DISPATCH"] = "ORDER_PLACEMENT"
    grace_hours: float = Field(default=0.0, ge=0, le=24 * 14)
    chronology_tolerance_hours: float = Field(default=0.0, ge=0, le=48)
    timezone: str = Field(default="UTC", max_length=64)
    weekend_days: list[int] = Field(default_factory=lambda: [5, 6], max_length=6)  # Monday=0
    holidays: list[str] = Field(default_factory=list, max_length=400)  # ISO dates
    supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    destination_facility_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    product_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    models: list[str] | None = Field(default=None, max_length=8)
    min_group_n: int = Field(default=8, ge=3, le=500)
    train_fraction: float = Field(default=0.6, ge=0.3, le=0.8)
    validation_fraction: float = Field(default=0.2, ge=0.1, le=0.4)
    min_eligible: int = Field(default=40, ge=20, le=100_000)
    interval_level: float = Field(default=0.8, ge=0.5, le=0.99)
    min_improvement: float = Field(default=0.05, ge=0, le=0.5)
    ridge_lambda: float = Field(default=1.0, ge=0, le=1000)
    recency_half_life_days: float | None = Field(default=None, ge=7, le=3650)
    include_documented_exceptions: bool = False
    seed: int = Field(default=11, ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _check(self) -> LeadTimeConfig:
        if self.train_fraction + self.validation_fraction >= 0.95:
            raise ValueError("train_fraction + validation_fraction must leave at least 5% for the held-out test set.")
        allowed = LEADTIME_MODELS if self.target == "LEAD_TIME" else RISK_MODELS
        for m in self.models or []:
            if m not in allowed:
                raise ValueError(f"Unknown model '{m}' for target {self.target}. Approved: {', '.join(allowed)}.")
        if self.target == "LEAD_TIME" and self.definition == "PROMISE_DEVIATION":
            raise ValueError(
                "PROMISE_DEVIATION is an observed deviation, not a lead-time target; use target LATE_RISK."
            )
        if self.unit == "BUSINESS_DAYS" and not self.weekend_days and not self.holidays:
            raise ValueError("BUSINESS_DAYS needs a defined calendar (weekend_days and/or holidays).")
        if any(d < 0 or d > 6 for d in self.weekend_days):
            raise ValueError("weekend_days must be integers 0 (Monday) to 6 (Sunday).")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Unknown time zone '{self.timezone}'.") from exc
        from datetime import date

        for h in self.holidays:
            try:
                date.fromisoformat(h)
            except ValueError as exc:
                raise ValueError(f"Holiday '{h}' is not an ISO date.") from exc
        return self

    def canonical(self) -> dict:
        return self.model_dump(mode="json")

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
