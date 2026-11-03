"""Entity-type specific wiring between canonical records and ORM models."""

from __future__ import annotations

from typing import Any

from app.models.enums import EntityKind
from app.models.facility import Facility
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier

MODEL = {
    EntityKind.SUPPLIER: Supplier,
    EntityKind.FACILITY: Facility,
    EntityKind.PRODUCT: Product,
    EntityKind.RELATIONSHIP: SupplyRelationship,
    EntityKind.PRODUCT_LINK: ProductLink,
}

CODE_ATTR = {
    EntityKind.SUPPLIER: "supplier_code",
    EntityKind.FACILITY: "facility_code",
    EntityKind.PRODUCT: "product_code",
    EntityKind.RELATIONSHIP: "relationship_code",
    EntityKind.PRODUCT_LINK: "link_code",
}

# Canonical scalar fields that map 1:1 to ORM attributes (references are handled separately).
APPLY_FIELDS: dict[EntityKind, tuple[str, ...]] = {
    EntityKind.SUPPLIER: ("name", "legal_name", "country_or_region", "category", "status"),
    EntityKind.FACILITY: (
        "name",
        "facility_type",
        "country_or_region",
        "city",
        "latitude",
        "longitude",
        "location_status",
        "capacity_value",
        "capacity_unit",
        "status",
    ),
    EntityKind.PRODUCT: ("name", "category", "unit_of_measure", "status"),
    EntityKind.RELATIONSHIP: ("relationship_type", "capacity_value", "capacity_unit", "lead_time_days"),
    EntityKind.PRODUCT_LINK: (
        "link_type",
        "quantity_value",
        "quantity_unit",
        "lead_time_days",
        "effective_from",
        "effective_to",
    ),
}

# Pairs that must be provided together (checked on the merged existing+incoming state).
PAIRS: dict[EntityKind, tuple[tuple[str, str], ...]] = {
    EntityKind.FACILITY: (("latitude", "longitude"), ("capacity_value", "capacity_unit")),
    EntityKind.RELATIONSHIP: (("capacity_value", "capacity_unit"),),
    EntityKind.PRODUCT_LINK: (("quantity_value", "quantity_unit"),),
}

# Endpoint reference fields per entity -> ORM foreign-key attribute
REF_ATTR: dict[EntityKind, dict[str, str]] = {
    EntityKind.FACILITY: {"owner_ref": "owner_supplier_id"},
    EntityKind.RELATIONSHIP: {
        "supplier_ref": "supplier_id",
        "source_facility_ref": "source_facility_id",
        "target_facility_ref": "target_facility_id",
    },
    EntityKind.PRODUCT_LINK: {
        "product_ref": "product_id",
        "supplier_ref": "supplier_id",
        "facility_ref": "facility_id",
    },
}

IMMUTABLE_ON_UPDATE: dict[EntityKind, tuple[str, ...]] = {
    EntityKind.RELATIONSHIP: ("relationship_type", "supplier_ref", "source_facility_ref", "target_facility_ref"),
    EntityKind.PRODUCT_LINK: ("link_type", "product_ref", "supplier_ref", "facility_ref"),
}


def external_code(namespace: str, external_id: str) -> str:
    """Internal registry code for an ingested record: namespace-prefixed so sources never collide."""
    return f"{namespace}:{external_id}"


def json_safe(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
