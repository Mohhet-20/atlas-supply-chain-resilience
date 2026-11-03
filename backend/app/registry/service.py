"""Registry write operations: create/update, aliases, identifiers, lifecycle. Callers own the transaction."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.core.auth import Actor
from app.core.errors import (
    ConflictError,
    DomainValidationError,
    EntityMergedError,
    NotFoundError,
    VersionConflictError,
)
from app.models.enums import EntityStatus, LocationStatus, Provenance, RegistryType
from app.models.registry import EntityAlias, EntityIdentifier
from app.models.source import DataSource
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.registry import audit
from app.registry.entities import (
    CODE_ATTR,
    CODE_PREFIX,
    EDITABLE,
    MODELS,
    SENSITIVE_FIELDS,
    Entity,
    code_of,
    get_entity,
    json_safe,
)
from app.registry.normalize import normalize_identifier, normalize_name
from app.schemas.registry import AliasIn, IdentifierIn

NON_NULLABLE = {
    RegistryType.SUPPLIER: {"name", "country_or_region", "category", "status"},
    RegistryType.FACILITY: {"name", "facility_type", "country_or_region", "status"},
    RegistryType.PRODUCT: {"name", "status"},
}


def _ensure_live(entity: Entity) -> None:
    if entity.merged_into_id is not None:
        raise EntityMergedError(
            f"Record {entity.id} was merged into {entity.merged_into_id} and can no longer be changed; "
            "edit the surviving record instead."
        )


def _supplier_ref(session: Session, value: int | None, label: str) -> None:
    if value is None:
        return
    ref = session.get(Supplier, value)
    if ref is None:
        raise DomainValidationError(f"{label} {value} does not exist.")
    if ref.merged_into_id is not None:
        raise DomainValidationError(f"{label} {value} was merged into {ref.merged_into_id}; use the surviving record.")


def _check_parent_cycle(session: Session, supplier_id: int | None, parent_id: int | None) -> None:
    node = parent_id
    for _ in range(100):
        if node is None:
            return
        if node == supplier_id:
            raise DomainValidationError("parent_supplier_id would create a cycle in the corporate hierarchy.")
        parent = session.get(Supplier, node)
        node = parent.parent_supplier_id if parent else None
    raise DomainValidationError("Corporate hierarchy is too deep to validate.")


def _validate_refs(session: Session, rtype: RegistryType, values: dict[str, Any], own_id: int | None = None) -> None:
    if rtype is RegistryType.SUPPLIER and values.get("parent_supplier_id") is not None:
        _supplier_ref(session, values["parent_supplier_id"], "Parent supplier")
        _check_parent_cycle(session, own_id, values["parent_supplier_id"])
    if rtype is RegistryType.FACILITY:
        _supplier_ref(session, values.get("owner_supplier_id"), "Owner supplier")
    if rtype is RegistryType.PRODUCT:
        _supplier_ref(session, values.get("manufacturer_supplier_id"), "Manufacturer supplier")


def _validate_facility_state(values: dict[str, Any]) -> None:
    if (values.get("latitude") is None) != (values.get("longitude") is None):
        raise DomainValidationError("latitude and longitude must be set together.")
    cap_v, cap_u = values.get("capacity_value"), values.get("capacity_unit")
    if (cap_v is None) != (cap_u is None or not str(cap_u).strip()):
        raise DomainValidationError("capacity_value and capacity_unit must be set together.")
    if values.get("latitude") is None and values.get("location_status") not in (None, LocationStatus.UNKNOWN):
        raise DomainValidationError("location_status must be UNKNOWN when no coordinates are recorded.")


def _clean(rtype: RegistryType, values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values)
    for key, val in list(out.items()):
        if isinstance(val, str):
            val = val.strip()
            out[key] = val or None
    return out


def create_entity(
    session: Session, rtype: RegistryType, payload: BaseModel, actor: Actor, request_id: str | None = None
) -> Entity:
    values = _clean(rtype, payload.model_dump(exclude_unset=False))
    _validate_refs(session, rtype, values)
    if rtype is RegistryType.FACILITY:
        has_coords = values.get("latitude") is not None
        values["location_status"] = values.get("location_status") or (
            LocationStatus.UNVERIFIED if has_coords else LocationStatus.UNKNOWN
        )
        _validate_facility_state(values)
    code = f"USR-{CODE_PREFIX[rtype]}-{uuid.uuid4().hex[:10].upper()}"
    model = MODELS[rtype]
    entity = model(**{CODE_ATTR[rtype]: code}, provenance=Provenance.USER_PROVIDED, **values)
    session.add(entity)
    session.flush()
    audit.record(
        session,
        actor,
        "registry.create",
        target_type=rtype.value,
        target_id=entity.id,
        details={"code": code, "name": entity.name},
        request_id=request_id,
    )
    return entity


def update_entity(
    session: Session,
    rtype: RegistryType,
    entity_id: int,
    patch: BaseModel,
    actor: Actor,
    request_id: str | None = None,
) -> Entity:
    entity = get_entity(session, rtype, entity_id)
    _ensure_live(entity)
    expected = patch.expected_version  # type: ignore[attr-defined]
    if entity.version != expected:
        raise VersionConflictError(
            f"The record was changed by someone else (current version {entity.version}, you sent {expected}). "
            "Reload and retry."
        )
    changes = {k: v for k, v in _clean(rtype, patch.model_dump(exclude_unset=True)).items() if k != "expected_version"}
    unknown = set(changes) - set(EDITABLE[rtype])
    if unknown:  # defence in depth; the schema already forbids extras
        raise DomainValidationError("Fields are not editable.")
    for key in changes:
        if changes[key] is None and key in NON_NULLABLE[rtype]:
            raise DomainValidationError(f"'{key}' cannot be cleared.")
    merged_state = {k: getattr(entity, k) for k in EDITABLE[rtype]} | changes
    _validate_refs(session, rtype, changes, own_id=entity.id)
    if rtype is RegistryType.FACILITY:
        if "latitude" in changes and changes["latitude"] is not None and "location_status" not in changes:
            if entity.location_status is LocationStatus.UNKNOWN:
                merged_state["location_status"] = changes["location_status"] = LocationStatus.UNVERIFIED
        if merged_state.get("latitude") is None:
            merged_state["location_status"] = changes["location_status"] = LocationStatus.UNKNOWN
        _validate_facility_state(merged_state)
    diff: dict[str, Any] = {}
    for key, new in changes.items():
        old = getattr(entity, key)
        if json_safe(old) == json_safe(new):
            continue
        setattr(entity, key, new)
        diff[key] = "changed" if key in SENSITIVE_FIELDS else [json_safe(old), json_safe(new)]
    if not diff:
        return entity
    try:
        session.flush()
    except StaleDataError as exc:
        raise VersionConflictError("The record was changed concurrently. Reload and retry.") from exc
    action = "registry.status_changed" if "status" in diff else "registry.update"
    audit.record(
        session,
        actor,
        action,
        target_type=rtype.value,
        target_id=entity.id,
        details={"changes": diff, "new_version": entity.version},
        request_id=request_id,
    )
    return entity


def retire_product(session: Session, product_id: int, expected_version: int, actor: Actor, rid: str | None) -> Entity:
    entity = get_entity(session, RegistryType.PRODUCT, product_id)
    _ensure_live(entity)
    if entity.version != expected_version:
        raise VersionConflictError(f"Current version is {entity.version}.")
    if entity.retired_at is not None:
        raise ConflictError("Product is already retired.")
    entity.retired_at = utcnow()
    entity.status = EntityStatus.INACTIVE
    session.flush()
    audit.record(
        session, actor, "registry.product_retired", target_type="PRODUCT", target_id=product_id, request_id=rid
    )
    return entity


def reactivate_product(session: Session, product_id: int, expected_version: int, actor: Actor, rid: str | None):
    entity = get_entity(session, RegistryType.PRODUCT, product_id)
    _ensure_live(entity)
    if entity.version != expected_version:
        raise VersionConflictError(f"Current version is {entity.version}.")
    if entity.retired_at is None:
        raise ConflictError("Product is not retired.")
    entity.retired_at = None
    entity.status = EntityStatus.ACTIVE
    session.flush()
    audit.record(
        session, actor, "registry.product_reactivated", target_type="PRODUCT", target_id=product_id, request_id=rid
    )
    return entity


# ---------------------------------------------------------------------- aliases


def add_alias(
    session: Session, rtype: RegistryType, entity_id: int, data: AliasIn, actor: Actor, rid: str | None = None
) -> EntityAlias:
    entity = get_entity(session, rtype, entity_id)
    _ensure_live(entity)
    normalized = normalize_name(data.alias)
    if not normalized:
        raise DomainValidationError("Alias must contain letters or digits.")
    exists = session.scalar(
        select(EntityAlias.id).where(
            EntityAlias.entity_type == rtype,
            EntityAlias.entity_id == entity_id,
            EntityAlias.alias_normalized == normalized,
            EntityAlias.kind == data.kind,
        )
    )
    if exists:
        raise ConflictError("This alias already exists for the record.")
    alias = EntityAlias(
        entity_type=rtype,
        entity_id=entity_id,
        alias=data.alias.strip(),
        alias_normalized=normalized[:255],
        kind=data.kind,
        created_by=actor.name,
    )
    session.add(alias)
    session.flush()
    audit.record(
        session,
        actor,
        "registry.alias_added",
        target_type=rtype.value,
        target_id=entity_id,
        details={"alias": alias.alias, "kind": data.kind.value},
        request_id=rid,
    )
    return alias


def remove_alias(
    session: Session, rtype: RegistryType, entity_id: int, alias_id: int, actor: Actor, rid: str | None = None
) -> None:
    alias = session.get(EntityAlias, alias_id)
    if alias is None or alias.entity_type != rtype or alias.entity_id != entity_id:
        raise NotFoundError("Alias not found.")
    if alias.origin_merge_id is not None:
        raise ConflictError("Aliases created by a merge are part of identity lineage and cannot be removed.")
    session.delete(alias)
    session.flush()
    audit.record(
        session,
        actor,
        "registry.alias_removed",
        target_type=rtype.value,
        target_id=entity_id,
        details={"alias": alias.alias},
        request_id=rid,
    )


# ------------------------------------------------------------------ identifiers


def _primary_holder(session: Session, rtype: RegistryType, source_id: int, value: str) -> int | None:
    model = MODELS[rtype]
    return session.scalar(select(model.id).where(model.source_id == source_id, model.external_id == value))


def add_identifier(
    session: Session, rtype: RegistryType, entity_id: int, data: IdentifierIn, actor: Actor, rid: str | None = None
) -> EntityIdentifier:
    entity = get_entity(session, rtype, entity_id)
    _ensure_live(entity)
    if session.get(DataSource, data.source_id) is None:
        raise DomainValidationError(f"Data source {data.source_id} does not exist.")
    value = normalize_identifier(data.value)
    holder = session.scalar(
        select(EntityIdentifier.entity_id).where(
            EntityIdentifier.entity_type == rtype,
            EntityIdentifier.source_id == data.source_id,
            EntityIdentifier.scheme == data.scheme,
            EntityIdentifier.value == value,
        )
    )
    if holder is None and data.scheme == "EXTERNAL_ID":
        holder = _primary_holder(session, rtype, data.source_id, value)
    if holder is not None:
        if holder == entity_id:
            raise ConflictError("This identifier is already recorded on the record.")
        raise ConflictError(
            f"Identifier {data.scheme}:{value} is already assigned to {rtype.value.lower()} {holder} in this source "
            "namespace. That is a duplicate signal: review it as a candidate rather than reassigning the identifier."
        )
    ident = EntityIdentifier(
        entity_type=rtype,
        entity_id=entity_id,
        source_id=data.source_id,
        scheme=data.scheme,
        value=value,
        authoritative=data.authoritative,
        created_by=actor.name,
    )
    session.add(ident)
    session.flush()
    audit.record(
        session,
        actor,
        "registry.identifier_added",
        target_type=rtype.value,
        target_id=entity_id,
        details={"scheme": data.scheme, "source_id": data.source_id, "authoritative": data.authoritative},
        request_id=rid,
    )
    return ident


def remove_identifier(
    session: Session, rtype: RegistryType, entity_id: int, identifier_id: int, actor: Actor, rid: str | None = None
) -> None:
    ident = session.get(EntityIdentifier, identifier_id)
    if ident is None or ident.entity_type != rtype or ident.entity_id != entity_id:
        raise NotFoundError("Identifier not found.")
    if ident.origin_merge_id is not None:
        raise ConflictError("Identifiers carried over by a merge are identity lineage and cannot be removed.")
    session.delete(ident)
    session.flush()
    audit.record(
        session,
        actor,
        "registry.identifier_removed",
        target_type=rtype.value,
        target_id=entity_id,
        details={"scheme": ident.scheme, "source_id": ident.source_id},
        request_id=rid,
    )


def list_identifiers(session: Session, rtype: RegistryType, entity: Entity) -> list[dict[str, Any]]:
    """Primary (source, external id) pair first, then additional identifiers."""
    rows: list[dict[str, Any]] = []
    if entity.source_id is not None and entity.external_id is not None:
        rows.append(
            {
                "id": None,
                "scheme": "EXTERNAL_ID",
                "value": entity.external_id,
                "source_id": entity.source_id,
                "source_namespace": entity.source_namespace,
                "authoritative": False,
                "primary": True,
                "origin_merge_id": None,
                "created_by": None,
            }
        )
    stmt = (
        select(EntityIdentifier, DataSource.namespace)
        .join(DataSource, DataSource.id == EntityIdentifier.source_id)
        .where(EntityIdentifier.entity_type == rtype, EntityIdentifier.entity_id == entity.id)
        .order_by(EntityIdentifier.id)
    )
    for ident, ns in session.execute(stmt):
        rows.append(
            {
                "id": ident.id,
                "scheme": ident.scheme,
                "value": ident.value,
                "source_id": ident.source_id,
                "source_namespace": ns,
                "authoritative": ident.authoritative,
                "primary": False,
                "origin_merge_id": ident.origin_merge_id,
                "created_by": ident.created_by,
            }
        )
    return rows


def list_aliases(session: Session, rtype: RegistryType, entity_id: int) -> list[EntityAlias]:
    return list(
        session.scalars(
            select(EntityAlias)
            .where(EntityAlias.entity_type == rtype, EntityAlias.entity_id == entity_id)
            .order_by(EntityAlias.id)
        )
    )


__all__ = [name for name in dir() if not name.startswith("_")] + ["code_of"]
