from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import EntityStatus, FacilityType, LocationStatus, Provenance
from app.schemas.common import ORMModel, validate_code
from app.schemas.observation import ObservationRead
from app.schemas.relationship import RelationshipRead


class FacilityCreate(BaseModel):
    facility_code: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=200)
    facility_type: FacilityType
    country_or_region: str = Field(min_length=1, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)
    location_status: LocationStatus = LocationStatus.UNKNOWN
    capacity_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    capacity_unit: str | None = Field(default=None, max_length=32)
    owner_supplier_code: str | None = None
    status: EntityStatus = EntityStatus.ACTIVE
    provenance: Provenance

    @model_validator(mode="after")
    def _pairs(self) -> FacilityCreate:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if (self.capacity_value is None) != (self.capacity_unit is None):
            raise ValueError("capacity_value and capacity_unit must be provided together")
        return self

    @field_validator("facility_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v, 32)

    @field_validator("name", "country_or_region")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class OwnerRef(ORMModel):
    id: int
    supplier_code: str
    name: str


class FacilityRead(ORMModel):
    id: int
    facility_code: str
    name: str
    facility_type: FacilityType
    country_or_region: str
    city: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    location_status: LocationStatus = LocationStatus.UNKNOWN
    capacity_value: float | None = None
    capacity_unit: str | None = None
    owner: OwnerRef | None = None
    source_namespace: str | None = None
    external_id: str | None = None
    status: EntityStatus
    provenance: Provenance
    created_at: datetime
    updated_at: datetime


class FacilityDetail(FacilityRead):
    inbound_relationships: list[RelationshipRead]
    outbound_relationships: list[RelationshipRead]
    recent_observations: list[ObservationRead]
