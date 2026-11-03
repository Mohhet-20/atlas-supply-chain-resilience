"""Graph algorithms over a lane multigraph. Pure, deterministic and bounded; no database access.

* ``dijkstra`` - shortest path for a non-negative additive weight (ties broken by the lane-id sequence, so equal-cost routes
  always come back in the same order).
* ``reverse_distances`` - distance from every facility to the target, used as an admissible lower bound for pruning.
* ``yen`` - k loopless shortest paths for one additive weight (used as a safety net when the exhaustive search is truncated).
* ``enumerate_paths`` - every loopless route up to a hop limit, with caller-supplied pruning that must only cut routes that can
  never become feasible. Reports whether the expansion budget stopped the search.

A* is intentionally not used: an admissible heuristic needs verified great-circle geometry and a guarantee that every lane is at
least as long as the straight line, which the registered data does not give. Reverse-distance bounds are exact and need neither.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from app.routing.network import Lane

Weight = Callable[[Lane], "float | None"]  # None = the lane cannot be used under this weight


def _key(path: list[Lane]) -> tuple[int, ...]:
    return tuple(ln.id for ln in path)


def dijkstra(
    out: dict[int, list[Lane]],
    source: int,
    target: int,
    weight: Weight,
    *,
    blocked_nodes: frozenset[int] = frozenset(),
    blocked_lanes: frozenset[int] = frozenset(),
) -> list[Lane] | None:
    """Minimum-weight path by Dijkstra's algorithm. Raises ValueError on a negative weight (the algorithm would be wrong)."""
    if source in blocked_nodes or target in blocked_nodes:
        return None
    best: dict[int, tuple[float, tuple[int, ...]]] = {source: (0.0, ())}
    paths: dict[int, list[Lane]] = {source: []}
    heap: list[tuple[float, tuple[int, ...], int]] = [(0.0, (), source)]
    done: set[int] = set()
    while heap:
        dist, ids, node = heapq.heappop(heap)
        if node in done:
            continue
        done.add(node)
        if node == target:
            return paths[node]
        for ln in out.get(node, ()):
            if ln.id in blocked_lanes or ln.dest in blocked_nodes or ln.dest in done:
                continue
            w = weight(ln)
            if w is None:
                continue
            if w < 0:
                raise ValueError("Dijkstra requires non-negative weights.")
            cand = (dist + w, (*ids, ln.id))
            if ln.dest not in best or cand < best[ln.dest]:
                best[ln.dest] = cand
                paths[ln.dest] = [*paths[node], ln]
                heapq.heappush(heap, (cand[0], cand[1], ln.dest))
    return None


def reverse_distances(out: dict[int, list[Lane]], target: int, weight: Weight) -> dict[int, float]:
    """Minimum weight from each facility to ``target`` (lanes whose weight is None count as 0, which keeps it a lower bound)."""
    rev: dict[int, list[tuple[int, float]]] = {}
    for lanes in out.values():
        for ln in lanes:
            w = weight(ln)
            rev.setdefault(ln.dest, []).append((ln.origin, 0.0 if w is None else max(0.0, w)))
    dist = {target: 0.0}
    heap = [(0.0, target)]
    while heap:
        d, n = heapq.heappop(heap)
        if d > dist.get(n, float("inf")):
            continue
        for p, w in rev.get(n, ()):
            nd = d + w
            if nd < dist.get(p, float("inf")):
                dist[p] = nd
                heapq.heappush(heap, (nd, p))
    return dist


def path_weight(path: Iterable[Lane], weight: Weight) -> float | None:
    total = 0.0
    for ln in path:
        w = weight(ln)
        if w is None:
            return None
        total += w
    return total


def yen(out: dict[int, list[Lane]], source: int, target: int, weight: Weight, k: int) -> list[list[Lane]]:
    """Yen's algorithm: the k cheapest loopless paths (as lane sequences), cheapest first, deterministic on ties."""
    first = dijkstra(out, source, target, weight)
    if first is None or k < 1:
        return []
    accepted: list[list[Lane]] = [first]
    candidates: list[tuple[float, tuple[int, ...], list[Lane]]] = []
    seen = {_key(first)}
    while len(accepted) < k:
        last = accepted[-1]
        for i in range(len(last)):
            spur = last[i].origin
            root = last[:i]
            blocked_lanes = {p[i].id for p in accepted if len(p) > i and _key(p[:i]) == _key(root)}
            blocked_nodes = frozenset([ln.origin for ln in root])
            tail = dijkstra(
                out, spur, target, weight, blocked_nodes=blocked_nodes, blocked_lanes=frozenset(blocked_lanes)
            )
            if tail is None:
                continue
            full = [*root, *tail]
            kk = _key(full)
            if kk in seen:
                continue
            w = path_weight(full, weight)
            if w is None:
                continue
            seen.add(kk)
            heapq.heappush(candidates, (w, kk, full))
        if not candidates:
            break
        accepted.append(heapq.heappop(candidates)[2])
    return accepted


@dataclass
class SearchStats:
    expansions: int = 0
    complete: int = 0
    pruned: int = 0
    truncated: bool = False


def enumerate_paths(
    out: dict[int, list[Lane]],
    source: int,
    target: int,
    max_hops: int,
    *,
    allow: Callable[[Lane], bool] | None = None,
    prune: Callable[[list[Lane]], bool] | None = None,
    budget: int = 200_000,
    stop: Callable[[], None] | None = None,
) -> tuple[list[list[Lane]], SearchStats]:
    """Every loopless route from source to target of at most ``max_hops`` lanes, in a deterministic order.

    ``prune(partial)`` may return True only when no completion of ``partial`` can be feasible. ``stop`` may raise to abort (time
    budget / cancellation). When ``budget`` expansions are used up the search stops and ``stats.truncated`` is set.
    """
    stats = SearchStats()
    found: list[list[Lane]] = []
    path: list[Lane] = []
    on_path = {source}

    def dfs(node: int) -> None:
        if stats.truncated:
            return
        for ln in out.get(node, ()):
            if ln.dest in on_path or (allow is not None and not allow(ln)):
                continue
            stats.expansions += 1
            if stats.expansions > budget:
                stats.truncated = True
                return
            if stop is not None and stats.expansions % 512 == 0:
                stop()
            path.append(ln)
            if prune is not None and prune(path):
                stats.pruned += 1
            elif ln.dest == target:
                stats.complete += 1
                found.append(list(path))
            elif len(path) < max_hops:
                on_path.add(ln.dest)
                dfs(ln.dest)
                on_path.discard(ln.dest)
            path.pop()
            if stats.truncated:
                return

    dfs(source)
    return found, stats
