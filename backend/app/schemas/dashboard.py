from __future__ import annotations

from pydantic import BaseModel

from app.schemas.observation import ObservationRead
from app.schemas.risk import RiskSummary


class DashboardSummary(BaseModel):
    supplier_count: int
    facility_count: int
    relationship_count: int
    observation_count: int
    provenance_counts: dict[str, int]
    contains_synthetic_demo: bool
    coverage_indicator: float | None
    coverage_state: str
    recent_observations: list[ObservationRead]
    risk: RiskSummary
