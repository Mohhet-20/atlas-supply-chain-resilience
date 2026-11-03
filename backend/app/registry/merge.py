"""Safe, explicit, reversible-by-compensation merging of confirmed duplicate records.

Never triggered by a score. Execution requires a CONFIRMED_DUPLICATE review of the same pair, a permission-checked
actor, matching record versions (optimistic concurrency) and explicit decisions for every conflict. Everything
happens in the caller's single transaction; any failure rolls it back and leaves all records untouched.
The merged record becomes a tombstone (``merged_into_id``) - nothing is hard-deleted except duplicate relationship
rows that an operator explicitly chose to drop, whose full snapshot is kept in the immutable merge record.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import Enum as SAEnum
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.auth import Actor
from app.core.errors import (
    CompensationUnsafeError,
    ConflictError,
    DomainValidationError,
    MergeBlockedError,
    NotFoundError,
    VersionConflictError,
)
from app.graph import service as graph_service
from app.graph.rules import EdgeKind
from app.models.enums import CandidateStatus, EntityStatus, MergeStatus, RegistryType
from app.models.facility import Facility
from app.models.observation import Observation
from app.models.product import Product, ProductLink
from app.models.registry import CandidateReview, DuplicateCandidate, EntityAlias, EntityIdentifier, MergeRecord
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.models.types import utcnow
from app.registry import audit, service
from app.registry.entities import MODELS, Entity, code_of, get_entity, json_safe, snapshot
from app.registry.normalize import normalize_name
from app.schemas.registry import FieldDecision, MergeRequest

_GRAPH_KIND = {"supply_relationships": EdgeKind.SUPPLY, "product_links": EdgeKind.PRODUCT}
# Test hook: called after relationship reassignment, before the merge record is written (to prove rollback).
_FAILPOINT: Callable[[], None] | None = None

SCALAR_FIELDS: dict[RegistryType, list[str]] = {
    RegistryType.SUPPLIER: [
        "name", "legal_name", "country_or_region", "category", "status", "address", "parent_supplier_id",
    ],
    RegistryType.FACILITY: [
        "name", "facility_type", "country_or_region", "city", "address", "status", "owner_supplier_id",
    ],
    RegistryType.PRODUCT: [
        "name", "category", "unit_of_measure", "description", "manufacturer_supplier_id", "status",
    ],
}  # fmt: skip
GROUPS: dict[RegistryType, dict[str, list[str]]] = {
    RegistryType.SUPPLIER: {},
    RegistryType.FACILITY: {
        "location": ["latitude", "longitude", "location_status"],
        "capacity": ["capacity_value", "capacity_unit"],
    },
    RegistryType.PRODUCT: {},
}
TABLES = {
    "supply_relationships": SupplyRelationship,
    "product_links": ProductLink,
    "facilities": Facility,
    "suppliers": Supplier,
    "products": Product,
    "observations": Observation,
    "entity_identifiers": EntityIdentifier,
    "entity_aliases": EntityAlias,
}
DROP = "DROP_MERGED_DUPLICATE"
AUTHORITATIVE_DEFAULT = {"LEI", "DUNS", "VAT", "GTIN", "TAX_ID"}


def _val(e: Entity, name: str) -> Any:
    return json_safe(getattr(e, name))


def _empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


# ------------------------------------------------------------------- planning


def _field_plan(
    rtype: RegistryType, survivor: Entity, merged: Entity, decisions: dict[str, FieldDecision]
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    """Attribute-by-attribute differences, the decision applied to each, and the resulting values."""
    rows: list[dict[str, Any]] = []
    resulting: dict[str, Any] = {}
    blockers: list[dict[str, str]] = []
    units: list[tuple[str, list[str]]] = [(f, [f]) for f in SCALAR_FIELDS[rtype]]
    units += [(g, cols) for g, cols in GROUPS[rtype].items()]
    for key, cols in units:
        sv = {c: _val(survivor, c) for c in cols}
        mv = {c: _val(merged, c) for c in cols}
        s_empty = all(_empty(v) for v in sv.values())
        m_empty = all(_empty(v) for v in mv.values())
        if sv == mv:
            status, default = "SAME", "KEEP_SURVIVOR"
        elif s_empty and not m_empty:
            status, default = "FILL_FROM_MERGED", "USE_MERGED"
        elif m_empty:
            status, default = "MERGED_EMPTY", "KEEP_SURVIVOR"
        else:
            status, default = "CONFLICT", None
        dec = decisions.get(key)
        choice, custom = (dec.choice, dec.value) if dec else (default, None)
        chosen: dict[str, Any] | None
        if choice == "KEEP_SURVIVOR":
            chosen = sv
        elif choice == "USE_MERGED":
            chosen = mv
        elif choice == "CUSTOM":
            if len(cols) > 1:
                blockers.append(
                    {"code": "custom_not_supported", "message": f"'{key}' is a grouped field; choose a side."}
                )
                chosen = None
            else:
                chosen = {cols[0]: custom}
        else:
            chosen = None
            blockers.append({"code": "unresolved_field_conflict", "message": f"Choose a value for '{key}'."})
        if chosen is not None:
            resulting.update(chosen)
        rows.append(
            {
                "field": key,
                "columns": cols,
                "survivor": sv if len(cols) > 1 else sv[cols[0]],
                "merged": mv if len(cols) > 1 else mv[cols[0]],
                "status": status,
                "default_choice": default,
                "choice": choice,
                "resolved_value": chosen if len(cols) > 1 or chosen is None else chosen[cols[0]],
                "needs_decision": status == "CONFLICT" and dec is None,
            }
        )
    return rows, resulting, blockers


def _rel_key(r: SupplyRelationship, supplier_id=None, target=None, source=None) -> tuple:
    return (
        r.relationship_type.value,
        r.supplier_id if supplier_id is None else supplier_id,
        r.source_facility_id if source is None else source,
        r.target_facility_id if target is None else target,
    )


def _impact(session: Session, rtype: RegistryType, survivor: Entity, merged: Entity) -> list[dict[str, Any]]:
    """Every row that references the merged record, with the action the merge would take."""
    s, m = survivor.id, merged.id
    out: list[dict[str, Any]] = []

    def add(table: str, row, column: str, action: str, code: str, note: str = "") -> None:
        out.append(
            {
                "table": table,
                "row_id": row.id,
                "code": code,
                "column": column,
                "old": m,
                "new": s,
                "action": action,
                "note": note,
            }
        )

    if rtype is RegistryType.SUPPLIER:
        existing_rel = {
            _rel_key(r) for r in session.scalars(select(SupplyRelationship).where(SupplyRelationship.supplier_id == s))
        }
        for r in session.scalars(
            select(SupplyRelationship).where(SupplyRelationship.supplier_id == m).order_by(SupplyRelationship.id)
        ):
            dup = _rel_key(r, supplier_id=s) in existing_rel
            add(
                "supply_relationships",
                r,
                "supplier_id",
                "CONFLICT_DUPLICATE" if dup else "REASSIGN",
                r.relationship_code,
                "survivor already supplies the same facility" if dup else "",
            )
        existing_links = {
            (link.link_type.value, link.product_id)
            for link in session.scalars(select(ProductLink).where(ProductLink.supplier_id == s))
        }
        for link in session.scalars(select(ProductLink).where(ProductLink.supplier_id == m).order_by(ProductLink.id)):
            dup = (link.link_type.value, link.product_id) in existing_links
            add(
                "product_links",
                link,
                "supplier_id",
                "CONFLICT_DUPLICATE" if dup else "REASSIGN",
                link.link_code,
                "survivor already supplies the same product" if dup else "",
            )
        for f in session.scalars(select(Facility).where(Facility.owner_supplier_id == m).order_by(Facility.id)):
            add("facilities", f, "owner_supplier_id", "REASSIGN", f.facility_code)
        for c in session.scalars(select(Supplier).where(Supplier.parent_supplier_id == m).order_by(Supplier.id)):
            add("suppliers", c, "parent_supplier_id", "REASSIGN", c.supplier_code)
        for p in session.scalars(select(Product).where(Product.manufacturer_supplier_id == m).order_by(Product.id)):
            add("products", p, "manufacturer_supplier_id", "REASSIGN", p.product_code)
        for o in session.scalars(select(Observation).where(Observation.supplier_id == m).order_by(Observation.id)):
            add("observations", o, "supplier_id", "REASSIGN", o.observation_code)
    elif rtype is RegistryType.FACILITY:
        existing_rel = {
            _rel_key(r)
            for r in session.scalars(
                select(SupplyRelationship).where(
                    (SupplyRelationship.target_facility_id == s) | (SupplyRelationship.source_facility_id == s)
                )
            )
        }
        for r in session.scalars(
            select(SupplyRelationship).where(SupplyRelationship.target_facility_id == m).order_by(SupplyRelationship.id)
        ):
            self_loop = r.source_facility_id == s
            dup = _rel_key(r, target=s) in existing_rel
            action = "CONFLICT_SELF_LOOP" if self_loop else ("CONFLICT_DUPLICATE" if dup else "REASSIGN")
            add(
                "supply_relationships",
                r,
                "target_facility_id",
                action,
                r.relationship_code,
                "would connect the facility to itself"
                if self_loop
                else ("an equivalent relationship already exists" if dup else ""),
            )
        for r in session.scalars(
            select(SupplyRelationship).where(SupplyRelationship.source_facility_id == m).order_by(SupplyRelationship.id)
        ):
            self_loop = r.target_facility_id == s
            dup = _rel_key(r, source=s) in existing_rel
            action = "CONFLICT_SELF_LOOP" if self_loop else ("CONFLICT_DUPLICATE" if dup else "REASSIGN")
            add(
                "supply_relationships",
                r,
                "source_facility_id",
                action,
                r.relationship_code,
                "would connect the facility to itself"
                if self_loop
                else ("an equivalent relationship already exists" if dup else ""),
            )
        existing_links = {
            (link.link_type.value, link.product_id)
            for link in session.scalars(select(ProductLink).where(ProductLink.facility_id == s))
        }
        for link in session.scalars(select(ProductLink).where(ProductLink.facility_id == m).order_by(ProductLink.id)):
            dup = (link.link_type.value, link.product_id) in existing_links
            add(
                "product_links",
                link,
                "facility_id",
                "CONFLICT_DUPLICATE" if dup else "REASSIGN",
                link.link_code,
                "survivor already has the same product link" if dup else "",
            )
        for o in session.scalars(select(Observation).where(Observation.facility_id == m).order_by(Observation.id)):
            add("observations", o, "facility_id", "REASSIGN", o.observation_code)
    else:
        existing = {
            (link.link_type.value, link.supplier_id, link.facility_id)
            for link in session.scalars(select(ProductLink).where(ProductLink.product_id == s))
        }
        for link in session.scalars(select(ProductLink).where(ProductLink.product_id == m).order_by(ProductLink.id)):
            dup = (link.link_type.value, link.supplier_id, link.facility_id) in existing
            add(
                "product_links",
                link,
                "product_id",
                "CONFLICT_DUPLICATE" if dup else "REASSIGN",
                link.link_code,
                "survivor already has the same link to this party" if dup else "",
            )
    return out  # fmt: skip


def _identifier_plan(session: Session, rtype: RegistryType, survivor: Entity, merged: Entity) -> dict[str, Any]:
    keep = service.list_identifiers(session, rtype, merged)
    s_ids = service.list_identifiers(session, rtype, survivor)
    conflicts = []
    by_scheme: dict[str, set[str]] = {}
    for i in s_ids:
        if i["authoritative"] or i["scheme"] in AUTHORITATIVE_DEFAULT:
            by_scheme.setdefault(i["scheme"], set()).add(i["value"])
    for i in keep:
        if (i["authoritative"] or i["scheme"] in AUTHORITATIVE_DEFAULT) and i["scheme"] in by_scheme:
            if i["value"] not in by_scheme[i["scheme"]]:
                conflicts.append(
                    {"scheme": i["scheme"], "survivor": sorted(by_scheme[i["scheme"]]), "merged": i["value"]}
                )
    return {"preserved": keep, "conflicts": conflicts}


def build_preview(
    session: Session,
    rtype: RegistryType,
    survivor_id: int,
    merged_id: int,
    *,
    field_decisions: dict[str, FieldDecision] | None = None,
    relationship_decisions: dict[str, str] | None = None,
    acknowledge_identifier_conflict: bool = False,
    candidate_id: int | None = None,
) -> dict[str, Any]:
    """Side-effect-free description of what a merge would do and what still blocks it."""
    if survivor_id == merged_id:
        raise DomainValidationError("The surviving and merged records must be different.")
    survivor = get_entity(session, rtype, survivor_id)
    merged = get_entity(session, rtype, merged_id)
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for e, label in ((survivor, "surviving"), (merged, "merged")):
        if e.merged_into_id is not None:
            blockers.append({"code": "already_merged", "message": f"The {label} record was already merged."})
    rows, resulting, field_blockers = _field_plan(rtype, survivor, merged, field_decisions or {})
    blockers += field_blockers
    impact = _impact(session, rtype, survivor, merged)
    decisions = relationship_decisions or {}
    for item in impact:
        if item["action"].startswith("CONFLICT"):
            item["decision"] = decisions.get(item["code"])
            if item["decision"] != DROP:
                blockers.append(
                    {
                        "code": "unresolved_relationship_conflict",
                        "message": (
                            f"{item['code']}: {item['note']}. Choose {DROP} to remove it (a full snapshot is kept)."
                        ),
                    }
                )
    ids = _identifier_plan(session, rtype, survivor, merged)
    if ids["conflicts"] and not acknowledge_identifier_conflict:
        blockers.append(
            {
                "code": "identifier_conflict",
                "message": (
                    "The records carry different authoritative identifiers. "
                    "Acknowledge the conflict explicitly to merge."
                ),
            }
        )
    if rtype is RegistryType.SUPPLIER and (
        survivor.parent_supplier_id == merged.id or merged.parent_supplier_id == survivor.id
    ):
        blockers.append(
            {
                "code": "hierarchy_relation",
                "message": "The records are parent and child; they are unlikely to be duplicates.",
            }
        )
    review = None
    if candidate_id is not None:
        cand = session.get(DuplicateCandidate, candidate_id)
        ok = (
            cand is not None and cand.entity_type == rtype and {cand.left_id, cand.right_id} == {survivor_id, merged_id}
        )
        review = {"candidate_id": candidate_id, "status": cand.status.value if cand else None, "matches_pair": ok}
        if not ok:
            blockers.append(
                {"code": "candidate_mismatch", "message": "The review candidate does not describe this pair."}
            )
        elif cand.status is not CandidateStatus.CONFIRMED_DUPLICATE:
            blockers.append({"code": "review_not_confirmed", "message": "A reviewer must confirm the duplicate first."})
    else:
        blockers.append(
            {"code": "review_required", "message": "A confirmed duplicate review (candidate_id) is required."}
        )
    if merged.provenance != survivor.provenance:
        warnings.append(
            {
                "code": "mixed_provenance",
                "message": (
                    f"Provenance differs: {survivor.provenance.value} vs {merged.provenance.value}; "
                    "both are preserved in the merge history."
                ),
            }
        )
    return {
        "entity_type": rtype.value,
        "survivor": {"id": survivor.id, "code": code_of(survivor), "name": survivor.name, "version": survivor.version},
        "merged": {"id": merged.id, "code": code_of(merged), "name": merged.name, "version": merged.version},
        "fields": rows,
        "resulting_values": resulting,
        "relationship_impact": impact,
        "identifiers": ids,
        "aliases_to_add": _alias_names(survivor, merged),
        "review": review,
        "blockers": blockers,
        "warnings": warnings,
        "can_execute": not blockers,
    }  # fmt: skip


def _alias_names(survivor: Entity, merged: Entity) -> list[str]:
    names = []
    for value in (merged.name, getattr(merged, "legal_name", None)):
        if value and normalize_name(value) and normalize_name(value) != normalize_name(survivor.name):
            names.append(value)
    return list(dict.fromkeys(names))


# ------------------------------------------------------------------ execution


def execute_merge(
    session: Session, rtype: RegistryType, req: MergeRequest, actor: Actor, request_id: str | None = None
) -> MergeRecord:
    survivor = get_entity(session, rtype, req.survivor_id)
    merged = get_entity(session, rtype, req.merged_id)
    if survivor.version != req.survivor_version or merged.version != req.merged_version:
        raise VersionConflictError(
            "One of the records changed since the merge preview "
            f"(survivor v{survivor.version}, merged v{merged.version}). Preview again."
        )
    preview = build_preview(
        session,
        rtype,
        req.survivor_id,
        req.merged_id,
        field_decisions=req.field_decisions,
        relationship_decisions=req.relationship_decisions,
        acknowledge_identifier_conflict=req.acknowledge_identifier_conflict,
        candidate_id=req.candidate_id,
    )
    if preview["blockers"]:
        raise MergeBlockedError("Merge blocked: " + "; ".join(b["code"] for b in preview["blockers"]))
    if not req.reason:
        raise DomainValidationError("A reason is required to execute a merge.")
    cand = session.get(DuplicateCandidate, req.candidate_id)

    survivor_before, merged_before = snapshot(survivor), snapshot(merged)
    # 1. field decisions
    for column, value in preview["resulting_values"].items():
        _set_column(rtype, survivor, column, value)
    _validate_final(session, rtype, survivor)

    # 2. relationships
    reassignments: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in preview["relationship_impact"]:
        model = TABLES[item["table"]]
        row = session.get(model, item["row_id"])
        edge_kind = _GRAPH_KIND.get(item["table"])
        if item["action"] == "REASSIGN":
            setattr(row, item["column"], survivor.id)
            reassignments.append({k: item[k] for k in ("table", "row_id", "column", "old", "new", "code")})
            if edge_kind is not None:
                row.updated_by = actor.name
                graph_service.record_event(
                    session, edge_kind, row.id, "REPOINTED", actor.name, reason=req.reason,
                    changes={item["column"]: [item["old"], item["new"]], "merge_survivor": survivor.id},
                    request_id=request_id,
                )  # fmt: skip
        else:  # explicit DROP_MERGED_DUPLICATE decision (verified by build_preview)
            removed.append({"table": item["table"], "row": snapshot(row), "reason": item["note"], "code": item["code"]})
            if edge_kind is not None:
                graph_service.record_event(
                    session, edge_kind, row.id, "DUPLICATE_REMOVED", actor.name, reason=req.reason,
                    from_status=row.status.value, changes={"merge_survivor": survivor.id, "note": item["note"]},
                    request_id=request_id,
                )  # fmt: skip
            session.delete(row)
    session.flush()
    if _FAILPOINT is not None:
        _FAILPOINT()

    # 3. identity lineage: identifiers and aliases
    created_aliases: list[EntityAlias] = []
    moved_identifiers: list[EntityIdentifier] = []
    if merged.source_id is not None and merged.external_id is not None:
        primary = EntityIdentifier(
            entity_type=rtype,
            entity_id=survivor.id,
            source_id=merged.source_id,
            scheme="EXTERNAL_ID",
            value=merged.external_id,
            authoritative=False,
            created_by=actor.name,
        )
        session.add(primary)
        created_aliases_ids = [primary]
    else:
        created_aliases_ids = []
    for ident in session.scalars(
        select(EntityIdentifier).where(EntityIdentifier.entity_type == rtype, EntityIdentifier.entity_id == merged.id)
    ):
        ident.entity_id = survivor.id
        moved_identifiers.append(ident)
        reassignments.append(
            {
                "table": "entity_identifiers",
                "row_id": ident.id,
                "column": "entity_id",
                "old": merged.id,
                "new": survivor.id,
                "code": ident.scheme,
            }
        )
    for name in preview["aliases_to_add"]:
        alias = EntityAlias(
            entity_type=rtype,
            entity_id=survivor.id,
            alias=name,
            alias_normalized=normalize_name(name)[:255],
            kind="MERGED_NAME",
            created_by=actor.name,
        )
        exists = session.scalar(
            select(EntityAlias.id).where(
                EntityAlias.entity_type == rtype,
                EntityAlias.entity_id == survivor.id,
                EntityAlias.alias_normalized == alias.alias_normalized,
                EntityAlias.kind == alias.kind,
            )
        )
        if not exists:
            session.add(alias)
            created_aliases.append(alias)
    session.flush()

    # 4. tombstone the merged record. The survivor is always touched so its version changes with every merge: an editor
    # holding the pre-merge version can no longer save over a record whose identity just absorbed another one.
    survivor.updated_at = utcnow()
    merged.merged_into_id = survivor.id
    merged.merged_at = utcnow()
    merged.status = EntityStatus.INACTIVE
    session.flush()

    # 5. immutable merge history
    merge = MergeRecord(
        merge_uid=f"MRG-{uuid.uuid4().hex[:16].upper()}",
        entity_type=rtype,
        survivor_id=survivor.id,
        merged_id=merged.id,
        status=MergeStatus.EXECUTED,
        actor=actor.name,
        authenticated=actor.authenticated,
        reason=req.reason,
        candidate_id=req.candidate_id,
        survivor_version_before=survivor_before["version"],
        survivor_version_after=survivor.version,
        merged_version_after=merged.version,
        survivor_before=survivor_before,
        merged_before=merged_before,
        field_decisions={
            "rows": [
                {k: r[k] for k in ("field", "status", "choice", "survivor", "merged", "resolved_value")}
                for r in preview["fields"]
            ],
            "merged_provenance": merged.provenance.value,
            "merged_source_namespace": merged.source_namespace,
        },
        reassignments=reassignments,
        removed_duplicates=removed,
    )
    session.add(merge)
    session.flush()
    for obj in (*created_aliases, *moved_identifiers, *created_aliases_ids):
        obj.origin_merge_id = merge.id
    session.flush()
    reassignments.extend({"table": "entity_aliases", "row_id": a.id, "created": True} for a in created_aliases)
    reassignments.extend({"table": "entity_identifiers", "row_id": i.id, "created": True} for i in created_aliases_ids)
    merge.reassignments = list(reassignments)

    # 6. candidate + audit
    cand.status = CandidateStatus.MERGED
    session.add(
        CandidateReview(
            candidate_id=cand.id,
            from_status=CandidateStatus.CONFIRMED_DUPLICATE,
            to_status=CandidateStatus.MERGED,
            actor=actor.name,
            authenticated=actor.authenticated,
            reason=req.reason,
            evidence_snapshot={"merge_uid": merge.merge_uid, "score": cand.score, "tier": cand.tier.value},
        )
    )
    session.flush()
    audit.record(
        session,
        actor,
        "merge.executed",
        target_type=rtype.value,
        target_id=survivor.id,
        details={
            "merge_uid": merge.merge_uid,
            "survivor_id": survivor.id,
            "merged_id": merged.id,
            "reassigned_rows": len(reassignments),
            "dropped_duplicates": len(removed),
            "reason": req.reason,
        },
        request_id=request_id,
    )
    audit.record(
        session,
        actor,
        "merge.tombstoned",
        target_type=rtype.value,
        target_id=merged.id,
        details={"merge_uid": merge.merge_uid, "merged_into": survivor.id},
        request_id=request_id,
    )
    return merge


def _set_column(rtype: RegistryType, entity: Entity, column: str, value: Any) -> None:
    col = entity.__table__.columns[column]
    if value is not None and isinstance(col.type, SAEnum):
        value = col.type.enum_class(value)
    setattr(entity, column, value)


def _validate_final(session: Session, rtype: RegistryType, survivor: Entity) -> None:
    refs = {
        RegistryType.SUPPLIER: "parent_supplier_id",
        RegistryType.FACILITY: "owner_supplier_id",
        RegistryType.PRODUCT: "manufacturer_supplier_id",
    }[rtype]
    ref_id = getattr(survivor, refs)
    if rtype is RegistryType.SUPPLIER and ref_id == survivor.id:
        raise DomainValidationError("The merged parent_supplier_id would make the supplier its own parent.")
    service._validate_refs(session, rtype, {refs: ref_id}, own_id=survivor.id)
    if rtype is RegistryType.FACILITY:
        service._validate_facility_state(
            {
                c: getattr(survivor, c)
                for c in ("latitude", "longitude", "capacity_value", "capacity_unit", "location_status")
            }
        )


# --------------------------------------------------------------- compensation


def _restore_value(column, value: Any) -> Any:
    if value is None:
        return None
    t = column.type
    if isinstance(t, SAEnum):
        return t.enum_class(value)
    try:
        py = t.python_type
    except NotImplementedError:
        try:  # TypeDecorator columns (UTCDateTime) expose the Python type of the type they wrap
            py = t.impl.python_type
        except (NotImplementedError, AttributeError):
            return value
    if py is datetime and isinstance(value, str):
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if py is date and isinstance(value, str):
        return date.fromisoformat(value)
    return value


def compensate_merge(
    session: Session, merge_id: int, reason: str, actor: Actor, request_id: str | None = None
) -> MergeRecord:
    """Undo a merge when - and only when - nothing relevant changed afterwards. Otherwise refuse with reasons."""
    merge = session.get(MergeRecord, merge_id)
    if merge is None:
        raise NotFoundError("Merge not found.")
    if merge.status is not MergeStatus.EXECUTED:
        raise ConflictError("This merge was already compensated.")
    rtype = merge.entity_type
    model = MODELS[rtype]
    survivor, merged = session.get(model, merge.survivor_id), session.get(model, merge.merged_id)
    problems: list[str] = []
    if survivor is None or merged is None:
        raise CompensationUnsafeError("One of the records no longer exists.")
    if survivor.merged_into_id is not None:
        problems.append("the surviving record was itself merged into another record")
    if merged.merged_into_id != survivor.id:
        problems.append("the merged record no longer points at the survivor")
    if survivor.version != merge.survivor_version_after:
        problems.append(
            f"the surviving record changed after the merge (v{merge.survivor_version_after} -> v{survivor.version})"
        )
    for item in merge.reassignments:
        if item.get("created"):
            if session.get(TABLES[item["table"]], item["row_id"]) is None:
                problems.append(f"{item['table']} row {item['row_id']} created by the merge no longer exists")
            continue
        row = session.get(TABLES[item["table"]], item["row_id"])
        if row is None or getattr(row, item["column"]) != item["new"]:
            problems.append(f"{item['table']} row {item['row_id']} ({item.get('code')}) was changed after the merge")
    for dropped in merge.removed_duplicates:
        tbl = TABLES[dropped["table"]]
        if session.get(tbl, dropped["row"]["id"]) is not None:
            problems.append(f"{dropped['table']} row {dropped['row']['id']} cannot be restored (id in use)")
    if problems:
        raise CompensationUnsafeError("Compensation is not safe: " + "; ".join(problems[:8]))

    # revert
    for item in merge.reassignments:
        if item.get("created"):
            row = session.get(TABLES[item["table"]], item["row_id"])
            session.delete(row)
        else:
            row = session.get(TABLES[item["table"]], item["row_id"])
            setattr(row, item["column"], item["old"])
            if item["table"] == "entity_identifiers":
                row.origin_merge_id = None
            if item["table"] in _GRAPH_KIND:
                row.updated_by = actor.name
                graph_service.record_event(
                    session, _GRAPH_KIND[item["table"]], row.id, "REPOINTED_BACK", actor.name, reason=reason,
                    changes={item["column"]: [item["new"], item["old"]], "merge_uid": merge.merge_uid},
                    request_id=request_id,
                )  # fmt: skip
    session.flush()
    for dropped in merge.removed_duplicates:
        tbl = TABLES[dropped["table"]]
        values = {
            c.key: _restore_value(c, dropped["row"].get(c.key)) for c in tbl.__table__.columns if c.key != "version"
        }
        restored = tbl(**values)
        session.add(restored)
        if dropped["table"] in _GRAPH_KIND:
            session.flush()
            graph_service.record_event(
                session, _GRAPH_KIND[dropped["table"]], restored.id, "RESTORED", actor.name, reason=reason,
                changes={"merge_uid": merge.merge_uid}, request_id=request_id,
            )  # fmt: skip
    for column in survivor.__table__.columns:
        if column.key in {
            "id",
            "version",
            "created_at",
            "updated_at",
            "merged_into_id",
            "merged_at",
        } or column.key.endswith("_code"):
            continue
        if column.key in {"source_id", "external_id", "provenance", "name_normalized"}:
            continue
        setattr(survivor, column.key, _restore_value(column, merge.survivor_before.get(column.key)))
    merged.merged_into_id = None
    merged.merged_at = None
    merged.status = _restore_value(merged.__table__.columns["status"], merge.merged_before["status"])
    session.flush()
    merge.status = MergeStatus.COMPENSATED
    merge.compensated_at = utcnow()
    merge.compensated_by = actor.name
    merge.compensation_reason = reason
    if merge.candidate_id:
        cand = session.get(DuplicateCandidate, merge.candidate_id)
        if cand is not None:
            cand.status = CandidateStatus.REOPENED
            session.add(
                CandidateReview(
                    candidate_id=cand.id,
                    from_status=CandidateStatus.MERGED,
                    to_status=CandidateStatus.REOPENED,
                    actor=actor.name,
                    authenticated=actor.authenticated,
                    reason=f"Merge {merge.merge_uid} compensated: {reason}",
                    evidence_snapshot={"merge_uid": merge.merge_uid},
                )
            )
    session.flush()
    audit.record(
        session,
        actor,
        "merge.compensated",
        target_type=rtype.value,
        target_id=survivor.id,
        details={"merge_uid": merge.merge_uid, "reason": reason, "restored_record": merged.id},
        request_id=request_id,
    )
    return merge


def merge_out(merge: MergeRecord, *, detail: bool = False) -> dict[str, Any]:
    base = {
        "id": merge.id,
        "merge_uid": merge.merge_uid,
        "entity_type": merge.entity_type.value,
        "survivor_id": merge.survivor_id,
        "merged_id": merge.merged_id,
        "status": merge.status.value,
        "actor": merge.actor,
        "authenticated": merge.authenticated,
        "reason": merge.reason,
        "candidate_id": merge.candidate_id,
        "survivor_version_before": merge.survivor_version_before,
        "survivor_version_after": merge.survivor_version_after,
        "created_at": merge.created_at,
        "compensated_at": merge.compensated_at,
        "compensated_by": merge.compensated_by,
        "compensation_reason": merge.compensation_reason,
        "reassigned_count": sum(1 for r in merge.reassignments if not r.get("created")),
        "dropped_count": len(merge.removed_duplicates),
    }
    if detail:
        base.update(
            survivor_before=merge.survivor_before,
            merged_before=merge.merged_before,
            field_decisions=merge.field_decisions,
            reassignments=merge.reassignments,
            removed_duplicates=merge.removed_duplicates,
        )
    return base
