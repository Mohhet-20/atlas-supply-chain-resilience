"""Small, dependency-free numeric helpers (stdlib only). Undefined results are ``None``, never a made-up number."""

from __future__ import annotations

import math
from statistics import median


def mean(x: list[float]) -> float | None:
    return sum(x) / len(x) if x else None


def std(x: list[float], ddof: int = 1) -> float | None:
    if len(x) <= ddof:
        return None
    m = sum(x) / len(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - ddof))


def med(x: list[float]) -> float | None:
    return float(median(x)) if x else None


def quantile(x: list[float], q: float) -> float | None:
    """Linear-interpolation quantile (type 7)."""
    if not x:
        return None
    s = sorted(x)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def acf(x: list[float], lag: int) -> float | None:
    n = len(x)
    if lag < 1 or lag >= n or n < 3:
        return None
    m = sum(x) / n
    den = sum((v - m) ** 2 for v in x)
    if den <= 1e-12:
        return None
    return sum((x[i] - m) * (x[i + lag] - m) for i in range(n - lag)) / den


def ols_trend(y: list[float]) -> dict | None:
    """OLS of y on 0..n-1. Returns slope, intercept and the slope's t statistic (None when undefined)."""
    n = len(y)
    if n < 4:
        return None
    xm = (n - 1) / 2
    ym = sum(y) / n
    sxx = sum((i - xm) ** 2 for i in range(n))
    sxy = sum((i - xm) * (y[i] - ym) for i in range(n))
    slope = sxy / sxx
    icpt = ym - slope * xm
    sse = sum((y[i] - icpt - slope * i) ** 2 for i in range(n))
    if sse <= 1e-12:
        return {"slope": slope, "intercept": icpt, "t": None, "r2": 1.0 if sxx else None}
    se = math.sqrt(sse / (n - 2) / sxx)
    sst = sum((v - ym) ** 2 for v in y)
    return {
        "slope": slope,
        "intercept": icpt,
        "t": slope / se if se > 0 else None,
        "r2": 1 - sse / sst if sst > 0 else None,
    }


def solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. ``None`` when the system is numerically singular."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-10:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][j] * x[j] for j in range(i + 1, n))) / m[i][i]
    return x


def ridge_fit(rows: list[list[float]], y: list[float], lam: float = 1e-6) -> list[float] | None:
    """Least squares with a tiny ridge on non-intercept columns (column 0 is the intercept)."""
    k = len(rows[0])
    xtx = [[sum(r[i] * r[j] for r in rows) for j in range(k)] for i in range(k)]
    xty = [sum(r[i] * v for r, v in zip(rows, y, strict=True)) for i in range(k)]
    for i in range(1, k):
        xtx[i][i] += lam * max(1.0, xtx[i][i])
    return solve(xtx, xty)
