"""Empirical prediction intervals from out-of-sample rolling-origin residuals (split-conformal style quantiles).

Residual = actual − forecast. For level L (α = 1 − L) the lower/upper offsets are the order statistics
``floor((n+1)·α/2)`` and ``ceil((n+1)·(1−α/2))``; if either rank falls outside 1..n there is not enough evidence and
**no bound is produced**. Horizons are grouped into bands so that every band has at least ``min_n`` residuals.
Coverage is evaluated out of sample: each fold is scored with offsets calibrated only on earlier folds.
"""

from __future__ import annotations

import math
from typing import Any


def required_n(level: float, configured: int) -> int:
    return max(configured, math.ceil(round(2 / (1 - level), 9)) - 1)


def offsets(res: list[float], level: float) -> tuple[float, float] | None:
    n = len(res)
    a = 1 - level
    lo, hi = (
        math.floor(round((n + 1) * a / 2, 9)),
        math.ceil(round((n + 1) * (1 - a / 2), 9)),
    )  # round: float noise at exact ranks
    if lo < 1 or hi > n:
        return None
    s = sorted(res)
    return s[lo - 1], s[hi - 1]


def bands(H: int, folds: int, need: int) -> list[list[int]] | None:
    """Consecutive horizon groups, each with ≥ ``need`` residuals (folds × horizons-in-band)."""
    if folds < 1 or folds * H < need:
        return None
    size = math.ceil(need / folds)
    out = [list(range(i, min(i + size, H + 1))) for i in range(1, H + 1, size)]
    if len(out) > 1 and len(out[-1]) * folds < need:
        last = out.pop()
        out[-1] = out[-1] + last
    return out


def calibrate(records: list[tuple[int, int, float, float]], H: int, level: float, min_n: int) -> dict[str, Any]:
    folds = len({r[0] for r in records})
    need = required_n(level, min_n)
    bs = bands(H, folds, need)
    if bs is None:
        return {"supported": False, "level": level, "reason": f"needs {need} residuals per band; have {folds} fold(s) × {H} horizon(s)"}  # fmt: skip
    per_h: dict[int, dict[str, Any]] = {}
    for band in bs:
        res = [a - f for _, h, a, f in records if h in band]
        o = offsets(res, level)
        if o is None:
            return {"supported": False, "level": level, "reason": f"rank outside 1..n with n={len(res)}"}
        for h in band:
            per_h[h] = {"lower_offset": o[0], "upper_offset": o[1], "n": len(res), "band": [band[0], band[-1]]}
    return {"supported": True, "level": level, "method": "empirical rolling-origin residual quantiles (horizon-banded)", "per_horizon": per_h, "min_residuals_per_band": need, "calibration_folds": folds, "horizons_validated": H}  # fmt: skip


def coverage(
    records: list[tuple[int, int, float, float]], H: int, level: float, min_n: int, nonneg: bool
) -> dict[str, Any]:
    folds = sorted({r[0] for r in records})
    hit = tot = 0
    width = 0.0
    for j in folds[1:]:
        prior = [r for r in records if r[0] < j]
        cal = calibrate(prior, H, level, min_n)
        if not cal["supported"]:
            continue
        for _, h, a, f in (r for r in records if r[0] == j):
            o = cal["per_horizon"].get(h)
            if o is None:
                continue
            lo, hi = f + o["lower_offset"], f + o["upper_offset"]
            if nonneg:
                lo, hi = max(0.0, lo), max(0.0, hi)
            hit += lo <= a <= hi
            tot += 1
            width += hi - lo
    if not tot:
        return {"level": level, "status": "insufficient_evidence", "n_scored": 0, "empirical_coverage": None, "mean_width": None,
                "note": "not enough earlier folds to calibrate before scoring"}  # fmt: skip
    return {"level": level, "status": "evaluated", "n_scored": tot, "empirical_coverage": hit / tot, "mean_width": width / tot,
            "note": "coverage from a limited number of out-of-sample points; treat as an estimate, not a guarantee"}  # fmt: skip
