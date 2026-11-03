"""Cascading impact storage: cascading impact analyses and their immutable results.

An analysis is derived from one finished simulation run. Its findings, the relationship records it used (an edge
snapshot) and its criticality/alternative results are insert-only, so a completed analysis stays explainable after the live
network changes. Everything here is hypothetical, structural analysis: nothing is an observed event or a forecast.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import RegistryType, SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow


class ImpactAnalysis(TenantOwned, Base):
    __tablename__ = "impact_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_uid: Mapped[str] = mapped_column(String(40), unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id", ondelete="RESTRICT"))
    scenario_id: Mapped[int] = mapped_column(Integer)
    scenario_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "impact_analysis_status"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    config: Mapped[Any] = mapped_column(JSON)  # effective, validated configuration (after server limits)
    config_hash: Mapped[str] = mapped_column(String(64))
    algorithm_version: Mapped[str] = mapped_column(String(16))
    source_engine_version: Mapped[str | None] = mapped_column(String(16))
    source_run_graph_hash: Mapped[str | None] = mapped_column(String(64))
    graph_hash: Mapped[str | None] = mapped_column(String(64))  # hash of the network facts this analysis read
    graph_basis: Mapped[str | None] = mapped_column(String(24))  # SAME_AS_RUN | CHANGED_SINCE_RUN | UNKNOWN
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    coverage: Mapped[Any] = mapped_column(JSON, nullable=True)
    uncertainty: Mapped[Any] = mapped_column(JSON, nullable=True)
    events: Mapped[Any] = mapped_column(JSON, nullable=True)  # initiating-event explanations
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    failures: Mapped[Any] = mapped_column(JSON, nullable=True)
    excluded: Mapped[Any] = mapped_column(JSON, nullable=True)  # relationships left out (dates, status) and why
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    truncation_reasons: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_impact_analyses_idempotency_key"),
        Index("ix_impact_analyses_run", "run_id", "id"),
        Index("ix_impact_analyses_status", "status"),
        Index(
            "uq_impact_analyses_active_run",
            "run_id",
            unique=True,
            sqlite_where=text("status IN ('QUEUED','RUNNING')"),
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
    )


class ImpactFinding(TenantOwned, Base):
    """One impacted entity in one analysis. Each entity appears once; paths and contributions are recorded, not multiplied."""

    __tablename__ = "impact_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("impact_analyses.id", ondelete="CASCADE"))
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "impact_finding_entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer)
    entity_code: Mapped[str] = mapped_column(String(128))
    entity_name: Mapped[str] = mapped_column(String(200))
    classification: Mapped[str] = mapped_column(String(12))  # DIRECT | PROPAGATED
    min_depth: Mapped[int] = mapped_column(Integer)
    quantification: Mapped[str] = mapped_column(String(16))  # QUANTIFIED | LOWER_BOUND | UNQUANTIFIED | QUALITATIVE
    exposure_level: Mapped[str] = mapped_column(String(16))
    impact_state: Mapped[str] = mapped_column(String(20))  # QUANTIFIED_IMPACT | POTENTIAL_EXPOSURE | INDETERMINATE
    loss_pct: Mapped[float | None] = mapped_column(Float)
    loss_pct_lower_bound: Mapped[float | None] = mapped_column(Float)
    added_lead_time_days: Mapped[float | None] = mapped_column(Float)
    path_count: Mapped[int] = mapped_column(Integer, default=0)
    paths_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    paths: Mapped[Any] = mapped_column(JSON)  # at most a few stored paths (see config), each a list of steps
    event_uids: Mapped[Any] = mapped_column(JSON)
    per_event: Mapped[Any] = mapped_column(JSON)  # contribution of each initiating event on its own
    interaction: Mapped[str] = mapped_column(String(16))  # SINGLE_EVENT | INDEPENDENT | OVERLAPPING | INDETERMINATE
    combined: Mapped[Any] = mapped_column(JSON)  # how events were combined, with bounds and the rule applied
    dimensions: Mapped[Any] = mapped_column(JSON)  # dimension -> status/value/unit/reason/missing
    confidence: Mapped[float | None] = mapped_column(Float)  # weakest recorded edge confidence on the primary path
    evidence_strength: Mapped[str] = mapped_column(String(12))  # LIMITED | MODERATE | STRONGER | UNKNOWN
    confidence_basis: Mapped[Any] = mapped_column(JSON)
    coverage: Mapped[float | None] = mapped_column(Float)  # known / needed inputs; NULL when not applicable
    coverage_detail: Mapped[Any] = mapped_column(JSON)
    data_quality: Mapped[Any] = mapped_column(JSON)
    uncertainty: Mapped[Any] = mapped_column(JSON)
    evidence: Mapped[Any] = mapped_column(JSON)  # relationship refs with status/provenance/confidence/last update
    assumptions: Mapped[Any] = mapped_column(JSON)
    rules: Mapped[Any] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)
    alternatives_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("analysis_id", "entity_type", "entity_id", name="uq_impact_findings_entity"),
        Index("ix_impact_findings_analysis", "analysis_id", "classification", "min_depth"),
    )


class ImpactEdgeSnapshot(TenantOwned, Base):
    """Immutable copy of every relationship record the analysis relied on, so results stay explainable later."""

    __tablename__ = "impact_edge_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("impact_analyses.id", ondelete="CASCADE"))
    edge_ref: Mapped[str] = mapped_column(String(32))
    edge_type: Mapped[str] = mapped_column(String(24))
    source_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "impact_edge_source_type"))
    source_id: Mapped[int] = mapped_column(Integer)
    destination_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "impact_edge_destination_type"))
    destination_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12))
    role: Mapped[str] = mapped_column(String(16))  # PROPAGATION | ALTERNATIVE | CONTEXT
    record: Mapped[Any] = mapped_column(JSON)  # attributes as read: share, capacity, lead time, dates, provenance, ...
    version: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("analysis_id", "edge_ref", name="uq_impact_edge_snapshots_ref"),
        Index("ix_impact_edge_snapshots_analysis", "analysis_id"),
    )


class ImpactCriticality(TenantOwned, Base):
    """Structural indicators for one entity inside the analysed (affected) subgraph. Candidates, not confirmed bottlenecks."""

    __tablename__ = "impact_criticality"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("impact_analyses.id", ondelete="CASCADE"))
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "impact_crit_entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer)
    entity_code: Mapped[str] = mapped_column(String(128))
    entity_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))  # INITIATING | INTERMEDIATE | TERMINAL
    downstream_reachable: Mapped[int] = mapped_column(Integer)
    products_reachable: Mapped[int] = mapped_column(Integer)
    facilities_reachable: Mapped[int] = mapped_column(Integer)
    paths_through: Mapped[int] = mapped_column(Integer)
    would_disconnect: Mapped[int] = mapped_column(Integer)  # entities cut off from all initiating entities if removed
    would_disconnect_sample: Mapped[Any] = mapped_column(JSON)
    sole_known_source_of: Mapped[Any] = mapped_column(
        JSON
    )  # entities for which this is the only confirmed inbound source
    alternatives_for_downstream: Mapped[int] = mapped_column(Integer, default=0)
    bottleneck_candidate: Mapped[bool] = mapped_column(Boolean, default=False)
    reasons: Mapped[Any] = mapped_column(JSON)
    shared_upstream_of: Mapped[Any] = mapped_column(
        JSON
    )  # affected entities that depend on this one together with others
    caveat: Mapped[str] = mapped_column(String(400))

    __table_args__ = (
        UniqueConstraint("analysis_id", "entity_type", "entity_id", name="uq_impact_criticality_entity"),
        Index("ix_impact_criticality_analysis", "analysis_id", "bottleneck_candidate"),
    )


class ImpactAlternative(TenantOwned, Base):
    """A structurally possible alternate path into an affected entity. Never an operational recommendation."""

    __tablename__ = "impact_alternatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    analysis_id: Mapped[int] = mapped_column(ForeignKey("impact_analyses.id", ondelete="CASCADE"))
    for_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "impact_alt_for_type"))
    for_id: Mapped[int] = mapped_column(Integer)
    for_name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "impact_alt_source_type"))
    source_id: Mapped[int] = mapped_column(Integer)
    source_name: Mapped[str] = mapped_column(String(200))
    path: Mapped[Any] = mapped_column(JSON)  # ordered steps from the alternate source to the affected entity
    path_length: Mapped[int] = mapped_column(Integer)
    path_state: Mapped[str] = mapped_column(String(12))  # ACTIVE | PROPOSED | UNCERTAIN | STALE
    feasibility: Mapped[str] = mapped_column(
        String(24)
    )  # always STRUCTURAL_ONLY: nothing here is operationally validated
    capacity_value: Mapped[float | None] = mapped_column(Float)
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    supply_share_pct: Mapped[float | None] = mapped_column(Float)
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    missing: Mapped[Any] = mapped_column(JSON)
    caveat: Mapped[str] = mapped_column(String(400))

    __table_args__ = (Index("ix_impact_alternatives_analysis", "analysis_id", "for_type", "for_id"),)
