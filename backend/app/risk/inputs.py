"""Batched loading of everything the risk engine reads. Constant query count per chunk of suppliers (no N+1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.graph.rules import LIMITS
from app.graph.store import EdgeRec, NodeKey, link_rec, load_nodes, supply_rec
from app.models.enums import EntityKind, ProductLinkType, RegistryType, RelationshipType, ScoreState
from app.models.facility import Facility
from app.models.ingestion import DataQualityFinding
from app.models.observation import Observation
from app.models.product import ProductLink
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.quality import engine as quality_engine
from app.quality import service as quality_service
from app.quality.context import chunks
from app.quality.findings import OPEN_LIKE
from app.registry.normalize import normalize_name
from app.risk.types import (
    EdgeFact,
    FacilityInfo,
    FindingFact,
    NetworkFacts,
    NodeInfo,
    ObsFact,
    QualityFact,
    SupplierFacts,
    SupplierInfo,
)

S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT
CHUNK = 100


def _edge_fact(rec: EdgeRec) -> EdgeFact:
    return EdgeFact(
        ref=rec.ref, code=rec.code, type=rec.type.value, supplier_id=rec.source[1], node=rec.destination,
        status=rec.status.value, criticality=rec.criticality, share_pct=rec.supply_share_pct, confidence=rec.confidence,
        provenance=rec.provenance, source_namespace=rec.source_namespace, effective_from=rec.effective_from,
        effective_to=rec.effective_to, updated_at=rec.updated_at,
    )  # fmt: skip


def supplier_info(s: Supplier) -> SupplierInfo:
    return SupplierInfo(
        id=s.id, code=s.supplier_code, name=s.name, country=s.country_or_region or "", status=s.status.value,
        provenance=s.provenance.value, source_namespace=s.source_namespace, updated_at=s.updated_at,
        merged_into_id=s.merged_into_id,
    )  # fmt: skip


def facility_info(f: Facility) -> FacilityInfo:
    return FacilityInfo(
        id=f.id, code=f.facility_code, name=f.name, country=f.country_or_region or "", latitude=f.latitude,
        longitude=f.longitude, location_status=f.location_status.value, status=f.status.value,
        provenance=f.provenance.value, updated_at=f.updated_at,
    )  # fmt: skip


def region_labels(country: str, facilities: Iterable[FacilityInfo]) -> frozenset[str]:
    labels = {normalize_name(country)} | {normalize_name(f.country) for f in facilities}
    return frozenset(x for x in labels if x)


def _all_edges_onto(session: Session, fac_ids: list[int], prod_ids: list[int]) -> list[EdgeFact]:
    out: list[EdgeFact] = []
    for part in chunks(fac_ids, LIMITS.in_chunk):
        q = select(SupplyRelationship).where(
            SupplyRelationship.relationship_type == RelationshipType.SUPPLIES,
            SupplyRelationship.target_facility_id.in_(part),
        )
        out.extend(_edge_fact(supply_rec(r)) for r in session.scalars(q.order_by(SupplyRelationship.id)).unique())
    for part in chunks(prod_ids, LIMITS.in_chunk):
        q = select(ProductLink).where(
            ProductLink.link_type == ProductLinkType.SUPPLIES_PRODUCT, ProductLink.product_id.in_(part)
        )
        out.extend(_edge_fact(link_rec(r)) for r in session.scalars(q.order_by(ProductLink.id)).unique())
    return out


def load_facts(session: Session, supplier_ids: list[int], today: date) -> tuple[dict[int, SupplierFacts], NetworkFacts]:
    """Facts for ``supplier_ids`` (at most ``CHUNK`` at a time is recommended) plus the supplier sets of their nodes."""
    ids = sorted(set(supplier_ids))
    facts: dict[int, SupplierFacts] = {}
    net = NetworkFacts()
    for s in session.scalars(select(Supplier).where(Supplier.id.in_(ids))).unique():
        facts[s.id] = SupplierFacts(info=supplier_info(s))
        net.suppliers[s.id] = facts[s.id].info
    if not facts:
        return facts, net
    owned: dict[int, list[FacilityInfo]] = defaultdict(list)
    for f in session.scalars(
        select(Facility)
        .where(Facility.owner_supplier_id.in_(list(facts)), Facility.merged_into_id.is_(None))
        .order_by(Facility.id)
    ).unique():
        owned[f.owner_supplier_id].append(facility_info(f))  # type: ignore[index]
    own_edges: list[EdgeFact] = []
    row_ids: dict[str, list[int]] = {"SUPPLY": [], "PRODUCT": []}
    for r in session.scalars(
        select(SupplyRelationship)
        .where(
            SupplyRelationship.relationship_type == RelationshipType.SUPPLIES,
            SupplyRelationship.supplier_id.in_(list(facts)),
        )
        .order_by(SupplyRelationship.id)
    ).unique():
        own_edges.append(_edge_fact(supply_rec(r)))
        row_ids["SUPPLY"].append(r.id)
    for lk in session.scalars(
        select(ProductLink)
        .where(ProductLink.link_type == ProductLinkType.SUPPLIES_PRODUCT, ProductLink.supplier_id.in_(list(facts)))
        .order_by(ProductLink.id)
    ).unique():
        own_edges.append(_edge_fact(link_rec(lk)))
        row_ids["PRODUCT"].append(lk.id)
    for e in own_edges:
        facts[e.supplier_id].edges.append(e)
    for sid, fl in owned.items():
        facts[sid].owned_facilities = fl

    # every supplier's relationships onto the nodes this chunk touches
    fac_nodes = sorted({e.node[1] for e in own_edges if e.node[0] is F})
    prod_nodes = sorted({e.node[1] for e in own_edges if e.node[0] is P})
    for e in _all_edges_onto(session, fac_nodes, prod_nodes):
        net.node_edges.setdefault(e.node, []).append(e)
    for key in {e.node for e in own_edges}:
        net.node_edges.setdefault(key, [])
    other_ids = sorted({e.supplier_id for lst in net.node_edges.values() for e in lst} - set(facts))
    for part in chunks(other_ids, LIMITS.in_chunk):
        for s in session.scalars(select(Supplier).where(Supplier.id.in_(part))).unique():
            net.suppliers[s.id] = supplier_info(s)
        for f in session.scalars(
            select(Facility).where(Facility.owner_supplier_id.in_(part), Facility.merged_into_id.is_(None))
        ).unique():
            owned[f.owner_supplier_id].append(facility_info(f))  # type: ignore[index]
    for sid, info in net.suppliers.items():
        net.regions[sid] = region_labels(info.country, owned.get(sid, []))
    for key, rec in load_nodes(session, list(net.node_edges)).items():
        net.nodes[key] = NodeInfo(key=key, code=rec.code, name=rec.name, lifecycle=rec.lifecycle)

    for o in session.scalars(
        select(Observation)
        .where(Observation.supplier_id.in_(list(facts)))
        .order_by(Observation.observed_at, Observation.id)
    ):
        facts[o.supplier_id].observations.append(  # type: ignore[index]
            ObsFact(code=o.observation_code, metric=o.metric_name, value=o.value, unit=o.unit, observed_at=o.observed_at,
                    provenance=o.provenance.value, source_label=o.source_label)
        )  # fmt: skip

    # Quality: latest score per subject, and open findings (disabled rules and accepted exceptions excluded)
    sup_scores = quality_engine.latest_scores(session, EntityKind.SUPPLIER, list(facts))
    for sid, sc in sup_scores.items():
        facts[sid].quality = QualityFact(
            f"SUPPLIER:{sid}", sc.state.value, sc.score if sc.state is ScoreState.SCORED else None, sc.evaluated_at
        )
    ref_by_row = {
        ("SUPPLY", rid): e for e in own_edges if e.ref.startswith("SUPPLY:") for rid in [int(e.ref.split(":")[1])]
    }
    ref_by_row |= {
        ("PRODUCT", rid): e for e in own_edges if e.ref.startswith("PRODUCT:") for rid in [int(e.ref.split(":")[1])]
    }
    for kind, tag, rows in (
        (EntityKind.RELATIONSHIP, "SUPPLY", row_ids["SUPPLY"]),
        (EntityKind.PRODUCT_LINK, "PRODUCT", row_ids["PRODUCT"]),
    ):
        for rid, sc in quality_engine.latest_scores(session, kind, rows).items():
            e = ref_by_row[(tag, rid)]
            facts[e.supplier_id].edge_quality[e.ref] = QualityFact(
                f"{kind.value}:{rid}",
                sc.state.value,
                sc.score if sc.state is ScoreState.SCORED else None,
                sc.evaluated_at,
            )
    where_base = quality_service.base_where(session)
    clauses = [
        and_(DataQualityFinding.entity_type == EntityKind.SUPPLIER, DataQualityFinding.entity_id.in_(list(facts)))
    ]
    if row_ids["SUPPLY"]:
        clauses.append(
            and_(
                DataQualityFinding.entity_type == EntityKind.RELATIONSHIP,
                DataQualityFinding.entity_id.in_(row_ids["SUPPLY"]),
            )
        )
    if row_ids["PRODUCT"]:
        clauses.append(
            and_(
                DataQualityFinding.entity_type == EntityKind.PRODUCT_LINK,
                DataQualityFinding.entity_id.in_(row_ids["PRODUCT"]),
            )
        )
    for f in session.scalars(
        select(DataQualityFinding)
        .where(or_(*clauses), DataQualityFinding.resolution_status.in_(OPEN_LIKE), *where_base)
        .order_by(DataQualityFinding.id)
    ):
        if f.entity_type is EntityKind.SUPPLIER:
            owner = f.entity_id
        else:
            tag = "SUPPLY" if f.entity_type is EntityKind.RELATIONSHIP else "PRODUCT"
            owner = ref_by_row[(tag, f.entity_id)].supplier_id
        facts[owner].findings.append(  # type: ignore[index]
            FindingFact(id=f.id, rule_id=f.rule_id, severity=f.severity.value, subject=f"{f.entity_type.value}:{f.entity_id}",  # type: ignore[union-attr]
                        message=f.message, first_detected_at=f.first_detected_at or f.created_at)
        )  # fmt: skip
    return facts, net


def graph_snapshot(session: Session) -> dict:
    """Cheap identifier of the graph and quality state a run read (counts and newest change), not a data copy."""
    from sqlalchemy import func

    from app.models.quality import QualityEvalRun

    sup_n, sup_max = session.execute(select(func.count(), func.max(SupplyRelationship.updated_at))).one()
    lnk_n, lnk_max = session.execute(select(func.count(), func.max(ProductLink.updated_at))).one()
    quality_run = session.scalar(select(func.max(QualityEvalRun.id)))
    return {
        "supply_relationships": sup_n, "supply_latest_change": sup_max.isoformat() if sup_max else None,
        "product_links": lnk_n, "product_latest_change": lnk_max.isoformat() if lnk_max else None,
        "latest_quality_run_id": quality_run,
    }  # fmt: skip


def load_reach(session: Session, supplier_id: int, depth: int, max_nodes: int, max_edges: int) -> dict:
    """Bounded downstream traversal from a supplier using the traversal (context only; never scored)."""
    from app.graph.store import EdgeFilter
    from app.graph.traversal import expand
    from app.models.enums import EdgeStatus

    start: NodeKey = (S, supplier_id)
    exp = expand(
        session,
        start,
        "downstream",
        depth,
        EdgeFilter(statuses=frozenset({EdgeStatus.CONFIRMED})),
        max_nodes,
        max_edges,
    )
    by_type: dict[str, int] = defaultdict(int)
    for key in exp.depth_of:
        if key != start:
            by_type[key[0].value] += 1
    return {
        "depth": depth, "nodes_by_type": dict(sorted(by_type.items())), "edges": len(exp.edges),
        "truncated": exp.truncated, "reasons": sorted(exp.truncation_reasons),
    }  # fmt: skip
