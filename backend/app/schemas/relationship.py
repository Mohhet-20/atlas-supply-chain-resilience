from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import EntityType, Provenance, RelationshipType
from app.schemas.common import validate_code


class RelationshipCreate(BaseModel):
    """Entities are referenced by stable code. Only valid endpoint combinations are accepted:

    SUPPLIES      = supplier_code -> target_facility_code
    TRANSFERS_TO  = source_facility_code -> target_facility_code
    """

    relationship_code: str = Field(max_length=48)
    relationship_type: RelationshipType
    supplier_code: str | None = None
    source_facility_code: str | None = None
    target_facility_code: str
    capacity_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    capacity_unit: str | None = Field(default=None, max_length=32)
    lead_time_days: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    provenance: Provenance

    @field_validator("relationship_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v, 48)

    @model_validator(mode="after")
    def _endpoints(self) -> RelationshipCreate:
        if self.relationship_type is RelationshipType.SUPPLIES:
            if not self.supplier_code or self.source_facility_code:
                raise ValueError("SUPPLIES requires supplier_code and forbids source_facility_code")
        else:
            if not self.source_facility_code or self.supplier_code:
                raise ValueError("TRANSFERS_TO requires source_facility_code and forbids supplier_code")
            if self.source_facility_code == self.target_facility_code:
                raise ValueError("A facility cannot transfer to itself")
        has_value, has_unit = self.capacity_value is not None, bool(self.capacity_unit and self.capacity_unit.strip())
        if has_value != has_unit:
            raise ValueError("capacity_value and capacity_unit must be provided together")
        return self


class EntityRef(BaseModel):
    type: EntityType
    id: int
    code: str
    name: str


class RelationshipRead(BaseModel):
    id: int
    relationship_code: str
    relationship_type: RelationshipType
    source: EntityRef
    target: EntityRef
    capacity_value: float | None
    capacity_unit: str | None
    lead_time_days: float | None
    provenance: Provenance
    created_at: datetime
