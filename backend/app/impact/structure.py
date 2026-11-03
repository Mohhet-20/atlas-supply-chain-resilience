"""Pure graph-structure computations over the affected subgraph: bounded path enumeration and structural criticality.

Everything here works on in-memory edges, is deterministic (sorted iteration) and bounded (step caps), so it can be tested
on hand-built graphs. Edge direction is always respected: a path follows edges from source to destination.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from app.graph.store import EdgeRec, NodeKey


@dataclass
class PathIndex:
    count: dict[NodeKey, int] = field(default_factory=lambda: defaultdict(int))  # simple paths found, all events
    per_event: dict[tuple[NodeKey, str], int] = field(default_factory=lambda: defaultdict(int))
    stored: dict[NodeKey, list[tuple[str, tuple[str, ...]]]] = field(default_factory=lambda: defaultdict(list))
    used: dict[tuple[NodeKey, str], tuple[set[str], set[NodeKey]]] = field(default_factory=dict)
    through: dict[NodeKey, int] = field(default_factory=lambda: defaultdict(int))  # paths passing through as interior
    steps: int = 0
    complete: bool = True


def enumerate_paths(
    event_edges: dict[str, list[EdgeRec]],
    node_seeds: dict[str, list[NodeKey]],
    edge_seeds: dict[str, list[EdgeRec]],
    *,
    max_length: int,
    max_store_per_event: int,
    max_steps: int,
) -> PathIndex:
    """Breadth-first enumeration of simple paths per initiating event, shortest first.

    ``event_edges`` holds the affected edges attributed to each event. Paths start at an event's target entities, or at the
    failed/delayed relationship itself. Enumeration stops (``complete = False``) when ``max_steps`` edge expansions are
    spent, so a combinatorial graph cannot run away; counts are then lower bounds.
    """
    idx = PathIndex()
    for uid in sorted(event_edges):
        adj: dict[NodeKey, list[EdgeRec]] = defaultdict(list)
        for e in sorted(event_edges[uid], key=lambda x: x.ref):
            adj[e.source].append(e)
        queue: deque[tuple[NodeKey, tuple[str, ...], tuple[NodeKey, ...]]] = deque()
        for s in sorted(node_seeds.get(uid, ()), key=lambda k: (k[0].value, k[1])):
            queue.append((s, (), (s,)))
        for e in sorted(edge_seeds.get(uid, ()), key=lambda x: x.ref):
            _arrive(idx, uid, e.destination, (e.ref,), (e.source, e.destination), max_store_per_event)
            queue.append((e.destination, (e.ref,), (e.source, e.destination)))
        while queue:
            node, refs, nodes = queue.popleft()
            for e in adj.get(node, ()):
                d = e.destination
                if d in nodes or len(refs) + 1 > max_length:
                    continue
                idx.steps += 1
                if idx.steps > max_steps:
                    idx.complete = False
                    return idx
                nrefs, nnodes = refs + (e.ref,), nodes + (d,)
                _arrive(idx, uid, d, nrefs, nnodes, max_store_per_event)
                queue.append((d, nrefs, nnodes))
    return idx


def _arrive(idx: PathIndex, uid: str, d: NodeKey, refs: tuple[str, ...], nodes: tuple[NodeKey, ...], cap: int) -> None:
    idx.count[d] += 1
    idx.per_event[(d, uid)] += 1
    if idx.per_event[(d, uid)] <= cap:
        idx.stored[d].append((uid, refs))
    ed, nd = idx.used.setdefault((d, uid), (set(), set()))
    ed.update(refs)
    nd.update(nodes[:-1])
    for n in nodes[1:-1]:
        idx.through[n] += 1


def reachable(adj: dict[NodeKey, set[NodeKey]], roots: set[NodeKey], banned: NodeKey | None = None) -> set[NodeKey]:
    """Entities reachable from ``roots`` following edge direction, optionally with one entity removed."""
    seen = {r for r in roots if r != banned}
    queue = deque(sorted(seen, key=lambda k: (k[0].value, k[1])))
    while queue:
        u = queue.popleft()
        for v in sorted(adj.get(u, ()), key=lambda k: (k[0].value, k[1])):
            if v != banned and v not in seen:
                seen.add(v)
                queue.append(v)
    return seen


def roots_reaching(
    adj: dict[NodeKey, set[NodeKey]], roots: set[NodeKey], cap: int = 200
) -> dict[NodeKey, set[NodeKey]] | None:
    """For each entity, which roots reach it. ``None`` when there are too many roots to compute cheaply."""
    if len(roots) > cap:
        return None
    out: dict[NodeKey, set[NodeKey]] = defaultdict(set)
    for r in roots:
        for v in reachable(adj, {r}):
            out[v].add(r)
    return out
