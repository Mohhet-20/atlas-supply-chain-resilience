from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import Provenance
from app.models.observation import Observation
from app.schemas.dashboard import DashboardSummary
from app.services.network import load_network_data, provenance_counts
from app.services.queries import recent_observations
from app.services.risk import compute_risk_summary


def build_dashboard_summary(session: Session) -> DashboardSummary:
    data = load_network_data(session)
    risk = compute_risk_summary(data)
    indicator = next(m for m in risk.metrics if m.key == "data_coverage_indicator")
    counts = provenance_counts(session)
    return DashboardSummary(
        supplier_count=len(data.suppliers),
        facility_count=len(data.facilities),
        relationship_count=len(data.relationships),
        observation_count=session.scalar(select(func.count()).select_from(Observation)) or 0,
        provenance_counts=counts,
        contains_synthetic_demo=counts[Provenance.SYNTHETIC_DEMO.value] > 0,
        coverage_indicator=indicator.value,
        coverage_state=indicator.state,
        recent_observations=recent_observations(session, 5),
        risk=risk,
    )
