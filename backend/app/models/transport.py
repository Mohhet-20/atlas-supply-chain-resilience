"""Routing storage: transport lanes (the validated transportation network) and immutable routing runs.

A lane is one directed, mode-specific connection between two registered facilities. A lane never invents a value: distance,
duration, cost, capacity and reliability are nullable and carry their own quality marker (VERIFIED / ESTIMATED / UNKNOWN), so a
missing number stays visibly missing. Lane rows are immutable except for their availability status; a changed price or duration
is a new lane row (the old one is retired), so a past routing run can always be traced to the exact data it used.

A routing run is frozen once it reaches a terminal status. A correction is a new run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantOwned
from app.models.enums import Provenance, SimStatus
from app.models.types import UTCDateTime, enum_column, utcnow

MODES = ("ROAD", "RAIL", "SEA", "AIR", "INLAND_WATERWAY", "PIPELINE", "MULTIMODAL")
LANE_STATUSES = ("ACTIVE", "SUSPENDED", "RETIRED")
QUALITY = ("VERIFIED", "ESTIMATED", "UNKNOWN")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} in ({','.join(repr(v) for v in values)})"


class TransportLane(TenantOwned, Base):
    __tablename__ = "transport_lanes"

    id: Mapped[int] = mapped_column(primary_key=True)
    lane_uid: Mapped[str] = mapped_column(String(40), unique=True)
    origin_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    destination_facility_id: Mapped[int] = mapped_column(ForeignKey("facilities.id", ondelete="RESTRICT"))
    mode: Mapped[str] = mapped_column(String(20))
    relationship_id: Mapped[int | None] = mapped_column(ForeignKey("supply_relationships.id", ondelete="RESTRICT"))
    distance_km: Mapped[float | None] = mapped_column(Float)
    duration_hours: Mapped[float | None] = mapped_column(Float)  # transit time on the lane, excluding any transfer wait
    duration_quality: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    cost_amount: Mapped[float | None] = mapped_column(Float)  # per shipment, on this lane only (not landed cost)
    currency: Mapped[str | None] = mapped_column(String(3))
    cost_quality: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    capacity_amount: Mapped[float | None] = mapped_column(Float)  # largest shipment the lane accepts
    capacity_unit: Mapped[str | None] = mapped_column(String(32))
    reliability_prior: Mapped[float | None] = mapped_column(Float)  # assumed on-time probability, NOT a measurement
    restrictions: Mapped[Any] = mapped_column(JSON, nullable=True)  # list of tags, e.g. ["HAZMAT_PROHIBITED"]
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")
    provenance: Mapped[Provenance] = mapped_column(enum_column(Provenance, "transport_lane_provenance"))
    source_note: Mapped[str | None] = mapped_column(String(300))
    observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # when the figures were last confirmed
    effective_from: Mapped[datetime | None] = mapped_column(UTCDateTime)
    effective_to: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        CheckConstraint("origin_facility_id <> destination_facility_id", name="ck_transport_lanes_no_self_loop"),
        CheckConstraint(_in("mode", MODES), name="ck_transport_lanes_mode"),
        CheckConstraint(_in("status", LANE_STATUSES), name="ck_transport_lanes_status"),
        CheckConstraint(_in("duration_quality", QUALITY), name="ck_transport_lanes_duration_quality"),
        CheckConstraint(_in("cost_quality", QUALITY), name="ck_transport_lanes_cost_quality"),
        CheckConstraint("distance_km IS NULL OR distance_km >= 0", name="ck_transport_lanes_distance"),
        CheckConstraint("duration_hours IS NULL OR duration_hours >= 0", name="ck_transport_lanes_duration"),
        CheckConstraint("cost_amount IS NULL OR cost_amount >= 0", name="ck_transport_lanes_cost"),
        CheckConstraint("(cost_amount IS NULL) = (currency IS NULL)", name="ck_transport_lanes_cost_currency_pair"),
        CheckConstraint("capacity_amount IS NULL OR capacity_amount > 0", name="ck_transport_lanes_capacity"),
        CheckConstraint(
            "(capacity_amount IS NULL) = (capacity_unit IS NULL)", name="ck_transport_lanes_capacity_unit_pair"
        ),
        CheckConstraint(
            "reliability_prior IS NULL OR (reliability_prior >= 0 AND reliability_prior <= 1)",
            name="ck_transport_lanes_reliability",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="ck_transport_lanes_window",
        ),
        Index("ix_transport_lanes_origin", "origin_facility_id", "status"),
        Index("ix_transport_lanes_destination", "destination_facility_id", "status"),
        Index("ix_transport_lanes_relationship", "relationship_id"),
    )


class RoutingRun(TenantOwned, Base):
    __tablename__ = "routing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # OPTIMIZE | FEASIBILITY
    status: Mapped[SimStatus] = mapped_column(enum_column(SimStatus, "routing_run_status"))
    result_status: Mapped[str | None] = mapped_column(
        String(24)
    )  # feasible | no_feasible_route | unverifiable | invalid_input
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"))
    requested_by: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    origin_facility_id: Mapped[int] = mapped_column(Integer)
    destination_facility_id: Mapped[int] = mapped_column(Integer)
    scenario_id: Mapped[int | None] = mapped_column(Integer)
    scenario_hash: Mapped[str | None] = mapped_column(String(64))
    request: Mapped[Any] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    network_hash: Mapped[str | None] = mapped_column(String(64))
    network_summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    algorithm_version: Mapped[str] = mapped_column(String(16))
    routes: Mapped[Any] = mapped_column(JSON, nullable=True)
    baseline_routes: Mapped[Any] = mapped_column(JSON, nullable=True)
    comparison: Mapped[Any] = mapped_column(JSON, nullable=True)
    rejections: Mapped[Any] = mapped_column(JSON, nullable=True)
    disruptions: Mapped[Any] = mapped_column(JSON, nullable=True)
    summary: Mapped[Any] = mapped_column(JSON, nullable=True)
    warnings: Mapped[Any] = mapped_column(JSON, nullable=True)
    provenance: Mapped[Any] = mapped_column(JSON, nullable=True)
    reproducibility: Mapped[Any] = mapped_column(JSON, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))

    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_routing_runs_idempotency_key"),
        CheckConstraint("kind in ('OPTIMIZE','FEASIBILITY')", name="ck_routing_runs_kind"),
        Index("ix_routing_runs_pair", "origin_facility_id", "destination_facility_id", "id"),
        Index("ix_routing_runs_scenario", "scenario_id", "id"),
        Index("ix_routing_runs_status", "status", "id"),
    )
