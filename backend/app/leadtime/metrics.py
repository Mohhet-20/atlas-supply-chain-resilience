"""Regression and classification metrics with explicit edge cases. Undefined values are ``None`` with a reason, never 0.

Regression error e = prediction - actual (positive bias = the model over-estimates the lead time).
"""

from __future__ import annotations

import math
from typing import Any

from app.forecast.stats import med, quantile


def _m(v: float | None, n: int, note: str | None = None) -> dict[str, Any]:
    return {"value": v, "n": n, "note": note}


def regression(pred: list[float], actual: list[float]) -> dict[str, Any]:
    n = len(pred)
    if n == 0:
        why = "no evaluation examples"
        return {"n": 0, **{k: _m(None, 0, why) for k in ("mae", "rmse", "median_ae", "bias")}}
    e = [p - a for p, a in zip(pred, actual, strict=True)]
    mean_a = sum(actual) / n
    return {
        "n": n, "mae": _m(sum(abs(x) for x in e) / n, n), "rmse": _m(math.sqrt(sum(x * x for x in e) / n), n),
        "median_ae": _m(med([abs(x) for x in e]), n), "bias": _m(sum(e) / n, n, "prediction - actual; positive = over-estimates"),
        "relative_bias": _m(sum(e) / n / mean_a if mean_a else None, n, None if mean_a else "undefined: mean actual is zero"),
    }  # fmt: skip


def by_range(pred: list[float], actual: list[float], bins: int = 3, min_n: int = 10) -> list[dict[str, Any]]:
    n = len(actual)
    if n < bins * min_n:
        return []
    cuts = [quantile(actual, i / bins) for i in range(1, bins)]
    out = []
    for b in range(bins):
        lo = cuts[b - 1] if b else None
        hi = cuts[b] if b < bins - 1 else None
        idx = [i for i, a in enumerate(actual) if (lo is None or a > lo) and (hi is None or a <= hi)]
        if len(idx) >= min_n:
            r = regression([pred[i] for i in idx], [actual[i] for i in idx])
            out.append({"range": [lo, hi], "n": len(idx), "mae": r["mae"]["value"], "bias": r["bias"]["value"]})
    return out


def confusion(score: list[float], y: list[int], thr: float) -> dict[str, int]:
    tp = sum(1 for s, t in zip(score, y, strict=True) if s >= thr and t == 1)
    fp = sum(1 for s, t in zip(score, y, strict=True) if s >= thr and t == 0)
    fn = sum(1 for s, t in zip(score, y, strict=True) if s < thr and t == 1)
    tn = sum(1 for s, t in zip(score, y, strict=True) if s < thr and t == 0)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn}


def prf(c: dict[str, int]) -> dict[str, Any]:
    tp, fp, fn = c["tp"], c["fp"], c["fn"]
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = (2 * p * r / (p + r)) if (p is not None and r is not None and p + r > 0) else None
    return {"precision": p, "recall": r, "f1": f,
            "note": None if p is not None and r is not None else "undefined: no predicted positives or no actual positives"}  # fmt: skip


def roc_auc(score: list[float], y: list[int]) -> float | None:
    pos = [s for s, t in zip(score, y, strict=True) if t == 1]
    neg = [s for s, t in zip(score, y, strict=True) if t == 0]
    if not pos or not neg:
        return None
    order = sorted(range(len(score)), key=lambda i: score[i])
    ranks = [0.0] * len(score)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and score[order[j + 1]] == score[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    sr = sum(r for r, t in zip(ranks, y, strict=True) if t == 1)
    return (sr - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def pr_auc(score: list[float], y: list[int]) -> float | None:
    """Average precision (step-wise area under the precision-recall curve). None without positives."""
    npos = sum(y)
    if npos == 0:
        return None
    order = sorted(range(len(score)), key=lambda i: -score[i])
    tp = fp = 0
    ap, prev_r = 0.0, 0.0
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and score[order[j + 1]] == score[order[i]]:
            j += 1
        for k in range(i, j + 1):
            if y[order[k]] == 1:
                tp += 1
            else:
                fp += 1
        r = tp / npos
        ap += (r - prev_r) * (tp / (tp + fp))
        prev_r = r
        i = j + 1
    return ap


def brier(prob: list[float], y: list[int]) -> float | None:
    return sum((p - t) ** 2 for p, t in zip(prob, y, strict=True)) / len(y) if y else None


def reliability(prob: list[float], y: list[int], max_bins: int = 10, min_per_bin: int = 20) -> dict[str, Any]:
    """Equal-frequency reliability bins and expected calibration error. Needs enough data for >= 2 bins."""
    n = len(y)
    k = min(max_bins, n // min_per_bin)
    if k < 2:
        return {
            "status": "insufficient",
            "note": f"needs at least {2 * min_per_bin} examples for a reliability diagram; has {n}",
            "bins": [],
            "ece": None,
        }
    order = sorted(range(n), key=lambda i: prob[i])
    bins, ece = [], 0.0
    for b in range(k):
        idx = order[b * n // k : (b + 1) * n // k]
        mp = sum(prob[i] for i in idx) / len(idx)
        fo = sum(y[i] for i in idx) / len(idx)
        ece += len(idx) / n * abs(mp - fo)
        bins.append({"n": len(idx), "mean_predicted": mp, "observed_rate": fo})
    return {"status": "ok", "bins": bins, "ece": ece, "note": None}


def classification(score: list[float], y: list[int], thr: float, train_prevalence: float | None) -> dict[str, Any]:
    n = len(y)
    pos = sum(y)
    c = confusion(score, y, thr)
    out: dict[str, Any] = {
        "n": n, "positives": pos, "negatives": n - pos, "prevalence": pos / n if n else None, "threshold": thr, "confusion": c, **prf(c),
        "roc_auc": roc_auc(score, y), "pr_auc": pr_auc(score, y), "brier_raw": brier(score, y),
        "brier_baseline_train_prevalence": brier([train_prevalence] * n, y) if (train_prevalence is not None and n) else None,
    }  # fmt: skip
    if out["roc_auc"] is None and n:
        out["roc_auc_note"] = "undefined: the evaluation set contains only one class"
    return out
