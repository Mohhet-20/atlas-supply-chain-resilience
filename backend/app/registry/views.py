"""Read models: entity detail, relationships, lineage and change history."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.enums import CandidateStatus, CandidateTier, EntityKind, RegistryType
from app.models.facility import Facility
from app.models.ingestion import RecordLineage
from app.models.observation import Observation
from app.models.product import Product, ProductLink
from app.models.registry import AuditEvent, DuplicateCandidate, MergeRecord, QualityAssessment
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.registry import service
from app.registry.entities import MODELS, Entity, canonical_of, code_of, json_safe, snapshot

KIND = {
    RegistryType.SUPPLIER: EntityKind.SUPPLIER,
    RegistryType.FACILITY: EntityKind.FACILITY,
    RegistryType.PRODUCT: EntityKind.PRODUCT,
}
LIMIT = 100


def _ref(rtype: str, obj) -> dict[str, Any] | None:
    if obj is None:
        return None
    return {"type": rtype, "id": obj.id, "code": code_of(obj), "name": obj.name}


def relationships_of(session: Session, rtype: RegistryType, entity_id: int) -> dict[str, list[dict[str, Any]]]:
    """Relationships and references that point at (or leave from) this record, bounded to LIMIT per kind."""
    out: dict[str, list[dict[str, Any]]] = {}
    if rtype is RegistryType.SUPPLIER:
        rels = session.scalars(
            select(SupplyRelationship).where(SupplyRelationship.supplier_id == entity_id).limit(LIMIT)
        ).all()
        out["supplies_facilities"] = [
            {"code": r.relationship_code, "target": _ref("FACILITY", r.target_facility), "id": r.id} for r in rels
        ]
        links = session.scalars(select(ProductLink).where(ProductLink.supplier_id == entity_id).limit(LIMIT)).all()
        out["product_links"] = [
            {"code": link.link_code, "product": _ref("PRODUCT", link.product), "id": link.id} for link in links
        ]
        out["owned_facilities"] = [
            _ref("FACILITY", f)
            for f in session.scalars(select(Facility).where(Facility.owner_supplier_id == entity_id).limit(LIMIT))
        ]
        out["child_suppliers"] = [
            _ref("SUPPLIER", s)
            for s in session.scalars(select(Supplier).where(Supplier.parent_supplier_id == entity_id).limit(LIMIT))
        ]
        out["manufactured_products"] = [
            _ref("PRODUCT", p)
            for p in session.scalars(select(Product).where(Product.manufacturer_supplier_id == entity_id).limit(LIMIT))
        ]
        out["observations"] = [
            {"code": o.observation_code, "metric": o.metric_name}
            for o in session.scalars(select(Observation).where(Observation.supplier_id == entity_id).limit(LIMIT))
        ]
    elif rtype is RegistryType.FACILITY:
        inbound = session.scalars(
            select(SupplyRelationship).where(SupplyRelationship.target_facility_id == entity_id).limit(LIMIT)
        ).all()
        out["inbound"] = [
            {
                "code": r.relationship_code,
                "id": r.id,
                "from": _ref("SUPPLIER", r.supplier) if r.supplier else _ref("FACILITY", r.source_facility),
            }
            for r in inbound
        ]
        outbound = session.scalars(
            select(SupplyRelationship).where(SupplyRelationship.source_facility_id == entity_id).limit(LIMIT)
        ).all()
        out["outbound"] = [
            {"code": r.relationship_code, "id": r.id, "to": _ref("FACILITY", r.target_facility)} for r in outbound
        ]
        links = session.scalars(select(ProductLink).where(ProductLink.facility_id == entity_id).limit(LIMIT)).all()
        out["product_links"] = [
            {"code": link.link_code, "product": _ref("PRODUCT", link.product), "id": link.id} for link in links
        ]
        owner = session.get(Facility, entity_id).owner
        out["owner"] = [_ref("SUPPLIER", owner)] if owner else []
        out["observations"] = [
            {"code": o.observation_code, "metric": o.metric_name}
            for o in session.scalars(select(Observation).where(Observation.facility_id == entity_id).limit(LIMIT))
        ]
    else:
        links = session.scalars(select(ProductLink).where(ProductLink.product_id == entity_id).limit(LIMIT)).all()
        out["product_links"] = [
            {
                "code": link.link_code,
                "id": link.id,
                "link_type": link.link_type.value,
                "party": _ref("SUPPLIER", link.supplier) if link.supplier else _ref("FACILITY", link.facility),
            }
            for link in links
        ]
        prod = session.get(Product, entity_id)
        mf = session.get(Supplier, prod.manufacturer_supplier_id) if prod and prod.manufacturer_supplier_id else None
        out["manufacturer"] = [_ref("SUPPLIER", mf)] if mf else []
    return out


def lineage_for(session: Session, rtype: RegistryType, entity_id: int) -> list[dict[str, Any]]:
    """Ingestion lineage of this record plus every record merged into it (identity lineage)."""
    ids = [entity_id] + [
        m
        for (m,) in session.execute(
            select(MergeRecord.merged_id).where(MergeRecord.entity_type == rtype, MergeRecord.survivor_id == entity_id)
        )
    ]
    rows = session.scalars(
        select(RecordLineage)
        .where(RecordLineage.entity_type == KIND[rtype], RecordLineage.entity_id.in_(ids))
        .order_by(RecordLineage.id.desc())
        .limit(LIMIT)
    ).all()
    return [
        {
            "entity_id": r.entity_id,
            "via_merge": r.entity_id != entity_id,
            "batch_id": r.batch_id,
            "source_id": r.source_id,
            "external_id": r.external_id,
            "row_index": r.row_index,
            "mapping_profile_ref": r.mapping_profile_ref,
            "normalization_version": r.normalization_version,
            "outcome": r.outcome.value,
            "changed_fields": r.changed_fields,
            "ingested_at": r.ingested_at,
        }
        for r in rows
    ]


def history_for(session: Session, rtype: RegistryType, entity_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.target_type == rtype.value, AuditEvent.target_id == str(entity_id))
        .order_by(AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": e.id,
            "occurred_at": e.occurred_at,
            "actor": e.actor,
            "authenticated": e.authenticated,
            "action": e.action,
            "outcome": e.outcome.value,
            "details": e.details,
        }
        for e in rows
    ]


def merge_lineage(session: Session, rtype: RegistryType, entity: Entity) -> dict[str, Any]:
    merged_from = session.scalars(
        select(MergeRecord)
        .where(MergeRecord.entity_type == rtype, MergeRecord.survivor_id == entity.id)
        .order_by(MergeRecord.id.desc())
    ).all()
    info: dict[str, Any] = {
        "merged_into": None,
        "merged_from": [
            {"merge_uid": m.merge_uid, "merged_id": m.merged_id, "status": m.status.value, "created_at": m.created_at}
            for m in merged_from
        ],
    }
    if entity.merged_into_id is not None:
        c = canonical_of(session, entity)
        info["merged_into"] = {"id": c.id, "code": code_of(c), "name": c.name}
    return info


def latest_quality(session: Session, rtype: RegistryType, entity_id: int) -> dict[str, Any] | None:
    qa = session.scalar(
        select(QualityAssessment)
        .where(QualityAssessment.entity_type == rtype, QualityAssessment.entity_id == entity_id)
        .order_by(QualityAssessment.id.desc())
        .limit(1)
    )
    if qa is None:
        return None
    current = session.get(MODELS[rtype], entity_id)
    return {
        "run_id": qa.run_id,
        "config_version": qa.config_version,
        "overall": qa.overall,
        "dimensions": qa.dimensions,
        "issues": qa.issues,
        "assessed_at": qa.assessed_at,
        "entity_version": qa.entity_version,
        "stale": current is not None and qa.entity_version != current.version,
    }


def open_candidates(session: Session, rtype: RegistryType, entity_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(DuplicateCandidate)
            .where(
                DuplicateCandidate.entity_type == rtype,
                or_(DuplicateCandidate.left_id == entity_id, DuplicateCandidate.right_id == entity_id),
                DuplicateCandidate.tier != CandidateTier.INSUFFICIENT,
                DuplicateCandidate.status.in_(
                    [
                        CandidateStatus.NEW,
                        CandidateStatus.UNDER_REVIEW,
                        CandidateStatus.REOPENED,
                        CandidateStatus.DEFERRED,
                    ]
                ),
            )
        )
        or 0
    )


def lifecycle_of(entity: Entity) -> str:
    if entity.merged_into_id is not None:
        return "MERGED"
    if getattr(entity, "retired_at", None) is not None:
        return "RETIRED"
    return str(getattr(entity.status, "value", entity.status))


def entity_detail(session: Session, rtype: RegistryType, entity: Entity) -> dict[str, Any]:
    snap = {k: v for k, v in snapshot(entity).items()}
    aliases = service.list_aliases(session, rtype, entity.id)
    return {
        "type": rtype.value,
        "id": entity.id,
        "code": code_of(entity),
        "lifecycle": lifecycle_of(entity),
        "version": entity.version,
        "name": entity.name,
        "attributes": snap,
        "normalized": {"name": entity.name_normalized},
        "source_namespace": entity.source_namespace,
        "aliases": [
            {
                "id": a.id,
                "alias": a.alias,
                "alias_normalized": a.alias_normalized,
                "kind": a.kind.value,
                "origin_merge_id": a.origin_merge_id,
                "created_by": a.created_by,
            }
            for a in aliases
        ],
        "identifiers": service.list_identifiers(session, rtype, entity),
        "relationships": relationships_of(session, rtype, entity.id),
        "lineage": lineage_for(session, rtype, entity.id),
        "identity": merge_lineage(session, rtype, entity),
        "quality": latest_quality(session, rtype, entity.id),
        "open_candidates": open_candidates(session, rtype, entity.id),
        "history": history_for(session, rtype, entity.id),
    }


__all__ = ["entity_detail", "relationships_of", "lineage_for", "history_for", "json_safe"]
