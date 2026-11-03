"""Forecast configuration (``fc-1``): versioned, bounded, declarative. No arbitrary code or expressions are accepted."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ALGORITHM_VERSION = "fc-1"
FREQUENCIES = ("daily", "weekly")
PERIOD_DAYS = {"daily": 1, "weekly": 7}
MODEL_KEYS = (
    "naive", "seasonal_naive", "historical_mean", "moving_average", "ses", "holt_damped", "holt_winters",
    "arima_like", "croston_sba", "lag_ridge",
)  # fmt: skip
BASELINE_KEYS = ("naive", "seasonal_naive", "historical_mean")
DEMAND_KINDS = ("CONSUMPTION", "DEMAND", "SALES")
METRICS = ("mae", "rmse", "wape", "mase", "bias", "smape")
MISSING_POLICIES = ("reject", "interpolate_linear", "carry_forward", "zero_fill_by_contract")
OUTLIER_METHODS = ("mad", "iqr")
OUTLIER_HANDLING = ("preserve", "flag", "transform", "exclude")

STATEMENTS = {
    "forecast": "A forecast is a statistical estimate of future demand conditional on the history it was trained on. "
    "It is not an observation, a commitment or a guarantee.",
    "fit": "A good in-sample fit does not guarantee a good forecast; only out-of-sample, origin-respecting evidence counts.",
    "interval": "A prediction interval is only reported where it is supported by empirical out-of-sample errors; "
    "its coverage is an estimate from limited evidence.",
    "decline": "When history is too short, too sparse or too noisy the platform declines to forecast rather than guess.",
}


@dataclass(frozen=True)
class ForecastLimits:
    max_horizon_daily: int = 90
    max_horizon_weekly: int = 26
    max_history_periods: int = 730
    min_history_periods: int = 14
    max_folds: int = 12
    min_folds_for_selection: int = 3
    max_candidates: int = 10
    max_concurrent_runs: int = 4
    max_series_per_run: int = 1
    max_observations: int = 200_000
    min_interval_calibration_n: int = 9
    stale_lock_seconds: int = 600


class ForecastConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency: Literal["daily", "weekly"] = "daily"
    horizon: int = Field(default=28, ge=1)
    lookback_periods: int = Field(default=365, ge=8)
    origin: date | None = None  # forecast starts here; None = end of the last observed period
    basis: Literal["AUTO", "CONSUMPTION", "DEMAND", "SALES"] = "AUTO"
    unit: str | None = Field(default=None, max_length=32)
    seasonal_period: int | None = Field(default=None, ge=2, le=60)
    candidates: list[str] | None = None  # None = every eligible registered model
    min_train_periods: int | None = Field(default=None, ge=8)
    n_folds: int = Field(default=6, ge=1)
    fold_step: int = Field(default=1, ge=1)
    interval_levels: list[float] = Field(default_factory=lambda: [0.8])
    missing_policy: Literal["reject", "interpolate_linear", "carry_forward", "zero_fill_by_contract"] = (
        "interpolate_linear"
    )
    max_gap_periods: int = Field(default=3, ge=1, le=30)
    max_missing_fraction: float = Field(default=0.2, ge=0.0, le=0.9)
    missing_means_zero: bool = False  # the source contract states that an absent period means zero demand
    max_stale_periods: int = Field(default=3, ge=0, le=60)
    outlier_method: Literal["mad", "iqr"] = "mad"
    outlier_threshold: float = Field(default=3.5, ge=2.0, le=10.0)
    outlier_handling: Literal["preserve", "flag", "transform", "exclude"] = "flag"
    selection_metric: Literal["mase", "wape", "mae", "rmse"] = "mase"
    min_improvement: float = Field(
        default=0.05, ge=0.0, le=0.5
    )  # relative gain over the best baseline to prefer a model
    nonnegative: bool = True
    seed: int = Field(default=0, ge=0, le=2**31 - 1)

    @field_validator("interval_levels")
    @classmethod
    def _levels(cls, v: list[float]) -> list[float]:
        if len(v) > 3 or any(not 0.5 <= x <= 0.99 for x in v):
            raise ValueError("interval_levels: up to 3 levels, each between 0.5 and 0.99")
        return sorted(set(round(x, 4) for x in v))

    @field_validator("candidates")
    @classmethod
    def _cands(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        bad = [k for k in v if k not in MODEL_KEYS]
        if bad:
            raise ValueError(f"unknown model(s): {', '.join(sorted(bad))}")
        if not v or len(set(v)) != len(v):
            raise ValueError("candidates must be a non-empty list without duplicates")
        return list(v)

    @model_validator(mode="after")
    def _combos(self) -> ForecastConfig:
        if self.missing_policy == "zero_fill_by_contract" and not self.missing_means_zero:
            raise ValueError("zero_fill_by_contract requires missing_means_zero=true (a stated source contract)")
        return self

    def check_limits(self, limits: ForecastLimits) -> None:
        cap = limits.max_horizon_daily if self.frequency == "daily" else limits.max_horizon_weekly
        if self.horizon > cap:
            raise ValueError(f"horizon {self.horizon} exceeds the {self.frequency} limit of {cap}")
        if self.lookback_periods > limits.max_history_periods:
            raise ValueError(f"lookback_periods exceeds the limit of {limits.max_history_periods}")
        if self.n_folds > limits.max_folds:
            raise ValueError(f"n_folds exceeds the limit of {limits.max_folds}")
        if self.candidates is not None and len(self.candidates) > limits.max_candidates:
            raise ValueError(f"candidates exceed the limit of {limits.max_candidates}")

    def season(self) -> int | None:
        if self.seasonal_period:
            return self.seasonal_period
        return 7 if self.frequency == "daily" else None

    def canonical(self) -> dict:
        return self.model_dump(mode="json")

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()
