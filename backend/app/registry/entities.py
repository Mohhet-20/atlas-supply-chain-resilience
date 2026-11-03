"""Type wiring for registry entities (suppliers, facilities, products)."""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.enums import RegistryType
from app.models.facility import Facility
from app.models.product import Product
from app.models.supplier import Supplier

Entity = Supplier | Facility | Product

MODELS: dict[RegistryType, type] = {
    RegistryType.SUPPLIER: Supplier,
    RegistryType.FACILITY: Facility,
    RegistryType.PRODUCT: Product,
}
CODE_ATTR = {
    RegistryType.SUPPLIER: "supplier_code",
    RegistryType.FACILITY: "facility_code",
    RegistryType.PRODUCT: "product_code",
}
CODE_PREFIX = {RegistryType.SUPPLIER: "SUP", RegistryType.FACILITY: "FAC", RegistryType.PRODUCT: "PRD"}
# Fields a registry API client may set. Everything else (id, code, provenance, source, version, tombstone,
# timestamps) is protected against mass assignment.
EDITABLE: dict[RegistryType, tuple[str, ...]] = {
    RegistryType.SUPPLIER: (
        "name",
        "legal_name",
        "country_or_region",
        "category",
        "status",
        "address",
        "parent_supplier_id",
    ),
    RegistryType.FACILITY: (
        "name",
        "facility_type",
        "country_or_region",
        "city",
        "address",
        "latitude",
        "longitude",
        "location_status",
        "capacity_value",
        "capacity_unit",
        "owner_supplier_id",
        "status",
    ),
    RegistryType.PRODUCT: (
        "name",
        "category",
        "unit_of_measure",
        "description",
        "manufacturer_supplier_id",
        "status",
    ),
}
# Values never copied verbatim into audit details (contact/location-like data): only the field name is kept.
SENSITIVE_FIELDS = frozenset({"address", "latitude", "longitude"})


def code_of(entity: Entity) -> str:
    return getattr(entity, CODE_ATTR[_type_of(entity)])


def _type_of(entity: Entity) -> RegistryType:
    for rtype, model in MODELS.items():
        if isinstance(entity, model):
            return rtype
    raise TypeError("not a registry entity")


def type_of(entity: Entity) -> RegistryType:
    return _type_of(entity)


def get_entity(session: Session, rtype: RegistryType, entity_id: int) -> Entity:
    entity = session.get(MODELS[rtype], entity_id)
    if entity is None:
        raise NotFoundError(f"{rtype.value.title()} {entity_id} was not found.")
    return entity


def canonical_of(session: Session, entity: Entity, max_hops: int = 25) -> Entity:
    """Follow merge tombstones to the surviving canonical record."""
    seen = {entity.id}
    current = entity
    for _ in range(max_hops):
        if current.merged_into_id is None:
            return current
        nxt = session.get(type(current), current.merged_into_id)
        if nxt is None or nxt.id in seen:
            return current
        seen.add(nxt.id)
        current = nxt
    return current


def json_safe(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def snapshot(entity: Entity) -> dict[str, Any]:
    """Complete column snapshot (JSON-safe), including version and tombstone columns."""
    return {c.key: json_safe(getattr(entity, c.key)) for c in entity.__table__.columns}


def display(entity: Entity) -> dict[str, Any]:
    return {"type": _type_of(entity).value, "id": entity.id, "code": code_of(entity), "name": entity.name}


def active_entities(session: Session, rtype: RegistryType, ids: list[int] | None = None) -> list[Entity]:
    model = MODELS[rtype]
    stmt = select(model).where(model.merged_into_id.is_(None)).order_by(model.id)
    if ids is not None:
        stmt = stmt.where(model.id.in_(ids))
    return list(session.scalars(stmt))
