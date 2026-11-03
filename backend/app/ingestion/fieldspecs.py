"""Canonical field specifications for every ingestable entity type."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.models.enums import (
    EntityKind,
    EntityStatus,
    FacilityType,
    LocationStatus,
    ProductLinkType,
    RelationshipType,
)


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str  # id | text | enum | number | date | ref
    required: bool = False
    max_len: int = 200
    enum: type[Enum] | None = None
    nonneg: bool = False
    minimum: float | None = None
    maximum: float | None = None
    ref_entity: EntityKind | None = None
    description: str = ""


def _s(name, **kw) -> FieldSpec:
    return FieldSpec(name, "text", **kw)


SPECS: dict[EntityKind, list[FieldSpec]] = {
    EntityKind.SUPPLIER: [
        FieldSpec("external_id", "id", required=True, description="Source-assigned identifier"),
        _s("name", required=True),
        _s("legal_name"),
        _s("country_or_region", required=True, max_len=100),
        _s("category", required=True, max_len=100),
        FieldSpec("status", "enum", enum=EntityStatus),
    ],
    EntityKind.FACILITY: [
        FieldSpec("external_id", "id", required=True),
        _s("name", required=True),
        FieldSpec("facility_type", "enum", required=True, enum=FacilityType),
        _s("country_or_region", required=True, max_len=100),
        _s("city", max_len=100),
        FieldSpec("latitude", "number", minimum=-90, maximum=90),
        FieldSpec("longitude", "number", minimum=-180, maximum=180),
        FieldSpec("location_status", "enum", enum=LocationStatus),
        FieldSpec("capacity_value", "number", nonneg=True),
        _s("capacity_unit", max_len=32),
        FieldSpec("owner_ref", "ref", ref_entity=EntityKind.SUPPLIER, description="Owning supplier"),
        FieldSpec("status", "enum", enum=EntityStatus),
    ],
    EntityKind.PRODUCT: [
        FieldSpec("external_id", "id", required=True),
        _s("name", required=True),
        _s("category", max_len=100),
        _s("unit_of_measure", max_len=32),
        FieldSpec("status", "enum", enum=EntityStatus),
    ],
    EntityKind.RELATIONSHIP: [
        FieldSpec("external_id", "id", required=True),
        FieldSpec("relationship_type", "enum", required=True, enum=RelationshipType),
        FieldSpec("supplier_ref", "ref", ref_entity=EntityKind.SUPPLIER),
        FieldSpec("source_facility_ref", "ref", ref_entity=EntityKind.FACILITY),
        FieldSpec("target_facility_ref", "ref", required=True, ref_entity=EntityKind.FACILITY),
        FieldSpec("capacity_value", "number", nonneg=True),
        _s("capacity_unit", max_len=32),
        FieldSpec("lead_time_days", "number", nonneg=True),
    ],
    EntityKind.PRODUCT_LINK: [
        FieldSpec("external_id", "id", required=True),
        FieldSpec("link_type", "enum", required=True, enum=ProductLinkType),
        FieldSpec("product_ref", "ref", required=True, ref_entity=EntityKind.PRODUCT),
        FieldSpec("supplier_ref", "ref", ref_entity=EntityKind.SUPPLIER),
        FieldSpec("facility_ref", "ref", ref_entity=EntityKind.FACILITY),
        FieldSpec("quantity_value", "number", nonneg=True),
        _s("quantity_unit", max_len=32),
        FieldSpec("lead_time_days", "number", nonneg=True),
        FieldSpec("effective_from", "date"),
        FieldSpec("effective_to", "date"),
    ],
}


def spec_map(entity: EntityKind) -> dict[str, FieldSpec]:
    return {s.name: s for s in SPECS[entity]}
