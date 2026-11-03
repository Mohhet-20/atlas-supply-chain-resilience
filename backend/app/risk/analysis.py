"""Pure network analysis used by the factors: supplier sets of a node, shares, HHI, criticality weights, freshness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from app.graph.rules import GraphRelType
from app.graph.store import NodeKey
from app.models.enums import EdgeStatus, Freshness, RegistryType
from app.risk.methodology import RiskConfig
from app.risk.types import EdgeFact, NetworkFacts, SupplierFacts

SHARE_DOCUMENTED = "documented"
SHARE_COUNT = "count_based"


def freshness_of(ts: datetime | None, now: datetime, cfg: RiskConfig) -> Freshness:
    if ts is None:
        return Freshness.UNKNOWN
    age = (now - ts).days
    if age <= cfg.freshness.fresh_days:
        return Freshness.FRESH
    if age <= cfg.freshness.aging_days:
        return Freshness.AGING
    return Freshness.STALE


_ORDER = {Freshness.FRESH: 0, Freshness.AGING: 1, Freshness.UNKNOWN: 2, Freshness.STALE: 3}


def worst_freshness(bands: list[Freshness]) -> Freshness:
    return max(bands, key=lambda b: _ORDER[b]) if bands else Freshness.UNKNOWN


def crit_weight(edge: EdgeFact, cfg: RiskConfig) -> tuple[float, bool]:
    """(weight, known). A missing criticality uses the labelled default and is reported as an approximation."""
    if edge.criticality and edge.criticality in cfg.criticality_weights:
        return cfg.criticality_weights[edge.criticality], True
    return cfg.unknown_criticality_weight, False


@dataclass
class NodeView:
    key: NodeKey
    code: str
    name: str
    subject_edge: EdgeFact
    confirmed: list[int] = field(
        default_factory=list
    )  # distinct viable supplier ids with a counted edge (includes subject)
    proposed: list[int] = field(default_factory=list)  # suppliers with only a PROPOSED edge
    ignored: dict[str, int] = field(default_factory=dict)  # rejected / inactive / expired / future edge counts
    shares: dict[int, float] = field(default_factory=dict)  # fractions summing to 1 when documented
    share_basis: str = SHARE_COUNT
    hhi: float | None = None
    edges: list[EdgeFact] = field(default_factory=list)  # counted edges onto the node (evidence)

    @property
    def n(self) -> int:
        return len(self.confirmed)

    @property
    def is_product(self) -> bool:
        return self.key[0] is RegistryType.PRODUCT


def node_views(sf: SupplierFacts, net: NetworkFacts, today: date, cfg: RiskConfig) -> list[NodeView]:
    """One view per node the supplier is confirmed to serve, ordered by node type then id (deterministic)."""
    out: list[NodeView] = []
    own = {e.node: e for e in sf.edges if e.counts(today)}
    for key in sorted(own, key=lambda k: (k[0].value, k[1])):
        info = net.nodes.get(key)
        view = NodeView(
            key=key, code=info.code if info else str(key[1]), name=info.name if info else "", subject_edge=own[key]
        )
        viable: dict[int, EdgeFact] = {}
        proposed: set[int] = set()
        ignored: dict[str, int] = {}
        for e in net.node_edges.get(key, []):
            sup = net.suppliers.get(e.supplier_id)
            if e.supplier_id != sf.info.id and (
                sup is None or sup.merged_into_id is not None or sup.status == "INACTIVE"
            ):
                ignored["supplier_not_viable"] = ignored.get("supplier_not_viable", 0) + 1
                continue
            if e.counts(today):
                viable[e.supplier_id] = e
            elif e.status == EdgeStatus.PROPOSED.value:
                proposed.add(e.supplier_id)
            elif e.status == EdgeStatus.CONFIRMED.value:
                ignored[e.timing(today).lower()] = ignored.get(e.timing(today).lower(), 0) + 1
            else:
                ignored[e.status.lower()] = ignored.get(e.status.lower(), 0) + 1
        view.confirmed = sorted(viable)
        view.proposed = sorted(proposed - set(viable))
        view.ignored = dict(sorted(ignored.items()))
        view.edges = [viable[i] for i in view.confirmed]
        view.shares, view.share_basis, view.hhi = shares_for(view.edges, cfg)
        out.append(view)
    return out


def shares_for(edges: list[EdgeFact], cfg: RiskConfig) -> tuple[dict[int, float], str, float | None]:
    """Documented shares (every supplier has one and they total 100% within tolerance) or equal-share counts.

    HHI is returned only for documented shares; a count-based "HHI" would be 1/n by construction and is not reported.
    """
    if not edges:
        return {}, SHARE_COUNT, None
    pcts = [e.share_pct for e in edges]
    if all(p is not None for p in pcts):
        total = sum(p for p in pcts if p is not None)
        if total > 0 and abs(total - 100.0) <= cfg.share_sum_tolerance_pct:
            shares = {e.supplier_id: (e.share_pct or 0.0) / total for e in edges}
            return shares, SHARE_DOCUMENTED, round(sum(s * s for s in shares.values()), 6)
    n = len(edges)
    return {e.supplier_id: 1.0 / n for e in edges}, SHARE_COUNT, None


def hhi_from_shares(shares_pct: list[float], tolerance_pct: float = 2.0) -> float | None:
    """HHI (0..1) from supply shares expressed in percent. ``None`` unless the shares total 100% within tolerance."""
    if not shares_pct or any(s < 0 for s in shares_pct):
        return None
    total = sum(shares_pct)
    if total <= 0 or abs(total - 100.0) > tolerance_pct:
        return None
    return round(sum((s / total) ** 2 for s in shares_pct), 6)


def edge_type_label(e: EdgeFact) -> str:
    return GraphRelType(e.type).value
