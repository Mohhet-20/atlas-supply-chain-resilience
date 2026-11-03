"""Scoped bulk lookups so evaluating N subjects costs a constant number of queries (no N+1)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.graph.integrity import _load_lifecycles
from app.graph.rules import LIMITS, RULES, GraphRelType
from app.graph.store import NodeKey
from app.models.enums import CandidateStatus, CandidateTier, EntityKind, RegistryType
from app.models.facility import Facility
from app.models.graph import GraphEdgeEvent
from app.models.ingestion import RecordLineage
from app.models.product import ProductLink
from app.models.registry import AuditEvent, DuplicateCandidate, EntityIdentifier
from app.models.relationship import SupplyRelationship
from app.models.supplier import Supplier
from app.quality.framework import ENTITY_KINDS, Subject
from app.registry.config import QualityConfig

_OPEN_CANDIDATE = [
    CandidateStatus.NEW,
    CandidateStatus.UNDER_REVIEW,
    CandidateStatus.REOPENED,
    CandidateStatus.DEFERRED,
    CandidateStatus.CONFIRMED_DUPLICATE,
]
_RTYPE = {
    EntityKind.SUPPLIER: RegistryType.SUPPLIER,
    EntityKind.FACILITY: RegistryType.FACILITY,
    EntityKind.PRODUCT: RegistryType.PRODUCT,
}
_ACYCLIC = {t for t, r in RULES.items() if r.cycles == "impossible" and r.type is not GraphRelType.OWNS}


def chunks(items: list[Any], size: int = LIMITS.in_chunk) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Context:
    def __init__(self, session: Session, qcfg: QualityConfig, today: date, now: datetime) -> None:
        self.session, self.qcfg, self.today, self.now = session, qcfg, today, now
        self.lineage: dict[tuple[EntityKind, int], int] = {}
        self.created_audit: set[tuple[RegistryType, int]] = set()
        self.candidates: dict[tuple[RegistryType, int], list[DuplicateCandidate]] = defaultdict(list)
        self.supplier_info: dict[int, tuple[Any, int | None]] = {}
        self.supplier_rel_count: dict[int, int] = {}
        self.product_link_count: dict[int, int] = {}
        self.product_link_units: dict[int, set[str]] = defaultdict(set)
        self.location_dupes: dict[int, list[int]] = defaultdict(list)
        self.authid_dupes: dict[tuple[RegistryType, int], list[tuple[str, str, int]]] = defaultdict(list)
        self.lifecycle: dict[NodeKey, str] = {}
        self.dup_edge_refs: set[str] = set()
        self.cycle_refs: set[str] = set()
        self.edge_evidence: set[str] = set()
        self.cycle_scan_truncated = False

    @staticmethod
    def rtype_of(kind: EntityKind) -> RegistryType:
        return _RTYPE[kind]

    # ------------------------------------------------------------------------------------------ loading

    def load(self, subjects: list[Subject]) -> None:
        by_kind: dict[EntityKind, list[Subject]] = defaultdict(list)
        for s in subjects:
            by_kind[s.kind].append(s)
        for kind in ENTITY_KINDS:
            if by_kind.get(kind):
                self._load_entities(kind, by_kind[kind])
        edges = by_kind.get(EntityKind.RELATIONSHIP, []) + by_kind.get(EntityKind.PRODUCT_LINK, [])
        if edges:
            self._load_edges(edges)

    def _load_entities(self, kind: EntityKind, subs: list[Subject]) -> None:
        rtype = _RTYPE[kind]
        ids = [s.id for s in subs]
        s = self.session
        for part in chunks(ids):
            for eid, n in s.execute(
                select(RecordLineage.entity_id, func.count())
                .where(RecordLineage.entity_type == kind, RecordLineage.entity_id.in_(part))
                .group_by(RecordLineage.entity_id)
            ):
                self.lineage[(kind, eid)] = n
            for (t,) in s.execute(
                select(AuditEvent.target_id).where(
                    AuditEvent.action == "registry.create",
                    AuditEvent.target_type == rtype.value,
                    AuditEvent.target_id.in_([str(i) for i in part]),
                )
            ):
                if t and str(t).isdigit():
                    self.created_audit.add((rtype, int(t)))
            for c in s.scalars(
                select(DuplicateCandidate).where(
                    DuplicateCandidate.entity_type == rtype,
                    DuplicateCandidate.status.in_(_OPEN_CANDIDATE),
                    DuplicateCandidate.tier != CandidateTier.INSUFFICIENT,
                    (DuplicateCandidate.left_id.in_(part)) | (DuplicateCandidate.right_id.in_(part)),
                )
            ):
                for eid in (c.left_id, c.right_id):
                    if eid in part:
                        self.candidates[(rtype, eid)].append(c)
        if kind is EntityKind.SUPPLIER:
            for part in chunks(ids):
                for sid, n in s.execute(
                    select(SupplyRelationship.supplier_id, func.count())
                    .where(SupplyRelationship.supplier_id.in_(part))
                    .group_by(SupplyRelationship.supplier_id)
                ):
                    self.supplier_rel_count[sid] = n
        if kind is EntityKind.PRODUCT:
            for part in chunks(ids):
                for pid, n in s.execute(
                    select(ProductLink.product_id, func.count())
                    .where(ProductLink.product_id.in_(part))
                    .group_by(ProductLink.product_id)
                ):
                    self.product_link_count[pid] = n
                for pid, unit in s.execute(
                    select(ProductLink.product_id, ProductLink.quantity_unit).where(
                        ProductLink.product_id.in_(part), ProductLink.quantity_unit.is_not(None)
                    )
                ):
                    self.product_link_units[pid].add(str(unit))
        # referenced suppliers (parent / owner / manufacturer) and the subject suppliers themselves
        ref_ids: set[int] = set(ids) if kind is EntityKind.SUPPLIER else set()
        for sub in subs:
            for attr in ("parent_supplier_id", "owner_supplier_id", "manufacturer_supplier_id"):
                v = getattr(sub.obj, attr, None)
                if v:
                    ref_ids.add(v)
        missing = sorted(ref_ids - set(self.supplier_info))
        for part in chunks(missing):
            for sid, status, merged in s.execute(
                select(Supplier.id, Supplier.status, Supplier.merged_into_id).where(Supplier.id.in_(part))
            ):
                self.supplier_info[sid] = (status, merged)
        if kind is EntityKind.FACILITY:
            self._load_location_dupes(subs)
        self._load_authoritative_dupes(rtype, ids)

    def _load_location_dupes(self, subs: list[Subject]) -> None:
        buckets: dict[tuple[float, float], list[int]] = defaultdict(list)
        for sub in subs:
            f: Facility = sub.obj
            if f.latitude is not None and f.longitude is not None:
                buckets[(round(f.latitude, 4), round(f.longitude, 4))].append(f.id)
        if not buckets:
            return
        lats = sorted({k[0] for k in buckets})
        for part in chunks(lats, 200):
            # A range, not SQL round(): PostgreSQL has no round(double precision, int), and the rounded key is
            # compared in Python below anyway, so the database only has to narrow the candidates.
            wanted = set(part)
            for fid, lat, lon in self.session.execute(
                select(Facility.id, Facility.latitude, Facility.longitude).where(
                    Facility.merged_into_id.is_(None),
                    Facility.latitude.is_not(None),
                    Facility.latitude.between(min(part) - 0.0001, max(part) + 0.0001),
                )
            ):
                if lon is None or round(lat, 4) not in wanted:
                    continue
                key = (round(lat, 4), round(lon, 4))
                for mine in buckets.get(key, []):
                    if mine != fid:
                        self.location_dupes[mine].append(fid)

    def _load_authoritative_dupes(self, rtype: RegistryType, ids: list[int]) -> None:
        mine: dict[tuple[str, str], list[int]] = defaultdict(list)
        for part in chunks(ids):
            for eid, scheme, value in self.session.execute(
                select(EntityIdentifier.entity_id, EntityIdentifier.scheme, EntityIdentifier.value).where(
                    EntityIdentifier.entity_type == rtype,
                    EntityIdentifier.authoritative.is_(True),
                    EntityIdentifier.entity_id.in_(part),
                )
            ):
                mine[(scheme, value)].append(eid)
        if not mine:
            return
        values = sorted({v for _, v in mine})
        for part in chunks(values):
            for eid, scheme, value in self.session.execute(
                select(EntityIdentifier.entity_id, EntityIdentifier.scheme, EntityIdentifier.value).where(
                    EntityIdentifier.entity_type == rtype,
                    EntityIdentifier.authoritative.is_(True),
                    EntityIdentifier.value.in_(part),
                )
            ):
                for owner in mine.get((scheme, value), []):
                    if owner != eid:
                        self.authid_dupes[(rtype, owner)].append((scheme, value, eid))

    def _load_edges(self, subs: list[Subject]) -> None:
        s = self.session
        keys: set[NodeKey] = set()
        supply_ids: list[int] = []
        link_ids: list[int] = []
        for sub in subs:
            e = sub.obj
            keys.add(e.source)
            keys.add(e.destination)
            (supply_ids if sub.kind is EntityKind.RELATIONSHIP else link_ids).append(sub.id)
        self.lifecycle = _load_lifecycles(s, {k for k in keys if k[1] is not None})
        for kind_name, ids in (("SUPPLY", supply_ids), ("PRODUCT", link_ids)):
            for part in chunks(ids):
                for (eid,) in s.execute(
                    select(GraphEdgeEvent.edge_id)
                    .where(GraphEdgeEvent.edge_kind == kind_name, GraphEdgeEvent.edge_id.in_(part))
                    .distinct()
                ):
                    self.edge_evidence.add(f"{kind_name}:{eid}")
        for ek, kind_name, ids in (
            (EntityKind.RELATIONSHIP, "SUPPLY", supply_ids),
            (EntityKind.PRODUCT_LINK, "PRODUCT", link_ids),
        ):
            for part in chunks(ids):
                for (eid,) in s.execute(
                    select(RecordLineage.entity_id)
                    .where(RecordLineage.entity_type == ek, RecordLineage.entity_id.in_(part))
                    .distinct()
                ):
                    self.edge_evidence.add(f"{kind_name}:{eid}")
        self._load_edge_duplicates()
        self._load_cycles()

    def _load_edge_duplicates(self) -> None:
        s = self.session
        for sid, fid in (
            (SupplyRelationship.supplier_id, SupplyRelationship.target_facility_id),
            (SupplyRelationship.source_facility_id, SupplyRelationship.target_facility_id),
        ):
            groups = s.execute(
                select(SupplyRelationship.relationship_type, sid, fid, func.count())
                .where(sid.is_not(None))
                .group_by(SupplyRelationship.relationship_type, sid, fid)
                .having(func.count() > 1)
            ).all()
            for rtype_, a, b, _n in groups:
                for (row_id,) in s.execute(
                    select(SupplyRelationship.id).where(
                        SupplyRelationship.relationship_type == rtype_, sid == a, fid == b
                    )
                ):
                    self.dup_edge_refs.add(f"SUPPLY:{row_id}")
        link_groups = s.execute(
            select(
                ProductLink.link_type,
                ProductLink.supplier_id,
                ProductLink.facility_id,
                ProductLink.product_id,
                func.count(),
            )
            .group_by(ProductLink.link_type, ProductLink.supplier_id, ProductLink.facility_id, ProductLink.product_id)
            .having(func.count() > 1)
        ).all()
        for lt, sup, fac, prod, _n in link_groups:
            q = select(ProductLink.id).where(ProductLink.link_type == lt, ProductLink.product_id == prod)
            q = q.where(ProductLink.supplier_id.is_(None) if sup is None else ProductLink.supplier_id == sup)
            q = q.where(ProductLink.facility_id.is_(None) if fac is None else ProductLink.facility_id == fac)
            for (row_id,) in s.execute(q):
                self.dup_edge_refs.add(f"PRODUCT:{row_id}")

    def _load_cycles(self) -> None:
        """Find edges that lie on a directed cycle among relationship types defined as acyclic (iterative Tarjan)."""
        from app.graph.store import link_rec, supply_rec

        adj: dict[NodeKey, list[tuple[NodeKey, str]]] = defaultdict(list)
        cap = LIMITS.scan_cap_components
        count = 0
        for r in self.session.scalars(select(SupplyRelationship).limit(cap + 1)).unique():
            e = supply_rec(r)
            count += 1
            if e.type in _ACYCLIC and e.source[1] is not None and e.destination[1] is not None:
                adj[e.source].append((e.destination, e.ref))
        for link in self.session.scalars(select(ProductLink).limit(cap + 1)).unique():
            e = link_rec(link)
            count += 1
            if e.type in _ACYCLIC and e.source[1] is not None and e.destination[1] is not None:
                adj[e.source].append((e.destination, e.ref))
        self.cycle_scan_truncated = count > cap
        index: dict[NodeKey, int] = {}
        low: dict[NodeKey, int] = {}
        on: set[NodeKey] = set()
        stack: list[NodeKey] = []
        comp: dict[NodeKey, int] = {}
        counter = 0
        ncomp = 0
        nodes = set(adj) | {d for outs in adj.values() for d, _ in outs}
        for root in nodes:
            if root in index:
                continue
            work: list[tuple[NodeKey, Iterable[tuple[NodeKey, str]]]] = [(root, iter(adj.get(root, [])))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on.add(root)
            while work:
                node, it = work[-1]
                advanced = False
                for nxt, _ref in it:
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter
                        counter += 1
                        stack.append(nxt)
                        on.add(nxt)
                        work.append((nxt, iter(adj.get(nxt, []))))
                        advanced = True
                        break
                    if nxt in on:
                        low[node] = min(low[node], index[nxt])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp[w] = ncomp
                        if w == node:
                            break
                    ncomp += 1
        size: dict[int, int] = defaultdict(int)
        for c in comp.values():
            size[c] += 1
        for src, outs in adj.items():
            for dst, ref in outs:
                if src == dst or (comp.get(src) == comp.get(dst) and size[comp[src]] > 1):
                    self.cycle_refs.add(ref)
