"""Relationship lifecycle (create / update / status) with validation, history and audit - one transaction each."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor, Role
from app.core.errors import (
    DomainValidationError,
    DuplicateRelationshipError,
    EntityMergedError,
    ForbiddenError,
    InvalidTransitionError,
    NotFoundError,
    ReadOnlyRelationshipError,
    VersionConflictError,
)
from app.graph.rules import RULES, EdgeKind, GraphRelType
from app.graph.store import EdgeRec, NodeKey, NodeRec, get_rec, load_nodes, make_ref, parse_ref
from app.models.enums import (
    AuditOutcome,
    EdgeCriticality,
    EdgeStatus,
    ProductLinkType,
    Provenance,
    RegistryType,
    RelationshipType,
)
from app.models.graph import GraphEdgeEvent
from app.models.product import ProductLink
from app.models.relationship import SupplyRelationship
from app.registry import audit
from app.registry.entities import canonical_of, get_entity
from app.schemas.graph import EdgeOut, NodeOut, RelationshipCreate, RelationshipUpdate, StatusChange

ATTRS = (
    "capacity_value", "capacity_unit", "lead_time_days", "supply_share_pct", "criticality", "confidence",
    "effective_from", "effective_to",
)  # fmt: skip
# Allowed status changes. Every transition needs a reviewer; unknown evidence is never promoted automatically.
TRANSITIONS: dict[EdgeStatus, set[EdgeStatus]] = {
    EdgeStatus.PROPOSED: {EdgeStatus.CONFIRMED, EdgeStatus.REJECTED},
    EdgeStatus.CONFIRMED: {EdgeStatus.INACTIVE},
    EdgeStatus.INACTIVE: {EdgeStatus.CONFIRMED},
    EdgeStatus.REJECTED: {EdgeStatus.PROPOSED},
}


# ------------------------------------------------------------------------------------------------ output mapping


def node_out(n: NodeRec, depth: int | None = None) -> NodeOut:
    return NodeOut(
        type=n.type, id=n.id, code=n.code, name=n.name, lifecycle=n.lifecycle, detail=n.detail,
        provenance=n.provenance, depth=depth,
    )  # fmt: skip


def edge_out(e: EdgeRec, hop: int | None = None) -> EdgeOut:
    return EdgeOut(
        ref=e.ref, type=e.type, source_type=e.source[0], source_id=e.source[1], destination_type=e.destination[0],
        destination_id=e.destination[1], status=e.status, status_reason=e.status_reason, code=e.code,
        provenance=e.provenance, source_namespace=e.source_namespace, effective_from=e.effective_from,
        effective_to=e.effective_to, capacity_value=e.capacity_value, capacity_unit=e.capacity_unit,
        lead_time_days=e.lead_time_days, supply_share_pct=e.supply_share_pct,
        criticality=EdgeCriticality(e.criticality) if e.criticality else None, confidence=e.confidence,
        version=e.version, read_only=e.kind is EdgeKind.OWNER, hop=hop, created_by=e.created_by,
        updated_by=e.updated_by, created_at=e.created_at, updated_at=e.updated_at,
    )  # fmt: skip


def resolve_node(
    session: Session, rtype: RegistryType, entity_id: int
) -> tuple[NodeKey, NodeRec, dict[str, Any] | None]:
    """Canonical node for a registry id. A merged record resolves to its survivor (reported via ``resolved_from``)."""
    entity = get_entity(session, rtype, entity_id)
    canonical = canonical_of(session, entity)
    key = (rtype, canonical.id)
    node = load_nodes(session, [key])[key]
    redirected = {"type": rtype.value, "id": entity_id} if canonical.id != entity_id else None
    return key, node, redirected


def _live_endpoint(session: Session, rtype: RegistryType, entity_id: int) -> NodeRec:
    entity = get_entity(session, rtype, entity_id)
    if entity.merged_into_id is not None:
        raise EntityMergedError(
            f"{rtype.value.title()} {entity_id} was merged into {entity.merged_into_id}; use the surviving record."
        )
    if getattr(entity, "retired_at", None) is not None:
        raise DomainValidationError(f"{rtype.value.title()} {entity_id} is retired and cannot take new relationships.")
    return load_nodes(session, [(rtype, entity_id)])[(rtype, entity_id)]


# ----------------------------------------------------------------------------------------------------- history


def record_event(
    session: Session, kind: EdgeKind, row_id: int, action: str, actor_name: str, *, from_status: str | None = None,
    to_status: str | None = None, reason: str | None = None, changes: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> GraphEdgeEvent:  # fmt: skip
    event = GraphEdgeEvent(
        edge_kind=kind.value, edge_id=row_id, action=action, from_status=from_status, to_status=to_status,
        actor=actor_name, reason=reason, changes=audit.safe_details(changes or {}), request_id=request_id,
    )  # fmt: skip
    session.add(event)
    session.flush()
    return event


def history(session: Session, kind: EdgeKind, row_id: int, limit: int = 100) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(GraphEdgeEvent)
        .where(GraphEdgeEvent.edge_kind == kind.value, GraphEdgeEvent.edge_id == row_id)
        .order_by(GraphEdgeEvent.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": e.id, "occurred_at": e.occurred_at, "action": e.action, "from_status": e.from_status,
            "to_status": e.to_status, "actor": e.actor, "reason": e.reason, "changes": e.changes,
        }
        for e in rows
    ]  # fmt: skip


# ---------------------------------------------------------------------------------------------------- helpers


def _find_existing(session: Session, rule_type: GraphRelType, src: int, dst: int) -> EdgeRec | None:
    from app.graph.store import link_rec, supply_rec

    if rule_type in (GraphRelType.SUPPLIES, GraphRelType.TRANSFERS_TO):
        col = (
            SupplyRelationship.supplier_id
            if rule_type is GraphRelType.SUPPLIES
            else SupplyRelationship.source_facility_id
        )
        row = (
            session.scalars(
                select(SupplyRelationship).where(
                    SupplyRelationship.relationship_type == RelationshipType(rule_type.value),
                    col == src,
                    SupplyRelationship.target_facility_id == dst,
                )
            )
            .unique()
            .first()
        )
        return supply_rec(row) if row else None
    col = ProductLink.supplier_id if rule_type is GraphRelType.SUPPLIES_PRODUCT else ProductLink.facility_id
    link = (
        session.scalars(
            select(ProductLink).where(
                ProductLink.link_type == ProductLinkType(rule_type.value), col == src, ProductLink.product_id == dst
            )
        )
        .unique()
        .first()
    )
    return link_rec(link) if link else None


def _editable_row(session: Session, ref: str) -> tuple[EdgeKind, Any]:
    kind, row_id = parse_ref(ref)
    if kind is EdgeKind.OWNER:
        raise ReadOnlyRelationshipError(
            "OWNS relationships are derived from the facility's owner; change the owner in the registry instead."
        )
    row = session.get(SupplyRelationship if kind is EdgeKind.SUPPLY else ProductLink, row_id)
    if row is None:
        raise NotFoundError(f"Relationship {ref} was not found.")
    return kind, row


def _ensure_version(row: Any, expected: int) -> None:
    if row.version != expected:
        raise VersionConflictError(
            f"The relationship changed since you loaded it (you have version {expected}, current is {row.version})."
        )


def _apply_attrs(row: Any, kind: EdgeKind, values: dict[str, Any]) -> None:
    for name, value in values.items():
        column = name
        if kind is EdgeKind.PRODUCT and name == "capacity_value":
            column = "quantity_value"
        elif kind is EdgeKind.PRODUCT and name == "capacity_unit":
            column = "quantity_unit"
        setattr(row, column, value)


def _current(row: Any, kind: EdgeKind, name: str) -> Any:
    column = name
    if kind is EdgeKind.PRODUCT:
        column = {"capacity_value": "quantity_value", "capacity_unit": "quantity_unit"}.get(name, name)
    value = getattr(row, column)
    return value.value if hasattr(value, "value") else value


# --------------------------------------------------------------------------------------------------- mutations


def create_relationship(session: Session, actor: Actor, body: RelationshipCreate, request_id: str | None) -> EdgeRec:
    rule = RULES[body.type]
    if not rule.editable:
        raise ReadOnlyRelationshipError("This relationship type is derived and cannot be created here.")
    if (body.source_type, body.destination_type) != (rule.source, rule.destination):
        raise DomainValidationError(
            f"{body.type.value} runs from {rule.source.value} to {rule.destination.value}; "
            f"got {body.source_type.value} to {body.destination_type.value}."
        )
    if body.status is EdgeStatus.CONFIRMED and actor.role < Role.REVIEWER:
        raise ForbiddenError("Creating a CONFIRMED relationship requires the reviewer role; propose it instead.")
    if body.type is GraphRelType.TRANSFERS_TO and body.source_id == body.destination_id:
        raise DomainValidationError("A facility cannot transfer to itself.")
    _live_endpoint(session, body.source_type, body.source_id)
    _live_endpoint(session, body.destination_type, body.destination_id)
    existing = _find_existing(session, body.type, body.source_id, body.destination_id)
    if existing is not None:
        raise DuplicateRelationshipError(
            f"{body.type.value} already exists between these records as {existing.ref} (status {existing.status.value}); "
            "update or reactivate it instead of creating another."
        )
    attrs = {name: getattr(body, name) for name in ATTRS}
    code = f"GR-{uuid.uuid4().hex[:12].upper()}"
    common = {
        "provenance": Provenance.USER_PROVIDED, "status": body.status, "status_reason": body.justification,
        "confidence": body.confidence, "criticality": body.criticality, "supply_share_pct": body.supply_share_pct,
        "effective_from": body.effective_from, "effective_to": body.effective_to, "lead_time_days": body.lead_time_days,
        "created_by": actor.name, "updated_by": actor.name,
    }  # fmt: skip
    if rule.kind is EdgeKind.SUPPLY:
        row: Any = SupplyRelationship(
            relationship_code=code, relationship_type=RelationshipType(body.type.value),
            supplier_id=body.source_id if body.type is GraphRelType.SUPPLIES else None,
            source_facility_id=body.source_id if body.type is GraphRelType.TRANSFERS_TO else None,
            target_facility_id=body.destination_id, capacity_value=body.capacity_value,
            capacity_unit=body.capacity_unit, **common,
        )  # fmt: skip
    else:
        row = ProductLink(
            link_code=code, link_type=ProductLinkType(body.type.value), product_id=body.destination_id,
            supplier_id=body.source_id if body.type is GraphRelType.SUPPLIES_PRODUCT else None,
            facility_id=None if body.type is GraphRelType.SUPPLIES_PRODUCT else body.source_id,
            quantity_value=body.capacity_value, quantity_unit=body.capacity_unit, **common,
        )  # fmt: skip
    session.add(row)
    session.flush()
    ref = make_ref(rule.kind, row.id)
    record_event(
        session, rule.kind, row.id, "CREATED", actor.name, to_status=body.status.value, reason=body.justification,
        changes={k: v for k, v in attrs.items() if v is not None}, request_id=request_id,
    )  # fmt: skip
    audit.record(
        session, actor, "graph.relationship.create", target_type="relationship", target_id=ref, request_id=request_id,
        details={"type": body.type.value, "source": f"{body.source_type.value}:{body.source_id}",
                 "destination": f"{body.destination_type.value}:{body.destination_id}", "status": body.status.value},
    )  # fmt: skip
    rec = get_rec(session, rule.kind, row.id)
    return _must(rec)


def update_relationship(
    session: Session, actor: Actor, ref: str, body: RelationshipUpdate, request_id: str | None
) -> EdgeRec:
    kind, row = _editable_row(session, ref)
    _ensure_version(row, body.expected_version)
    provided = {n: getattr(body, n) for n in ATTRS if n in body.model_fields_set}
    if not provided:
        raise DomainValidationError("No attributes were provided to change.")
    final = {n: provided[n] if n in provided else _current(row, kind, n) for n in ATTRS}
    if (final["capacity_value"] is None) != (final["capacity_unit"] is None):
        raise DomainValidationError("capacity_value and capacity_unit must be set or cleared together.")
    if final["effective_from"] and final["effective_to"] and final["effective_to"] < final["effective_from"]:
        raise DomainValidationError("effective_to must not be before effective_from.")
    changes = {
        n: [_jsonable(_current(row, kind, n)), _jsonable(v)]
        for n, v in provided.items()
        if _current(row, kind, n) != _enum_val(v)
    }
    if not changes:
        raise DomainValidationError("The provided values are identical to the stored ones.")
    _apply_attrs(row, kind, {n: provided[n] for n in changes})
    row.updated_by = actor.name
    session.flush()
    record_event(
        session, kind, row.id, "UPDATED", actor.name, from_status=row.status.value, to_status=row.status.value,
        reason=body.reason, changes=changes, request_id=request_id,
    )  # fmt: skip
    audit.record(
        session, actor, "graph.relationship.update", target_type="relationship", target_id=ref, request_id=request_id,
        details={"fields": sorted(changes)},
    )  # fmt: skip
    rec = get_rec(session, kind, row.id)
    return _must(rec)


def change_status(session: Session, actor: Actor, ref: str, body: StatusChange, request_id: str | None) -> EdgeRec:
    kind, row = _editable_row(session, ref)
    _ensure_version(row, body.expected_version)
    old = row.status
    if body.status is old:
        raise DomainValidationError(f"The relationship is already {old.value}.")
    if body.status not in TRANSITIONS[old]:
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS[old]))
        raise InvalidTransitionError(f"A {old.value} relationship can only become: {allowed}.")
    if body.status in (EdgeStatus.CONFIRMED, EdgeStatus.PROPOSED):
        rec = get_rec(session, kind, row.id)
        rec = _must(rec)
        _live_endpoint(session, *rec.source)
        _live_endpoint(session, *rec.destination)
    row.status = body.status
    row.status_reason = body.reason
    row.updated_by = actor.name
    session.flush()
    record_event(
        session, kind, row.id, "STATUS_CHANGED", actor.name, from_status=old.value, to_status=body.status.value,
        reason=body.reason, request_id=request_id,
    )  # fmt: skip
    audit.record(
        session, actor, "graph.relationship.status", target_type="relationship", target_id=ref, request_id=request_id,
        outcome=AuditOutcome.SUCCESS, details={"from": old.value, "to": body.status.value},
    )  # fmt: skip
    out = get_rec(session, kind, row.id)
    return _must(out)


def _must(rec: EdgeRec | None) -> EdgeRec:
    if rec is None:  # the row was just written in this transaction; this is a defensive guard
        raise NotFoundError("The relationship could not be read back.")
    return rec


def _enum_val(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _jsonable(v: Any) -> Any:
    v = _enum_val(v)
    return v.isoformat() if hasattr(v, "isoformat") else v
