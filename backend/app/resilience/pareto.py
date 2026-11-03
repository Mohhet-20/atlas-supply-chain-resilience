"""Dominance, normalisation, weighted scoring and thinning. All vectors are in internal minimisation form."""

from __future__ import annotations

from typing import Any

from app.resilience.objectives import sign


def vector(values: dict[str, Any], oids: list[str]) -> tuple[float, ...]:
    return tuple(sign(o) * float(values[o]) for o in oids)


def dominates(a: tuple[float, ...], b: tuple[float, ...], tol: float = 1e-9) -> bool:
    """a dominates b: no worse everywhere, strictly better somewhere."""
    better = False
    for x, y in zip(a, b):
        if x > y + tol:
            return False
        if x < y - tol:
            better = True
    return better


def nondominated(
    items: list[tuple[str, tuple[float, ...]]], tol: float = 1e-9
) -> tuple[list[str], dict[str, list[str]]]:
    """Returns (ids of non-dominated points, ties: kept id -> ids with an identical vector). Deterministic."""
    srt = sorted(items, key=lambda it: (it[1], it[0]))
    front: list[tuple[str, tuple[float, ...]]] = []
    ties: dict[str, list[str]] = {}
    for iid, v in srt:
        dup = next((f for f in front if all(abs(x - y) <= tol for x, y in zip(f[1], v))), None)
        if dup is not None:
            ties.setdefault(dup[0], []).append(iid)
            continue
        if any(dominates(f[1], v, tol) for f in front):
            continue
        front = [f for f in front if not dominates(v, f[1], tol)] + [(iid, v)]
    return [f[0] for f in front], ties


def ranges(vecs: list[tuple[float, ...]]) -> list[tuple[float, float]]:
    if not vecs:
        return []
    return [(min(v[k] for v in vecs), max(v[k] for v in vecs)) for k in range(len(vecs[0]))]


def normalise(v: tuple[float, ...], rg: list[tuple[float, float]]) -> list[float]:
    return [0.0 if hi - lo <= 1e-12 else (x - lo) / (hi - lo) for x, (lo, hi) in zip(v, rg)]


def score(v: tuple[float, ...], w: list[float], rg: list[tuple[float, float]]) -> float:
    n = normalise(v, rg)
    return sum(wi * ni for wi, ni in zip(w, n))


def thin(
    front_ids: list[str], vec_of: dict[str, tuple[float, ...]], rg: list[tuple[float, float]], k: int, keep: set[str]
) -> list[str]:
    """Deterministic farthest-point selection of at most k front members; always keeps ``keep`` and each objective's best point."""
    if len(front_ids) <= k:
        return sorted(front_ids)
    chosen: list[str] = sorted(x for x in keep if x in vec_of)
    for j in range(len(rg)):
        best = min(front_ids, key=lambda i: (vec_of[i][j], i))
        if best not in chosen:
            chosen.append(best)
    nv = {i: normalise(vec_of[i], rg) for i in front_ids}
    while len(chosen) < k:
        far = max(
            (i for i in front_ids if i not in chosen),
            key=lambda i: (min(sum((a - b) ** 2 for a, b in zip(nv[i], nv[c])) for c in chosen), i),
        )
        chosen.append(far)
    return sorted(chosen[: max(k, len(keep))])
