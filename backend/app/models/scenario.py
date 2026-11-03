"""Scenario storage: scenario definitions, their events, append-only history, simulation runs and persisted results.

A scenario is a *definition* (hypothetical events). A run is an *execution* that stores a frozen copy of the definition it
used, so editing a scenario never rewrites what an earlier run analysed. Nothing here is an observed event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import (
    EventType,
    EventValidation,
    ImpactKind,
    Quantification,
    RegistryType,
    ScenarioStatus,
    ScenarioType,
    SimStatus,
)
from app.models.types import UTCDateTime, enum_column, utcnow


class Scenario(TenantOwned, Base):
    __tablename__ = "scenarios"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_uid: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(String(2000))
    scenario_type: Mapped[ScenarioType] = mapped_column(enum_column(ScenarioType, "scenario_type"))
    template_key: Mapped[str | None] = mapped_column(String(48))  # which template pre-filled the assumptions, if any
    status: Mapped[ScenarioStatus] = mapped_column(enum_column(ScenarioStatus, "scenario_status"))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # +1 on every definition change
    assumptions: Mapped[Any] = mapped_column(JSON)  # analyst-reviewed simulation assumptions (see scenarios/config.py)
    config_version: Mapped[str] = mapped_column(String(16))  # simulation configuration the assumptions were written for
    notes: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    validated_version: Mapped[int | None] = mapped_column(Integer)  # the version that last passed validation

    __table_args__ = (
        CheckConstraint("version >= 1", name="scenario_version_positive"),
        Index("ix_scenarios_status", "status", "id"),
        Index("ix_scenarios_owner", "owner"),
    )


class ScenarioEvent(TenantOwned, Base):
    __tablename__ = "scenario_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_uid: Mapped[str] = mapped_column(String(40), unique=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    event_type: Mapped[EventType] = mapped_column(enum_column(EventType, "scenario_event_type"))
    label: Mapped[str | None] = mapped_column(String(120))
    target_type: Mapped[RegistryType | None] = mapped_column(enum_column(RegistryType, "scenario_event_target_type"))
    target_id: Mapped[int | None] = mapped_column(Integer)
    relationship_ref: Mapped[str | None] = mapped_column(String(32))  # SUPPLY:12 / PRODUCT:7
    targets: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # REGION_DISRUPTION / CUSTOM: [{"type": "FACILITY", "id": 3}]
    region_label: Mapped[str | None] = mapped_column(String(120))  # a label only; never used to select entities
    effect: Mapped[str | None] = mapped_column(String(16))  # CUSTOM: UNAVAILABLE | REDUCTION | LEAD_TIME
    magnitude_pct: Mapped[float | None] = mapped_column(Float)  # share of capacity lost (0 < x <= 100)
    added_lead_time_days: Mapped[float | None] = mapped_column(Float)
    start_offset_days: Mapped[int | None] = mapped_column(Integer)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    recovery_days: Mapped[int | None] = mapped_column(Integer)
    assumption_notes: Mapped[str | None] = mapped_column(String(1000))
    input_provenance: Mapped[str | None] = mapped_column(String(300))  # where the event idea came from, if real
    validation_status: Mapped[EventValidation] = mapped_column(
        enum_column(EventValidation, "scenario_event_validation"), default=EventValidation.UNVALIDATED
    )
    validation_messages: Mapped[Any] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    # Value ranges are enforced by the validation service (a draft can hold, and explain, an invalid value).
    __table_args__ = (Index("ix_scenario_events_scenario", "scenario_id", "position"),)


class ScenarioHistory(TenantOwned, Base):
    """Append-only record of meaningful definition changes (the full definition is kept so any version can be shown)."""

    __tablename__ = "scenario_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    summary: Mapped[str] = mapped_column(String(300))
    definition: Mapped[Any] = mapped_column(JSON)

    __table_args__ = (Index("ix_scenario_history_scenario", "scenario_id", "id"),)


class SimulationRun(TenantOwned, Base):
    __tablename__ = "simulation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenarios.id", ondelete="RESTRICT"))
    scenario_version: Mapped[int] = mapped_column(Integer)
    definition_hash: Mapped[str] = mapped_column(String(64))
    scenario_snapshot: Mapped[Any] = mapped_column(JSON)  # frozen copy of the definition that was analysed
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "simulation_run_status"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    engine_version: Mapped[str | None] = mapped_column(String(16))
    config_hash: Mapped[str | None] = mapped_column(String(64))
    assumptions: Mapped[Any] = mapped_column(JSON, nullable=True)  # effective assumptions after server limits applied
    graph_snapshot: Mapped[Any] = mapped_column(
        JSON, nullable=True
    )  # input references (counts + newest change), not a copy
    graph_hash: Mapped[str | None] = mapped_column(String(64))
    entities_examined: Mapped[int] = mapped_column(Integer, default=0)
    entities_affected: Mapped[int] = mapped_column(Integer, default=0)
    direct_count: Mapped[int] = mapped_column(Integer, default=0)
    indirect_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    missing_data: Mapped[Any] = mapped_column(JSON, nullable=True)
    failures: Mapped[Any] = mapped_column(JSON, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    truncation_reasons: Mapped[Any] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_simulation_runs_idempotency_key"),
        Index("ix_simulation_runs_scenario", "scenario_id", "id"),
        Index("ix_simulation_runs_status", "status"),
        Index(
            "uq_simulation_runs_active_scenario",
            "scenario_id",
            unique=True,
            sqlite_where=text("status IN ('QUEUED','RUNNING')"),
            postgresql_where=text("status IN ('QUEUED','RUNNING')"),
        ),
    )


class SimulationImpact(TenantOwned, Base):
    """One affected entity in one run. Each entity appears once; multiple paths are recorded, not multiplied."""

    __tablename__ = "simulation_impacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id", ondelete="CASCADE"))
    entity_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "simulation_impact_entity_type"))
    entity_id: Mapped[int] = mapped_column(Integer)
    entity_code: Mapped[str] = mapped_column(String(128))
    entity_name: Mapped[str] = mapped_column(String(200))
    impact_kind: Mapped[ImpactKind] = mapped_column(enum_column(ImpactKind, "simulation_impact_kind"))
    depth: Mapped[int] = mapped_column(Integer)
    event_uids: Mapped[Any] = mapped_column(JSON)
    quantification: Mapped[Quantification] = mapped_column(
        enum_column(Quantification, "simulation_impact_quantification")
    )
    exposure_level: Mapped[str] = mapped_column(String(16))  # DIRECT | HIGH | PARTIAL | UNQUANTIFIED | QUALITATIVE
    loss_pct: Mapped[float | None] = mapped_column(Float)  # known share of inflow at risk; NULL when unknown
    loss_pct_lower_bound: Mapped[float | None] = mapped_column(Float)
    capacity_lost_value: Mapped[float | None] = mapped_column(Float)
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    added_lead_time_days: Mapped[float | None] = mapped_column(Float)
    path_count: Mapped[int] = mapped_column(Integer, default=1)
    primary_path: Mapped[Any] = mapped_column(JSON)  # [{"ref","type","from","to"}] from the disrupted entity
    parent_edges: Mapped[Any] = mapped_column(JSON)  # inbound affected edge refs (at most 10)
    rules: Mapped[Any] = mapped_column(JSON)
    uncertainty: Mapped[Any] = mapped_column(JSON)
    explanation: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("run_id", "entity_type", "entity_id", name="uq_simulation_impacts_entity"),
        Index("ix_simulation_impacts_run", "run_id", "impact_kind"),
    )


class SimulationEdgeImpact(TenantOwned, Base):
    __tablename__ = "simulation_edge_impacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id", ondelete="CASCADE"))
    edge_ref: Mapped[str] = mapped_column(String(32))
    edge_type: Mapped[str] = mapped_column(String(24))
    source_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "simulation_edge_source_type"))
    source_id: Mapped[int] = mapped_column(Integer)
    destination_type: Mapped[RegistryType] = mapped_column(
        enum_column(RegistryType, "simulation_edge_destination_type")
    )
    destination_id: Mapped[int] = mapped_column(Integer)
    effect: Mapped[str] = mapped_column(String(16))  # UNAVAILABLE | REDUCED | DELAYED | EXPOSED
    supply_share_pct: Mapped[float | None] = mapped_column(Float)
    capacity_value: Mapped[float | None] = mapped_column(Float)
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    added_lead_time_days: Mapped[float | None] = mapped_column(Float)
    event_uids: Mapped[Any] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("run_id", "edge_ref", name="uq_simulation_edge_impacts_ref"),
        Index("ix_simulation_edge_impacts_run", "run_id"),
    )


class SimulationAlternative(TenantOwned, Base):
    """A known alternative source for an affected entity. For analyst review only; never an action or a recommendation."""

    __tablename__ = "simulation_alternatives"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("simulation_runs.id", ondelete="CASCADE"))
    for_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "simulation_alt_for_type"))
    for_id: Mapped[int] = mapped_column(Integer)
    source_type: Mapped[RegistryType] = mapped_column(enum_column(RegistryType, "simulation_alt_source_type"))
    source_id: Mapped[int] = mapped_column(Integer)
    source_code: Mapped[str] = mapped_column(String(128))
    source_name: Mapped[str] = mapped_column(String(200))
    edge_ref: Mapped[str] = mapped_column(String(32))
    edge_type: Mapped[str] = mapped_column(String(24))
    edge_status: Mapped[str] = mapped_column(String(16))
    supply_share_pct: Mapped[float | None] = mapped_column(Float)
    capacity_value: Mapped[float | None] = mapped_column(Float)
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    lead_time_days: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    missing: Mapped[Any] = mapped_column(JSON)
    caveat: Mapped[str] = mapped_column(String(400))

    __table_args__ = (
        UniqueConstraint("run_id", "for_type", "for_id", "edge_ref", name="uq_simulation_alternatives_edge"),
        Index("ix_simulation_alternatives_run", "run_id", "for_type", "for_id"),
    )
