"""Read side: unified relationship records and batched, bounded lookups (no per-node queries)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.errors import DomainValidationError
from app.graph.rules import LIMITS, RULES, EdgeKind, GraphRelType
from app.models.enums import EdgeStatus, RegistryType, RelationshipType
from app.models.facility import Facility
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.registry.entities import CODE_ATTR

NodeKey = tuple[RegistryType, int]
S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT


@dataclass(frozen=True)
class EdgeRec:
    ref: str
    kind: EdgeKind
    row_id: int
    type: GraphRelType
    source: NodeKey
    destination: NodeKey
    status: EdgeStatus
    code: str
    provenance: str
    source_namespace: str | None = None
    status_reason: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    capacity_value: float | None = None
    capacity_unit: str | None = None
    lead_time_days: float | None = None
    supply_share_pct: float | None = None
    criticality: str | None = None
    confidence: float | None = None
    version: int | None = None
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def other(self, key: NodeKey) -> NodeKey:
        return self.destination if key == self.source else self.source

    def effective_on(self, day: date) -> bool:
        return (self.effective_from is None or self.effective_from <= day) and (
            self.effective_to is None or self.effective_to >= day
        )


@dataclass
class NodeRec:
    type: RegistryType
    id: int
    code: str
    name: str
    lifecycle: str
    detail: str | None
    provenance: str
    merged_into_id: int | None = None


@dataclass(frozen=True)
class EdgeFilter:
    types: frozenset[GraphRelType] | None = None
    statuses: frozenset[EdgeStatus] = field(default_factory=lambda: frozenset({EdgeStatus.CONFIRMED}))
    node_types: frozenset[RegistryType] | None = None
    as_of: date | None = None  # None -> dates are not applied


def make_ref(kind: EdgeKind, row_id: int) -> str:
    return f"{kind.value}:{row_id}"


def parse_ref(ref: str) -> tuple[EdgeKind, int]:
    kind_text, _, id_text = ref.partition(":")
    try:
        kind = EdgeKind(kind_text)
        row_id = int(id_text)
    except ValueError as exc:
        raise DomainValidationError("Relationship reference must look like SUPPLY:12, PRODUCT:7 or OWNER:3.") from exc
    if row_id < 1 or row_id > 2**31 - 1:
        raise DomainValidationError("Relationship reference id is out of range.")
    return kind, row_id


# ----------------------------------------------------------------------------------------------- record builders


def supply_rec(r: SupplyRelationship) -> EdgeRec:
    if r.relationship_type is RelationshipType.SUPPLIES:
        typ, src = GraphRelType.SUPPLIES, (S, r.supplier_id)
    else:
        typ, src = GraphRelType.TRANSFERS_TO, (F, r.source_facility_id)
    return EdgeRec(
        ref=make_ref(EdgeKind.SUPPLY, r.id), kind=EdgeKind.SUPPLY, row_id=r.id, type=typ, source=src,
        destination=(F, r.target_facility_id), status=r.status, code=r.relationship_code,
        provenance=r.provenance.value, source_namespace=r.source_namespace, status_reason=r.status_reason,
        effective_from=r.effective_from, effective_to=r.effective_to, capacity_value=r.capacity_value,
        capacity_unit=r.capacity_unit, lead_time_days=r.lead_time_days, supply_share_pct=r.supply_share_pct,
        criticality=r.criticality.value if r.criticality else None, confidence=r.confidence, version=r.version,
        created_by=r.created_by, updated_by=r.updated_by, created_at=r.created_at, updated_at=r.updated_at,
    )  # fmt: skip


def link_rec(link: ProductLink) -> EdgeRec:
    typ = GraphRelType(link.link_type.value)
    src = (S, link.supplier_id) if typ is GraphRelType.SUPPLIES_PRODUCT else (F, link.facility_id)
    return EdgeRec(
        ref=make_ref(EdgeKind.PRODUCT, link.id), kind=EdgeKind.PRODUCT, row_id=link.id, type=typ, source=src,
        destination=(P, link.product_id), status=link.status, code=link.link_code, provenance=link.provenance.value,
        source_namespace=link.source_namespace, status_reason=link.status_reason, effective_from=link.effective_from,
        effective_to=link.effective_to, capacity_value=link.quantity_value, capacity_unit=link.quantity_unit,
        lead_time_days=link.lead_time_days, supply_share_pct=link.supply_share_pct,
        criticality=link.criticality.value if link.criticality else None, confidence=link.confidence,
        version=link.version, created_by=link.created_by, updated_by=link.updated_by, created_at=link.created_at,
        updated_at=link.updated_at,
    )  # fmt: skip


def owner_rec(f: Facility) -> EdgeRec:
    return EdgeRec(
        ref=make_ref(EdgeKind.OWNER, f.id), kind=EdgeKind.OWNER, row_id=f.id, type=GraphRelType.OWNS,
        source=(S, f.owner_supplier_id), destination=(F, f.id), status=EdgeStatus.CONFIRMED,
        code=f"OWNS-{f.facility_code}", provenance=f.provenance.value, source_namespace=f.source_namespace,
        status_reason="Derived from the facility's registered owner.", updated_at=f.updated_at, created_at=f.created_at,
    )  # fmt: skip


def get_rec(session: Session, kind: EdgeKind, row_id: int) -> EdgeRec | None:
    if kind is EdgeKind.SUPPLY:
        row = session.get(SupplyRelationship, row_id)
        return supply_rec(row) if row else None
    if kind is EdgeKind.PRODUCT:
        link = session.get(ProductLink, row_id)
        return link_rec(link) if link else None
    fac = session.get(Facility, row_id)
    return owner_rec(fac) if fac is not None and fac.owner_supplier_id is not None else None


# --------------------------------------------------------------------------------------------- bounded fetching


def _chunks(items: list[NodeKey], size: int) -> Iterator[list[NodeKey]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _types_for(flt: EdgeFilter, kind: EdgeKind) -> list[GraphRelType]:
    return [t for t, rule in RULES.items() if rule.kind is kind and (flt.types is None or t in flt.types)]


def _date_clause(model: Any, day: date):
    return and_(
        or_(model.effective_from.is_(None), model.effective_from <= day),
        or_(model.effective_to.is_(None), model.effective_to >= day),
    )


def fetch_incident(
    session: Session, keys: Iterable[NodeKey], direction: str, flt: EdgeFilter, limit: int
) -> tuple[list[EdgeRec], bool]:
    """Edges touching ``keys``. ``direction``: ``out`` (key is the source), ``in`` (key is the destination) or ``both``.

    Returns at most ``limit`` records (ordered by reference, so results are deterministic) and whether more matched.
    """
    ordered = sorted(set(keys), key=lambda k: (k[0].value, k[1]))
    found: dict[str, EdgeRec] = {}
    want_out, want_in = direction in ("out", "both"), direction in ("in", "both")
    supply_types = [t for t in _types_for(flt, EdgeKind.SUPPLY)]
    product_types = [t for t in _types_for(flt, EdgeKind.PRODUCT)]
    owner_ok = bool(_types_for(flt, EdgeKind.OWNER)) and EdgeStatus.CONFIRMED in flt.statuses
    statuses = list(flt.statuses)
    for chunk in _chunks(ordered, LIMITS.in_chunk):
        sup = [i for t, i in chunk if t is S]
        fac = [i for t, i in chunk if t is F]
        prod = [i for t, i in chunk if t is P]
        # supply_relationships
        conds = []
        if want_out and sup:
            conds.append(
                and_(SupplyRelationship.relationship_type == RelationshipType.SUPPLIES,
                     SupplyRelationship.supplier_id.in_(sup))
            )  # fmt: skip
        if want_out and fac:
            conds.append(
                and_(SupplyRelationship.relationship_type == RelationshipType.TRANSFERS_TO,
                     SupplyRelationship.source_facility_id.in_(fac))
            )  # fmt: skip
        if want_in and fac:
            conds.append(SupplyRelationship.target_facility_id.in_(fac))
        if conds and supply_types:
            q = select(SupplyRelationship).where(
                or_(*conds),
                SupplyRelationship.status.in_(statuses),
                SupplyRelationship.relationship_type.in_([RelationshipType(t.value) for t in supply_types]),
            )
            if flt.as_of:
                q = q.where(_date_clause(SupplyRelationship, flt.as_of))
            for r in session.scalars(q.order_by(SupplyRelationship.id).limit(limit + 1)).unique():
                rec = supply_rec(r)
                found[rec.ref] = rec
        # product_links
        conds = []
        if want_out and sup:
            conds.append(ProductLink.supplier_id.in_(sup))
        if want_out and fac:
            conds.append(ProductLink.facility_id.in_(fac))
        if want_in and prod:
            conds.append(ProductLink.product_id.in_(prod))
        if conds and product_types:
            q = select(ProductLink).where(
                or_(*conds),
                ProductLink.status.in_(statuses),
                ProductLink.link_type.in_([_link_type(t) for t in product_types]),
            )
            if flt.as_of:
                q = q.where(_date_clause(ProductLink, flt.as_of))
            for link in session.scalars(q.order_by(ProductLink.id).limit(limit + 1)).unique():
                rec = link_rec(link)
                found[rec.ref] = rec
        # derived ownership
        if owner_ok:
            conds = []
            if want_out and sup:
                conds.append(Facility.owner_supplier_id.in_(sup))
            if want_in and fac:
                conds.append(and_(Facility.id.in_(fac), Facility.owner_supplier_id.is_not(None)))
            if conds:
                q = select(Facility).where(or_(*conds)).order_by(Facility.id).limit(limit + 1)
                for f in session.scalars(q).unique():
                    rec = owner_rec(f)
                    found[rec.ref] = rec
    records = sorted(found.values(), key=lambda e: (e.kind.value, e.row_id))
    if flt.node_types is not None:
        records = [e for e in records if e.source[0] in flt.node_types or e.destination[0] in flt.node_types]
    return records[:limit], len(records) > limit


def _link_type(t: GraphRelType):
    from app.models.enums import ProductLinkType

    return ProductLinkType(t.value)


# --------------------------------------------------------------------------------------------------- node lookup

_MODEL = {S: Supplier, F: Facility, P: Product}


def load_nodes(session: Session, keys: Iterable[NodeKey]) -> dict[NodeKey, NodeRec]:
    out: dict[NodeKey, NodeRec] = {}
    wanted = sorted(set(keys), key=lambda k: (k[0].value, k[1]))
    for rtype in (S, F, P):
        ids = [i for t, i in wanted if t is rtype]
        model = _MODEL[rtype]
        for start in range(0, len(ids), LIMITS.in_chunk):
            rows = session.scalars(select(model).where(model.id.in_(ids[start : start + LIMITS.in_chunk]))).unique()
            for e in rows:
                lifecycle = (
                    "MERGED"
                    if e.merged_into_id is not None
                    else "RETIRED"
                    if getattr(e, "retired_at", None) is not None
                    else e.status.value
                )
                out[(rtype, e.id)] = NodeRec(
                    type=rtype, id=e.id, code=getattr(e, CODE_ATTR[rtype]), name=e.name, lifecycle=lifecycle,
                    detail=e.category if rtype is P else e.country_or_region, provenance=e.provenance.value,
                    merged_into_id=e.merged_into_id,
                )  # fmt: skip
    return out


def list_edges(
    session: Session, flt: EdgeFilter, node: NodeKey | None, limit: int, offset: int
) -> tuple[list[EdgeRec], int]:
    """Paginated relationships ordered SUPPLY, PRODUCT, OWNER (then id), optionally incident to one node."""
    from sqlalchemy import func

    statuses = list(flt.statuses)
    sup_q = select(SupplyRelationship).where(
        SupplyRelationship.status.in_(statuses),
        SupplyRelationship.relationship_type.in_([RelationshipType(t.value) for t in _types_for(flt, EdgeKind.SUPPLY)]),
    )
    link_q = select(ProductLink).where(
        ProductLink.status.in_(statuses),
        ProductLink.link_type.in_([_link_type(t) for t in _types_for(flt, EdgeKind.PRODUCT)]),
    )
    own_q = select(Facility).where(Facility.owner_supplier_id.is_not(None))
    if flt.as_of:
        sup_q = sup_q.where(_date_clause(SupplyRelationship, flt.as_of))
        link_q = link_q.where(_date_clause(ProductLink, flt.as_of))
    if node is not None:
        ntype, nid = node
        if ntype is S:
            sup_q = sup_q.where(SupplyRelationship.supplier_id == nid)
            link_q = link_q.where(ProductLink.supplier_id == nid)
            own_q = own_q.where(Facility.owner_supplier_id == nid)
        elif ntype is F:
            sup_q = sup_q.where(
                or_(SupplyRelationship.source_facility_id == nid, SupplyRelationship.target_facility_id == nid)
            )
            link_q = link_q.where(ProductLink.facility_id == nid)
            own_q = own_q.where(Facility.id == nid)
        else:
            sup_q = sup_q.where(False)
            link_q = link_q.where(ProductLink.product_id == nid)
            own_q = own_q.where(False)
    owner_enabled = bool(_types_for(flt, EdgeKind.OWNER)) and EdgeStatus.CONFIRMED in flt.statuses
    parts = [
        (sup_q.order_by(SupplyRelationship.id), supply_rec, bool(_types_for(flt, EdgeKind.SUPPLY))),
        (link_q.order_by(ProductLink.id), link_rec, bool(_types_for(flt, EdgeKind.PRODUCT))),
        (own_q.order_by(Facility.id), owner_rec, owner_enabled),
    ]
    total = 0
    items: list[EdgeRec] = []
    skip = offset
    for query, builder, enabled in parts:
        if not enabled:
            continue
        n = session.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
        total += n
        if skip >= n:
            skip -= n
            continue
        need = limit - len(items)
        if need > 0:
            items.extend(builder(r) for r in session.scalars(query.offset(skip).limit(need)).unique())
        skip = 0
    return items, total
