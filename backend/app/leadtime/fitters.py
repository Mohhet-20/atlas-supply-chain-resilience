"""Pure-Python fitters on sparse rows: ridge regression, L2 logistic regression (Newton), Platt scaling. No dependencies.

A sparse row is a list of (column_index, value). Column 0 is the intercept and is never penalised.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from app.forecast.stats import solve

SparseRow = list[tuple[int, float]]


def _xtwx(rows: list[SparseRow], w: list[float] | None, p: int) -> list[list[float]]:
    a = [[0.0] * p for _ in range(p)]
    for k, r in enumerate(rows):
        wk = 1.0 if w is None else w[k]
        for i, vi in r:
            ai = a[i]
            for j, vj in r:
                ai[j] += wk * vi * vj
    return a


def ridge(rows: list[SparseRow], y: list[float], p: int, lam: float) -> list[float] | None:
    a = _xtwx(rows, None, p)
    b = [0.0] * p
    for r, t in zip(rows, y, strict=True):
        for i, v in r:
            b[i] += v * t
    for i in range(1, p):
        a[i][i] += lam
    return solve(a, b)


def dot(beta: list[float], row: SparseRow) -> float:
    return sum(beta[i] * v for i, v in row)


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 700)))
    e = math.exp(max(z, -700))
    return e / (1.0 + e)


def logistic(rows: list[SparseRow], y: list[int], p: int, lam: float, max_iter: int = 40, tol: float = 1e-7, check: Callable[[], None] | None = None) -> tuple[list[float] | None, dict]:  # fmt: skip
    """Newton / IRLS with an L2 penalty on non-intercept coefficients. Returns (beta, info); beta None on numerical failure."""
    beta = [0.0] * p
    info = {"iterations": 0, "converged": False}
    for it in range(1, max_iter + 1):
        if check:
            check()
        eta = [dot(beta, r) for r in rows]
        mu = [sigmoid(z) for z in eta]
        w = [max(m * (1 - m), 1e-9) for m in mu]
        h = _xtwx(rows, w, p)
        g = [0.0] * p
        for r, yi, m in zip(rows, y, mu, strict=True):
            for i, v in r:
                g[i] += (yi - m) * v
        for i in range(1, p):
            g[i] -= lam * beta[i]
            h[i][i] += lam
        step = solve(h, g)
        if step is None:
            return None, {**info, "iterations": it, "failure": "singular Hessian"}
        beta = [b + s for b, s in zip(beta, step, strict=True)]
        info["iterations"] = it
        if max(abs(s) for s in step) < tol:
            info["converged"] = True
            break
    if any(not math.isfinite(b) for b in beta):
        return None, {**info, "failure": "non-finite coefficients"}
    return beta, info


def platt(scores: list[float], y: list[int], max_iter: int = 50) -> tuple[float, float] | None:
    """Fit p = sigmoid(a * logit(score) + b) by Newton. Returns (a, b) or None if not identifiable."""
    if len(set(y)) < 2:
        return None
    eps = 1e-6
    z = [math.log(min(max(s, eps), 1 - eps) / (1 - min(max(s, eps), 1 - eps))) for s in scores]
    a, b = 1.0, 0.0
    for _ in range(max_iter):
        mu = [sigmoid(a * zi + b) for zi in z]
        w = [max(m * (1 - m), 1e-9) for m in mu]
        g0 = sum((yi - m) for yi, m in zip(y, mu, strict=True))
        g1 = sum((yi - m) * zi for yi, m, zi in zip(y, mu, z, strict=True))
        h00, h01, h11 = (
            sum(w),
            sum(wi * zi for wi, zi in zip(w, z, strict=True)),
            sum(wi * zi * zi for wi, zi in zip(w, z, strict=True)),
        )
        det = h00 * h11 - h01 * h01 + 1e-9
        db = (h11 * g0 - h01 * g1) / det
        da = (h00 * g1 - h01 * g0) / det
        a, b = a + da, b + db
        if abs(da) + abs(db) < 1e-8:
            break
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    return a, b


def apply_platt(score: float, ab: tuple[float, float]) -> float:
    eps = 1e-6
    s = min(max(score, eps), 1 - eps)
    return sigmoid(ab[0] * math.log(s / (1 - s)) + ab[1])
