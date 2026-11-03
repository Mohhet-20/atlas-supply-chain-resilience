"""Network statistics with explicit counting rules. Every figure is computed from stored rows at request time."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import and_, func, or_, select, union
from sqlalchemy.orm import Session

from app.graph.rules import LIMITS, RULES, EdgeKind, GraphRelType
from app.graph.store import NodeKey
from app.models.enums import EdgeStatus, RegistryType
from app.models.facility import Facility
from app.models.product import Product, ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier

S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT
MODEL = {S: Supplier, F: Facility, P: Product}

COUNTING_RULES = {
    "nodes": "Registry records that are not merged tombstones (merged records are counted separately).",
    "relationships": "Stored supply relationships, product links and derived OWNS links, counted per status.",
    "active": "A relationship is active when its status is CONFIRMED and today lies inside its effective dates "
    "(open-ended dates count as always in effect).",
    "isolated": "A live registry record with no active relationship at either end. Unimported or unreviewed data "
    "therefore shows up as isolated, not as an error.",
    "components": "Connected components of the active network, ignoring direction; isolated nodes are excluded.",
}


def _active_clause(model: Any, day: date):
    return and_(
        model.status == EdgeStatus.CONFIRMED,
        or_(model.effective_from.is_(None), model.effective_from <= day),
        or_(model.effective_to.is_(None), model.effective_to >= day),
    )


def active_pairs(session: Session, day: date, cap: int) -> tuple[list[tuple[GraphRelType, NodeKey, NodeKey]], bool]:
    """Endpoints of active relationships as light tuples (column selects, no ORM objects). Returns (pairs, overflowed)."""
    pairs: list[tuple[GraphRelType, NodeKey, NodeKey]] = []
    for rt, sup, srcf, tgt in session.execute(
        select(SupplyRelationship.relationship_type, SupplyRelationship.supplier_id,
               SupplyRelationship.source_facility_id, SupplyRelationship.target_facility_id)
        .where(_active_clause(SupplyRelationship, day)).limit(cap + 1)
    ):  # fmt: skip
        typ = GraphRelType(rt.value)
        pairs.append((typ, (S, sup) if sup is not None else (F, srcf), (F, tgt)))
    for lt, sup, fac, prod in session.execute(
        select(ProductLink.link_type, ProductLink.supplier_id, ProductLink.facility_id, ProductLink.product_id)
        .where(_active_clause(ProductLink, day)).limit(cap + 1)
    ):  # fmt: skip
        pairs.append((GraphRelType(lt.value), (S, sup) if sup is not None else (F, fac), (P, prod)))
    for fid, owner in session.execute(
        select(Facility.id, Facility.owner_supplier_id).where(Facility.owner_supplier_id.is_not(None)).limit(cap + 1)
    ):
        pairs.append((GraphRelType.OWNS, (S, owner), (F, fid)))
    return pairs[: cap + 1], len(pairs) > cap


def _components(pairs: list[tuple[GraphRelType, NodeKey, NodeKey]]) -> dict[str, Any]:
    parent: dict[NodeKey, NodeKey] = {}

    def find(x: NodeKey) -> NodeKey:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for _t, a, b in pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    sizes: dict[NodeKey, int] = defaultdict(int)
    for node in parent:
        sizes[find(node)] += 1
    largest = sorted(sizes.values(), reverse=True)[:5]
    return {"computed": True, "count": len(sizes), "largest_sizes": largest}


def summary(session: Session, day: date) -> dict[str, Any]:
    nodes: dict[str, dict[str, int]] = {}
    for rtype, model in MODEL.items():
        total = session.scalar(select(func.count()).select_from(model)) or 0
        merged = session.scalar(select(func.count()).select_from(model).where(model.merged_into_id.is_not(None))) or 0
        nodes[rtype.value] = {"live": total - merged, "merged": merged}

    by_type: dict[str, dict[str, int]] = {t.value: {s.value: 0 for s in EdgeStatus} for t in GraphRelType}
    for rt, st, n in session.execute(
        select(SupplyRelationship.relationship_type, SupplyRelationship.status, func.count()).group_by(
            SupplyRelationship.relationship_type, SupplyRelationship.status)
    ):  # fmt: skip
        by_type[rt.value][st.value] += n
    for lt, st, n in session.execute(
        select(ProductLink.link_type, ProductLink.status, func.count()).group_by(ProductLink.link_type, ProductLink.status)
    ):  # fmt: skip
        by_type[lt.value][st.value] += n
    by_type[GraphRelType.OWNS.value][EdgeStatus.CONFIRMED.value] = (
        session.scalar(select(func.count()).select_from(Facility).where(Facility.owner_supplier_id.is_not(None))) or 0
    )
    by_status = {s.value: sum(by_type[t][s.value] for t in by_type) for s in EdgeStatus}
    active_total = (
        (
            session.scalar(
                select(func.count()).select_from(SupplyRelationship).where(_active_clause(SupplyRelationship, day))
            )
            or 0
        )
        + (session.scalar(select(func.count()).select_from(ProductLink).where(_active_clause(ProductLink, day))) or 0)
        + by_type[GraphRelType.OWNS.value][EdgeStatus.CONFIRMED.value]
    )
    total_edges = sum(by_status.values())

    isolated: dict[str, int] = {}
    for rtype, model in MODEL.items():
        ends = _active_ends(rtype, day)
        stmt = select(func.count()).select_from(model).where(model.merged_into_id.is_(None))
        if ends is not None:
            stmt = stmt.where(model.id.not_in(ends))
        isolated[rtype.value] = session.scalar(stmt) or 0

    pairs, overflow = active_pairs(session, day, LIMITS.scan_cap_components)
    components: dict[str, Any] = (
        {"computed": False, "reason": f"more than {LIMITS.scan_cap_components} active relationships"}
        if overflow
        else _components(pairs)
    )
    live_nodes = sum(v["live"] for v in nodes.values())
    return {
        "as_of": day,
        "nodes": {"live": live_nodes, "by_type": nodes},
        "relationships": {
            "total": total_edges,
            "active": active_total,
            "by_status": by_status,
            "by_type": by_type,
            "expired_or_future_confirmed": by_status[EdgeStatus.CONFIRMED.value] - active_total,
        },
        "isolated_nodes": {"total": sum(isolated.values()), "by_type": isolated},
        "components": components,
        "types": [
            {
                "type": r.type.value,
                "source": r.source.value,
                "destination": r.destination.value,
                "editable": r.editable,
                "meaning": r.meaning,
            }
            for r in RULES.values()
        ],  # fmt: skip
        "counting_rules": COUNTING_RULES,
        "notes": (
            [
                "No relationships are stored yet: every node is isolated until relationships are imported or proposed and confirmed."
            ]
            if total_edges == 0
            else []
        )
        + (
            ["Proposed relationships are excluded from the active network until a reviewer confirms them."]
            if by_status["PROPOSED"]
            else []
        ),
    }


def _active_ends(rtype: RegistryType, day: date):
    """UNION of node ids that have an active relationship (None if the type can never have one)."""
    parts = []
    if rtype is S:
        parts = [
            select(SupplyRelationship.supplier_id).where(
                SupplyRelationship.supplier_id.is_not(None), _active_clause(SupplyRelationship, day)
            ),
            select(ProductLink.supplier_id).where(
                ProductLink.supplier_id.is_not(None), _active_clause(ProductLink, day)
            ),
            select(Facility.owner_supplier_id).where(Facility.owner_supplier_id.is_not(None)),
        ]
    elif rtype is F:
        parts = [
            select(SupplyRelationship.target_facility_id).where(_active_clause(SupplyRelationship, day)),
            select(SupplyRelationship.source_facility_id).where(
                SupplyRelationship.source_facility_id.is_not(None), _active_clause(SupplyRelationship, day)
            ),
            select(ProductLink.facility_id).where(
                ProductLink.facility_id.is_not(None), _active_clause(ProductLink, day)
            ),
            select(Facility.id).where(Facility.owner_supplier_id.is_not(None)),
        ]
    else:
        parts = [select(ProductLink.product_id).where(_active_clause(ProductLink, day))]
    return union(*parts)


_ = EdgeKind  # re-exported for type checkers
