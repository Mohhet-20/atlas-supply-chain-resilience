from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.enums import EntityStatus, Provenance
from app.schemas.common import ORMModel, validate_code
from app.schemas.observation import ObservationRead
from app.schemas.relationship import RelationshipRead


class SupplierCreate(BaseModel):
    """Validated input for creating a supplier (used by seeding and future ingestion)."""

    supplier_code: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    country_or_region: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    status: EntityStatus = EntityStatus.ACTIVE
    provenance: Provenance

    @field_validator("supplier_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return validate_code(v, 32)

    @field_validator("name", "country_or_region", "category")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class SupplierRead(ORMModel):
    id: int
    supplier_code: str
    name: str
    legal_name: str | None = None
    source_namespace: str | None = None
    external_id: str | None = None
    country_or_region: str
    category: str
    status: EntityStatus
    provenance: Provenance
    created_at: datetime
    updated_at: datetime


class SupplierDetail(SupplierRead):
    relationships: list[RelationshipRead]
    recent_observations: list[ObservationRead]
