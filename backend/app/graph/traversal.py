"""Bounded traversal: neighbours, upstream/downstream expansion and simple-path search.

All traversals are level-synchronous: one batched query round per level (never per node), hard node/edge/time caps,
and a visited set so cycles cannot cause repeated work. Direction follows relationship semantics: *downstream* follows
edges from source to destination; *upstream* follows them towards the node (see ``rules.py``).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.graph.rules import LIMITS
from app.graph.store import EdgeFilter, EdgeRec, NodeKey, fetch_incident

DIRECTION_TO_FETCH = {"downstream": "out", "upstream": "in", "both": "both"}


@dataclass
class Expansion:
    start: NodeKey
    direction: str
    requested_depth: int
    depth_of: dict[NodeKey, int] = field(default_factory=dict)
    edges: dict[str, EdgeRec] = field(default_factory=dict)
    hop_of: dict[str, int] = field(default_factory=dict)
    truncated: bool = False
    truncation_reasons: list[str] = field(default_factory=list)
    max_depth_reached: int = 0
    nodes_at_depth_limit: int = 0
    starts: tuple[NodeKey, ...] = ()

    def truncate(self, reason: str) -> None:
        self.truncated = True
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)


def expand(
    session: Session,
    start: NodeKey,
    direction: str,
    depth: int,
    flt: EdgeFilter,
    max_nodes: int,
    max_edges: int,
) -> Expansion:
    """Breadth-first expansion from ``start`` bounded by depth, node count, edge count and a time budget."""
    return expand_multi(session, [start], direction, depth, flt, max_nodes, max_edges)


def expand_multi(
    session: Session,
    starts: Sequence[NodeKey],
    direction: str,
    depth: int,
    flt: EdgeFilter,
    max_nodes: int,
    max_edges: int,
    time_budget: float | None = None,
) -> Expansion:
    """Like :func:`expand` from several start nodes at once (all at depth 0), still one batched query per level."""
    ordered_starts = tuple(sorted(set(starts), key=lambda k: (k[0].value, k[1])))
    start = ordered_starts[0]
    start_set = set(ordered_starts)
    fetch_dir = DIRECTION_TO_FETCH[direction]
    out = Expansion(start=start, direction=direction, requested_depth=depth, starts=ordered_starts)
    for k in ordered_starts:
        out.depth_of[k] = 0
    deadline = time.monotonic() + (time_budget if time_budget is not None else LIMITS.time_budget_seconds)
    frontier: set[NodeKey] = set(ordered_starts)
    for level in range(1, depth + 1):
        if not frontier:
            break
        remaining = max_edges - len(out.edges)
        if remaining <= 0:
            out.truncate("edge_limit")
            break
        if time.monotonic() > deadline:
            out.truncate("time_budget")
            break
        records, more = fetch_incident(session, frontier, fetch_dir, flt, remaining)
        if more:
            out.truncate("edge_limit")
        discovered: set[NodeKey] = set()
        for e in records:
            if e.ref in out.edges:
                continue
            ends = [e.source, e.destination]
            neighbours = [
                k
                for k in ends
                if k not in out.depth_of and _follows(e, k, frontier, fetch_dir) and _node_ok(k, start_set, flt)
            ]
            blocked = False
            for k in neighbours:
                if k in discovered:
                    continue
                if len(out.depth_of) + len(discovered) >= max_nodes:
                    out.truncate("node_limit")
                    blocked = True
                    break
                discovered.add(k)
            if blocked:
                continue
            if all(k in out.depth_of or k in discovered for k in ends):
                out.edges[e.ref] = e
                out.hop_of[e.ref] = level
        for k in discovered:
            out.depth_of[k] = level
        if discovered:
            out.max_depth_reached = level
        frontier = discovered
    out.nodes_at_depth_limit = len(frontier) if out.max_depth_reached == depth else 0
    return out


def _follows(e: EdgeRec, new: NodeKey, frontier: set[NodeKey], fetch_dir: str) -> bool:
    """True if ``new`` is reached from a frontier node along the allowed direction."""
    if fetch_dir == "out":
        return new == e.destination and e.source in frontier
    if fetch_dir == "in":
        return new == e.source and e.destination in frontier
    return (new == e.destination and e.source in frontier) or (new == e.source and e.destination in frontier)


def _node_ok(key: NodeKey, start: NodeKey | set[NodeKey], flt: EdgeFilter) -> bool:
    if isinstance(start, set):
        return key in start or flt.node_types is None or key[0] in flt.node_types
    return key == start or flt.node_types is None or key[0] in flt.node_types


@dataclass
class PathResult:
    paths: list[list[EdgeRec]]  # each path = ordered edges; nodes are derived by walking from the start
    truncated: bool
    reasons: list[str]
    shortest_length: int | None


def find_paths(
    session: Session,
    start: NodeKey,
    goal: NodeKey,
    flt: EdgeFilter,
    *,
    mode: str,
    max_length: int,
    max_paths: int,
) -> PathResult:
    """Simple (cycle-free) paths in non-decreasing length order, up to ``max_paths``.

    ``mode`` is ``directed`` (follow material flow from ``start`` to ``goal``) or ``undirected`` (ignore direction).
    """
    fetch_dir = "out" if mode == "directed" else "both"
    reasons: list[str] = []
    results: list[list[EdgeRec]] = []
    if start == goal:
        return PathResult([], False, [], 0)
    deadline = time.monotonic() + LIMITS.time_budget_seconds
    # a partial path = (end node, nodes visited, edges taken)
    level: list[tuple[NodeKey, tuple[NodeKey, ...], tuple[EdgeRec, ...]]] = [(start, (start,), ())]
    adjacency: dict[NodeKey, list[EdgeRec]] = {}
    for _length in range(1, max_length + 1):
        if not level or len(results) >= max_paths:
            break
        if time.monotonic() > deadline:
            reasons.append("time_budget")
            break
        ends = {p[0] for p in level if p[0] not in adjacency}
        if ends:
            records, more = fetch_incident(session, ends, fetch_dir, flt, LIMITS.max_edges * 20)
            if more:
                reasons.append("edge_scan_limit")
            fetched: dict[NodeKey, list[EdgeRec]] = {k: [] for k in ends}
            for e in records:
                if e.source in fetched:
                    fetched[e.source].append(e)
                if fetch_dir == "both" and e.destination in fetched and e.destination != e.source:
                    fetched[e.destination].append(e)
            adjacency.update(fetched)
        nxt: list[tuple[NodeKey, tuple[NodeKey, ...], tuple[EdgeRec, ...]]] = []
        for end, seen, taken in level:
            for e in adjacency.get(end, ()):
                other = e.destination if e.source == end else e.source
                if fetch_dir == "out" and e.source != end:
                    continue
                if other in seen or not _node_ok(other, start, flt):
                    continue
                path_edges = taken + (e,)
                if other == goal:
                    results.append(list(path_edges))
                    if len(results) >= max_paths:
                        break
                    continue
                if len(nxt) >= LIMITS.max_partial_paths_per_level:
                    if "path_frontier_limit" not in reasons:
                        reasons.append("path_frontier_limit")
                    continue
                nxt.append((other, seen + (other,), path_edges))
            if len(results) >= max_paths:
                break
        level = nxt
    shortest = min((len(p) for p in results), default=None)
    return PathResult(results, bool(reasons), reasons, shortest)
