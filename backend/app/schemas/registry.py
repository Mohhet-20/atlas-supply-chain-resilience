"""Request/response contracts for the registry intelligence API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    AliasKind,
    CandidateStatus,
    CandidateTier,
    EntityStatus,
    FacilityType,
    LocationStatus,
    RegistryType,
)

_SCHEME = re.compile(r"^[A-Z][A-Z0-9_]{1,31}\Z")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/#]{0,63}\Z")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _blank_to_none(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    return v or None


class SupplierIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    country_or_region: str = Field(min_length=1, max_length=100)
    category: str = Field(min_length=1, max_length=100)
    status: EntityStatus = EntityStatus.ACTIVE
    address: str | None = Field(default=None, max_length=300)
    parent_supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)


class FacilityIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    facility_type: FacilityType
    country_or_region: str = Field(min_length=1, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_status: LocationStatus | None = None
    capacity_value: float | None = Field(default=None, ge=0)
    capacity_unit: str | None = Field(default=None, max_length=32)
    owner_supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    status: EntityStatus = EntityStatus.ACTIVE

    @model_validator(mode="after")
    def _pairs(self) -> FacilityIn:
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        if (self.capacity_value is None) != (self.capacity_unit is None):
            raise ValueError("capacity_value and capacity_unit must be provided together")
        return self


class ProductIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    unit_of_measure: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=1000)
    manufacturer_supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    status: EntityStatus = EntityStatus.ACTIVE


class _Patch(Strict):
    expected_version: int = Field(ge=1, le=2**31 - 1, description="Record version the client last saw.")


class SupplierPatch(_Patch):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    legal_name: str | None = Field(default=None, max_length=200)
    country_or_region: str | None = Field(default=None, min_length=1, max_length=100)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    status: EntityStatus | None = None
    address: str | None = Field(default=None, max_length=300)
    parent_supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)


class FacilityPatch(_Patch):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    facility_type: FacilityType | None = None
    country_or_region: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, max_length=100)
    address: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    location_status: LocationStatus | None = None
    capacity_value: float | None = Field(default=None, ge=0)
    capacity_unit: str | None = Field(default=None, max_length=32)
    owner_supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    status: EntityStatus | None = None


class ProductPatch(_Patch):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = Field(default=None, max_length=100)
    unit_of_measure: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=1000)
    manufacturer_supplier_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    status: EntityStatus | None = None


CREATE_MODELS = {RegistryType.SUPPLIER: SupplierIn, RegistryType.FACILITY: FacilityIn, RegistryType.PRODUCT: ProductIn}
PATCH_MODELS = {
    RegistryType.SUPPLIER: SupplierPatch,
    RegistryType.FACILITY: FacilityPatch,
    RegistryType.PRODUCT: ProductPatch,
}


class AliasIn(Strict):
    alias: str = Field(min_length=1, max_length=200)
    kind: AliasKind = AliasKind.ALIAS

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: AliasKind) -> AliasKind:
        if v in (AliasKind.MERGED_NAME,):
            raise ValueError("MERGED_NAME aliases are created only by merges")
        return v


class IdentifierIn(Strict):
    source_id: int = Field(ge=1, le=2**31 - 1)
    scheme: str = Field(default="EXTERNAL_ID", description="Upper-case scheme, e.g. EXTERNAL_ID, LEI, DUNS, SKU.")
    value: str = Field(min_length=1, max_length=64)
    authoritative: bool = False

    @field_validator("scheme")
    @classmethod
    def _scheme(cls, v: str) -> str:
        if not _SCHEME.match(v):
            raise ValueError("scheme must be 2-32 upper-case letters, digits or '_'")
        return v

    @field_validator("value")
    @classmethod
    def _value(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError("value must be 1-64 characters: letters/digits then . _ - / #")
        return v


class Reason(Strict):
    reason: str | None = Field(default=None, max_length=1000)


class ReviewDecision(Strict):
    decision: CandidateStatus
    reason: str | None = Field(default=None, max_length=1000)
    acknowledge_identifier_conflict: bool = False


class GenerateRequest(Strict):
    entity_type: RegistryType
    entity_ids: list[int] | None = Field(default=None, max_length=200)


class FieldDecision(Strict):
    choice: Literal["KEEP_SURVIVOR", "USE_MERGED", "CUSTOM"]
    value: Any = None


class MergeRequest(Strict):
    entity_type: RegistryType
    survivor_id: int = Field(ge=1, le=2**31 - 1)
    merged_id: int = Field(ge=1, le=2**31 - 1)
    survivor_version: int = Field(ge=1, le=2**31 - 1)
    merged_version: int = Field(ge=1, le=2**31 - 1)
    field_decisions: dict[str, FieldDecision] = Field(default_factory=dict, max_length=40)
    relationship_decisions: dict[str, str] = Field(
        default_factory=dict,
        max_length=500,
        description="Per conflicting relationship code: DROP_MERGED_DUPLICATE (explicit removal with snapshot).",
    )
    acknowledge_identifier_conflict: bool = False
    candidate_id: int | None = Field(default=None, ge=1, le=2**31 - 1)
    reason: str | None = Field(default=None, min_length=3, max_length=1000)


class CompensateRequest(Strict):
    reason: str = Field(min_length=3, max_length=1000)


class QualityRunRequest(Strict):
    entity_types: list[RegistryType] | None = None


# ------------------------------------------------------------------- responses


class AliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    alias: str
    alias_normalized: str
    kind: AliasKind
    origin_merge_id: int | None
    created_by: str
    created_at: datetime


class IdentifierOut(BaseModel):
    id: int | None
    scheme: str
    value: str
    source_id: int | None
    source_namespace: str | None
    authoritative: bool
    primary: bool = False
    origin_merge_id: int | None = None
    created_by: str | None = None


class CandidateOut(BaseModel):
    id: int
    entity_type: RegistryType
    left_id: int
    right_id: int
    left_name: str | None = None
    right_name: str | None = None
    rule_version: str
    score: float
    tier: CandidateTier
    hard_conflict: bool
    status: CandidateStatus
    evidence: Any
    left_version: int
    right_version: int
    stale: bool = False
    first_seen_at: datetime
    updated_at: datetime
