"""Forecast accuracy metrics. Each states its formula; an undefined result is ``None`` with a note, never a fabricated number.

Errors are ``forecast − actual``: positive bias = over-forecast. ``records`` are ``(fold, horizon, actual, forecast)``.
"""

from __future__ import annotations

import math
from typing import Any

FORMULAS = {
    "mae": ("mean(|f − a|)", "undefined when there are no scored pairs"),
    "rmse": ("sqrt(mean((f − a)²))", "undefined when there are no scored pairs"),
    "wape": ("sum|f − a| / sum|a|", "undefined when the actuals sum to zero (all-zero demand)"),
    "mase": ("MAE / MAE of the one-step naive forecast on the training window (averaged per fold)", "undefined when a fold's naive scale is zero or there are no folds"),  # fmt: skip
    "bias": ("mean(f − a); positive = over-forecast", "undefined when there are no scored pairs"),
    "smape": ("mean(2|f − a| / (|a| + |f|)), range 0–2", "pairs with a = f = 0 are excluded and counted; undefined if none remain"),
}  # fmt: skip


def _m(v: float | None, n: int, note: str | None = None) -> dict[str, Any]:
    return {"value": None if v is None or not math.isfinite(v) else v, "n": n, "note": note}


def metric_set(records: list[tuple[int, int, float, float]], fold_scales: dict[int, float | None]) -> dict[str, Any]:
    n = len(records)
    if not n:
        return {k: _m(None, 0, "no scored pairs") for k in FORMULAS}
    err = [f - a for _, _, a, f in records]
    sa = sum(abs(a) for _, _, a, _ in records)
    out: dict[str, Any] = {
        "mae": _m(sum(abs(e) for e in err) / n, n),
        "rmse": _m(math.sqrt(sum(e * e for e in err) / n), n),
        "wape": _m(sum(abs(e) for e in err) / sa, n) if sa > 1e-12 else _m(None, n, "undefined: actuals sum to zero"),
        "bias": _m(sum(err) / n, n),
    }  # fmt: skip
    pairs = [(a, f) for _, _, a, f in records if abs(a) + abs(f) > 1e-12]
    out["smape"] = _m(sum(2 * abs(f - a) / (abs(a) + abs(f)) for a, f in pairs) / len(pairs), len(pairs), f"{n - len(pairs)} pair(s) with a=f=0 excluded" if len(pairs) < n else None) if pairs else _m(None, 0, "undefined: all pairs are a=f=0")  # fmt: skip
    # MASE: per-fold MAE / per-fold scale, averaged over folds with a defined scale
    by_fold: dict[int, list[float]] = {}
    for fo, _, a, f in records:
        by_fold.setdefault(fo, []).append(abs(f - a))
    vals = [sum(v) / len(v) / fold_scales[fo] for fo, v in by_fold.items() if fold_scales.get(fo)]  # type: ignore[operator]
    out["mase"] = _m(sum(vals) / len(vals), len(vals), None if len(vals) == len(by_fold) else f"{len(by_fold) - len(vals)} fold(s) skipped: zero naive scale") if vals else _m(None, 0, "undefined: naive scale is zero")  # fmt: skip
    return out


def split(records: list[tuple[int, int, float, float]], fold_scales: dict[int, float | None]) -> dict[str, Any]:
    folds = sorted({r[0] for r in records})
    hs = sorted({r[1] for r in records})
    return {
        "overall": metric_set(records, fold_scales),
        "per_fold": {str(k): metric_set([r for r in records if r[0] == k], fold_scales) for k in folds},
        "per_horizon": {str(h): metric_set([r for r in records if r[1] == h], fold_scales) for h in hs},
    }  # fmt: skip
