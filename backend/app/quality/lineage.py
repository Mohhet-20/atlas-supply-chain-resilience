"""Assemble the provenance story of one record from the layers that actually exist.

Layers (each reported only if data is present): source record, ingestion lineage (record level), registry audit
events, merge history, aliases and identifiers, relationship change events. Field-level lineage is not stored:
``changed_fields`` lists the fields an import touched, and the audit trail lists later edits. The hash chain on the
audit trail detects accidental or casual changes to history; it is NOT a tamper-proof guarantee (an actor with
database write access could rebuild it).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models.enums import EntityKind, RegistryType
from app.models.graph import GraphEdgeEvent
from app.models.ingestion import RecordLineage
from app.models.product import ProductLink
from app.models.registry import AuditEvent, EntityAlias, EntityIdentifier, MergeRecord
from app.models.relationship import SupplyRelationship
from app.models.source import DataSource
from app.registry.entities import MODELS

LIMIT = 50
LIMITATIONS = [
    "Lineage is recorded per imported record (batch, row, mapping profile, fingerprint, changed field names), not per field value.",
    "Raw source rows are not stored. Only a fingerprint and the names of fields an import changed are kept.",
    "The audit hash chain makes accidental or casual edits to history detectable; it is not proof against someone with database write access.",
    "Records created or edited by hand have an audit trail but no source record.",
]  # fmt: skip


def lineage(session: Session, kind: EntityKind, entity_id: int) -> dict[str, Any]:
    layers: list[str] = []
    out: dict[str, Any] = {"entity_type": kind.value, "entity_id": entity_id, "limitations": LIMITATIONS}
    edge_kind: str | None = None
    if kind in (EntityKind.SUPPLIER, EntityKind.FACILITY, EntityKind.PRODUCT):
        e = session.get(MODELS[RegistryType(kind.value)], entity_id)
        if e is None:
            raise NotFoundError("Record not found.")
        src = session.get(DataSource, e.source_id) if e.source_id else None
        out["record"] = {
            "name": e.name,
            "provenance": e.provenance.value,
            "created_at": e.created_at,
            "updated_at": e.updated_at,
            "version": e.version,
            "merged_into_id": e.merged_into_id,
            "external_id": e.external_id,
            "source": {"id": src.id, "namespace": src.namespace} if src else None,
        }
        audit_type, audit_id = kind.value, str(entity_id)
        rt = RegistryType(kind.value)
        merges = list(
            session.scalars(
                select(MergeRecord)
                .where(
                    MergeRecord.entity_type == rt,
                    or_(MergeRecord.survivor_id == entity_id, MergeRecord.merged_id == entity_id),
                )
                .order_by(MergeRecord.id.desc())
                .limit(LIMIT)
            )
        )
        out["merges"] = [
            {
                "merge_uid": m.merge_uid,
                "role": "survivor" if m.survivor_id == entity_id else "merged_away",
                "survivor_id": m.survivor_id,
                "merged_id": m.merged_id,
                "status": m.status.value,
                "actor": m.actor,
                "reason": m.reason,
                "created_at": m.created_at,
                "compensated_at": m.compensated_at,
            }
            for m in merges
        ]
        out["aliases"] = [
            {
                "alias": a.alias,
                "kind": a.kind.value,
                "source_id": a.source_id,
                "from_merge": a.origin_merge_id is not None,
                "created_at": a.created_at,
            }
            for a in session.scalars(
                select(EntityAlias)
                .where(EntityAlias.entity_type == rt, EntityAlias.entity_id == entity_id)
                .limit(LIMIT)
            )
        ]
        out["identifiers"] = [
            {
                "scheme": i.scheme,
                "source_id": i.source_id,
                "authoritative": i.authoritative,
                "from_merge": i.origin_merge_id is not None,
                "created_at": i.created_at,
            }
            for i in session.scalars(
                select(EntityIdentifier)
                .where(EntityIdentifier.entity_type == rt, EntityIdentifier.entity_id == entity_id)
                .limit(LIMIT)
            )
        ]
        if merges:
            layers.append("merge_history")
        if out["aliases"] or out["identifiers"]:
            layers.append("aliases_identifiers")
    else:
        if kind is EntityKind.RELATIONSHIP:
            r = session.get(SupplyRelationship, entity_id)
            if r is None:
                raise NotFoundError("Relationship not found.")
            edge_kind, audit_id = "SUPPLY", f"SUPPLY:{entity_id}"
            out["record"] = {
                "name": r.relationship_code,
                "status": r.status.value,
                "provenance": r.provenance.value,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
        else:
            link = session.get(ProductLink, entity_id)
            if link is None:
                raise NotFoundError("Product link not found.")
            edge_kind, audit_id = "PRODUCT", f"PRODUCT:{entity_id}"
            out["record"] = {
                "name": link.link_code,
                "status": link.status.value,
                "provenance": link.provenance.value,
                "created_at": link.created_at,
                "updated_at": link.updated_at,
            }
        audit_type = "relationship"
        out["merges"], out["aliases"], out["identifiers"] = [], [], []
    ing = list(
        session.scalars(
            select(RecordLineage)
            .where(RecordLineage.entity_type == kind, RecordLineage.entity_id == entity_id)
            .order_by(RecordLineage.id.desc())
            .limit(LIMIT)
        )
    )
    out["ingestion"] = [
        {
            "batch_id": x.batch_id,
            "source_id": x.source_id,
            "external_id": x.external_id,
            "row_index": x.row_index,
            "source_line": x.source_line,
            "mapping_profile_ref": x.mapping_profile_ref,
            "normalization_version": x.normalization_version,
            "record_fingerprint": x.record_fingerprint,
            "outcome": x.outcome.value,
            "changed_fields": x.changed_fields,
            "ingested_at": x.ingested_at,
        }
        for x in ing
    ]
    if ing:
        layers.append("ingestion_lineage")
    aud = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.target_type == audit_type, AuditEvent.target_id == audit_id)
            .order_by(AuditEvent.id.desc())
            .limit(LIMIT)
        )
    )
    out["audit"] = [
        {
            "id": a.id,
            "occurred_at": a.occurred_at,
            "actor": a.actor,
            "action": a.action,
            "outcome": a.outcome.value,
            "details": a.details,
        }
        for a in aud
    ]
    if aud:
        layers.append("audit_trail")
    if edge_kind:
        evs = list(
            session.scalars(
                select(GraphEdgeEvent)
                .where(GraphEdgeEvent.edge_kind == edge_kind, GraphEdgeEvent.edge_id == entity_id)
                .order_by(GraphEdgeEvent.id.desc())
                .limit(LIMIT)
            )
        )
        out["edge_events"] = [
            {
                "occurred_at": v.occurred_at,
                "action": v.action,
                "from_status": v.from_status,
                "to_status": v.to_status,
                "actor": v.actor,
                "reason": v.reason,
            }
            for v in evs
        ]
        if evs:
            layers.append("relationship_events")
    out["layers_present"] = layers
    out["has_source_record"] = bool(ing)
    out["truncated_at"] = LIMIT
    return out
