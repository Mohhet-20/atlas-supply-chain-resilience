"""Lane registration and availability. A lane's figures are immutable; a changed figure is a new lane and the old one is retired."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, NotFoundError
from app.models.enums import EntityStatus, Provenance
from app.models.facility import Facility
from app.models.relationship import SupplyRelationship
from app.models.transport import MODES, TransportLane
from app.models.types import utcnow
from app.routing.config import Mode

Quality = Literal["VERIFIED", "ESTIMATED", "UNKNOWN"]
Tag = Annotated[str, Field(pattern=r"^[A-Z0-9_]{2,40}$")]


class LaneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_facility_id: int = Field(ge=1, le=2**31 - 1)
    destination_facility_id: int = Field(ge=1, le=2**31 - 1)
    mode: Mode
    relationship_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    distance_km: float | None = Field(default=None, ge=0, le=100_000)
    duration_hours: float | None = Field(default=None, ge=0, le=100_000)
    duration_quality: Quality = "UNKNOWN"
    cost_amount: float | None = Field(default=None, ge=0, le=1e12)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    cost_quality: Quality = "UNKNOWN"
    capacity_amount: float | None = Field(default=None, gt=0, le=1e12)
    capacity_unit: str | None = Field(default=None, min_length=1, max_length=32)
    reliability_prior: float | None = Field(default=None, ge=0, le=1)
    restrictions: list[Tag] = Field(default_factory=list, max_length=20)
    provenance: Literal["SYNTHETIC_DEMO", "USER_PROVIDED", "IMPORTED_EXTERNAL", "MODEL_ESTIMATE", "SIMULATED"] = (
        "USER_PROVIDED"
    )
    source_note: str | None = Field(default=None, max_length=300)
    observed_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None

    @model_validator(mode="after")
    def _check(self) -> LaneIn:
        if self.origin_facility_id == self.destination_facility_id:
            raise ValueError("A lane must connect two different facilities.")
        if (self.cost_amount is None) != (self.currency is None):
            raise ValueError("cost_amount and currency must be given together.")
        if (self.capacity_amount is None) != (self.capacity_unit is None):
            raise ValueError("capacity_amount and capacity_unit must be given together.")
        if self.duration_hours is None and self.duration_quality != "UNKNOWN":
            raise ValueError("duration_quality must be UNKNOWN when no duration is given.")
        if self.cost_amount is None and self.cost_quality != "UNKNOWN":
            raise ValueError("cost_quality must be UNKNOWN when no cost is given.")
        for n in ("observed_at", "effective_from", "effective_to"):
            v = getattr(self, n)
            if v is not None and v.tzinfo is None:
                raise ValueError(f"{n} must include a timezone offset.")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not be before effective_from.")
        return self


def create(session: Session, body: LaneIn, actor: str) -> TransportLane:
    if body.mode not in MODES:
        raise DomainValidationError(f"Unsupported mode {body.mode}.")
    for label, fid in (("origin", body.origin_facility_id), ("destination", body.destination_facility_id)):
        f = session.get(Facility, fid)
        if f is None:
            raise NotFoundError(f"The {label} facility {fid} does not exist.")
        if f.merged_into_id is not None:
            raise ConflictError(
                f"The {label} facility {fid} was merged into {f.merged_into_id}; register the lane on the surviving facility."
            )
        if f.status is not EntityStatus.ACTIVE:
            raise ConflictError(
                f"The {label} facility {fid} is {f.status.value}; lanes can only be registered on ACTIVE facilities."
            )
    if body.relationship_id is not None:
        rel = session.get(SupplyRelationship, body.relationship_id)
        if rel is None:
            raise NotFoundError(f"Relationship {body.relationship_id} does not exist.")
    row = TransportLane(
        lane_uid=f"LN-{uuid.uuid4().hex[:16].upper()}", origin_facility_id=body.origin_facility_id, destination_facility_id=body.destination_facility_id,
        mode=body.mode, relationship_id=body.relationship_id, distance_km=body.distance_km, duration_hours=body.duration_hours,
        duration_quality=body.duration_quality, cost_amount=body.cost_amount, currency=body.currency, cost_quality=body.cost_quality,
        capacity_amount=body.capacity_amount, capacity_unit=body.capacity_unit, reliability_prior=body.reliability_prior,
        restrictions=sorted(set(body.restrictions)), status="ACTIVE", provenance=Provenance(body.provenance), source_note=body.source_note,
        observed_at=body.observed_at, effective_from=body.effective_from, effective_to=body.effective_to, created_by=actor, created_at=utcnow(),
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row


def get(session: Session, uid: str) -> TransportLane:
    row = session.scalar(select(TransportLane).where(TransportLane.lane_uid == uid))
    if row is None:
        raise NotFoundError("Lane not found.")
    return row


_ALLOWED = {("ACTIVE", "SUSPENDED"), ("SUSPENDED", "ACTIVE"), ("ACTIVE", "RETIRED"), ("SUSPENDED", "RETIRED")}


def set_status(session: Session, row: TransportLane, to: str) -> TransportLane:
    if (row.status, to) not in _ALLOWED:
        raise ConflictError(
            f"A lane cannot move from {row.status} to {to}."
            + (" Retired lanes are final." if row.status == "RETIRED" else "")
        )
    row.status = to
    if to == "RETIRED":
        row.retired_at = utcnow()
    return row


def out(r: TransportLane) -> dict[str, Any]:
    iso = lambda d: None if d is None else d.isoformat()  # noqa: E731
    return {
        "lane_uid": r.lane_uid, "origin_facility_id": r.origin_facility_id, "destination_facility_id": r.destination_facility_id, "mode": r.mode,
        "relationship_id": r.relationship_id, "distance_km": r.distance_km, "duration_hours": r.duration_hours, "duration_quality": r.duration_quality,
        "cost_amount": r.cost_amount, "currency": r.currency, "cost_quality": r.cost_quality, "capacity_amount": r.capacity_amount,
        "capacity_unit": r.capacity_unit, "reliability_prior": r.reliability_prior, "restrictions": list(r.restrictions or []), "status": r.status,
        "provenance": r.provenance.value, "source_note": r.source_note, "observed_at": iso(r.observed_at), "effective_from": iso(r.effective_from),
        "effective_to": iso(r.effective_to), "created_by": r.created_by, "created_at": iso(r.created_at), "retired_at": iso(r.retired_at),
    }  # fmt: skip
