"""Constants, fixed statements and the validated evaluation configuration."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ALGORITHM_VERSION = "mo-1"

STATES = ("DEVELOPMENT", "EVALUATED", "APPROVED", "DEPRECATED", "ARCHIVED")
# allowed lifecycle moves -> minimum role name (see api); APPROVED needs evidence, never a metric alone
TRANSITIONS: dict[tuple[str, str], str] = {
    ("DEVELOPMENT", "EVALUATED"): "system",
    ("EVALUATED", "APPROVED"): "reviewer",
    ("EVALUATED", "DEVELOPMENT"): "reviewer",
    ("APPROVED", "DEPRECATED"): "reviewer",
    ("EVALUATED", "DEPRECATED"): "reviewer",
    ("DEVELOPMENT", "DEPRECATED"): "reviewer",
    ("DEPRECATED", "ARCHIVED"): "admin",
    ("DEPRECATED", "APPROVED"): "admin",
}
KINDS = ("RECORDED_BACKTEST", "PROSPECTIVE")
SEGMENT_FIELDS = ("supplier", "destination", "product", "lane", "mode", "horizon", "pattern")

STATEMENTS = {
    "registry": "A registered version is a record of a model that exists; registration, a high metric or an evaluation is not an approval for any decision.",
    "recorded": "RECORDED_BACKTEST evidence is what the training run measured on its own chronological validation and test splits. It is re-expressed in the common schema, not re-fitted.",
    "prospective": "PROSPECTIVE evidence compares predictions that ATLAS issued and stored before the outcome was known with what actually happened. Small samples say little.",
    "score": "A model score is not a probability. Only a calibrated probability, supported by held-out calibration evidence, may be read as one; a prediction interval is not a probability of an individual outcome.",
    "attribution": "An attribution describes how the fitted model produced a number. It is not evidence of cause in the real world.",
    "drift": "Drift is a signal for investigation. It does not show that the model has become inaccurate; only labelled outcomes can show deterioration.",
    "card": "A model card is documentation generated from stored records. It is not proof that the model is safe for every business decision.",
    "synthetic": "Results computed on synthetic data verify the software only; they say nothing about real-world performance.",
    "tenancy": "Data is separated by organisation: every query is limited to the caller's organisation; within it, every user at or above the endpoint role sees the registered models, evaluations and explanations. Segment results below the minimum sample are suppressed.",
}


class EvalConfig(BaseModel):
    """What an analyst may choose for an evaluation. Unknown fields (including any organisation id) are rejected."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["RECORDED_BACKTEST", "PROSPECTIVE"] = "RECORDED_BACKTEST"
    min_segment_n: int = Field(default=20, ge=5, le=1000)
    min_calibration_n: int = Field(default=100, ge=40, le=100_000)
    ece_bins: int = Field(default=10, ge=3, le=20)
    segment_by: list[Literal["supplier", "destination", "product", "lane", "mode", "horizon", "pattern"]] = Field(default_factory=list, max_length=4)  # fmt: skip
    max_records: int = Field(default=5000, ge=50, le=20_000)
    interval_level: float = Field(default=0.8, gt=0.5, lt=1.0)

    def canonical(self) -> dict[str, Any]:
        d = self.model_dump()
        d["segment_by"] = sorted(set(d["segment_by"]))
        return d

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.canonical(), sort_keys=True).encode()).hexdigest()


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True)
    max_active_evaluations: int = 4
    time_budget_seconds: float = 60.0
    stale_lock_seconds: int = 600
