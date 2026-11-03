from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MetricState = Literal["ok", "insufficient_data"]


class MetricResult(BaseModel):
    """One deterministic metric. Inputs, calculation, assumptions, coverage and provenance are explicit."""

    key: str
    label: str
    state: MetricState
    value: float | None = None
    unit: str | None = None
    calculation: str
    inputs: dict[str, float | int | str | None] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    coverage: float | None = Field(default=None, ge=0, le=1)
    provenance: dict[str, int] = Field(default_factory=dict)
    message: str | None = None
    details: list[dict[str, float | int | str | None]] = Field(default_factory=list)


class RiskSummary(BaseModel):
    metrics: list[MetricResult]
    disclaimer: str
