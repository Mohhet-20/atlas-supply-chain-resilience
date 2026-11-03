"""One metric library for every task, with explicit definitions and edge cases.

Every value is ``{"value", "n", "status", "note"}`` where ``status`` is ``ok``, ``undefined`` (mathematically undefined for these
data) or ``insufficient_data``. A missing or invalid value is never replaced by 0 or NaN. Errors are ``prediction - actual``:
positive bias means over-prediction. Classification metrics reuse ``leadtime.metrics`` (verified independently in tests).
"""

from __future__ import annotations

import math
from typing import Any

from app.forecast.stats import med
from app.leadtime import metrics as _lm
from app.leadtime.analytics import wilson

LOG_LOSS_EPS = 1e-15

DEFINITIONS: dict[str, dict[str, str]] = {
    "mae": {
        "task": "regression",
        "definition": "mean(|p − a|)",
        "aggregation": "mean over evaluated pairs",
        "direction": "lower is better",
    },
    "rmse": {
        "task": "regression",
        "definition": "sqrt(mean((p − a)²))",
        "aggregation": "root of mean squared error",
        "direction": "lower is better",
    },
    "median_ae": {
        "task": "regression",
        "definition": "median(|p − a|)",
        "aggregation": "median",
        "direction": "lower is better",
    },
    "bias": {
        "task": "regression",
        "definition": "mean(p − a); positive = over-prediction",
        "aggregation": "mean signed error",
        "direction": "closer to 0 is better",
    },
    "wape": {
        "task": "regression",
        "definition": "sum|p − a| / sum|a|",
        "aggregation": "ratio of sums",
        "direction": "lower is better",
    },
    "mase": {
        "task": "regression",
        "definition": "MAE / scale, where scale is a caller-supplied naive in-sample MAE",
        "aggregation": "ratio",
        "direction": "lower is better; < 1 beats the naive scale",
    },
    "precision": {
        "task": "classification",
        "definition": "TP / (TP + FP)",
        "aggregation": "at the stated threshold",
        "direction": "higher is better",
    },
    "recall": {
        "task": "classification",
        "definition": "TP / (TP + FN)",
        "aggregation": "at the stated threshold",
        "direction": "higher is better",
    },
    "f1": {
        "task": "classification",
        "definition": "2PR / (P + R)",
        "aggregation": "at the stated threshold",
        "direction": "higher is better",
    },
    "roc_auc": {
        "task": "classification",
        "definition": "P(score of a random positive > score of a random negative), ties count half",
        "aggregation": "rank statistic",
        "direction": "higher is better; 0.5 = chance",
    },
    "pr_auc": {
        "task": "classification",
        "definition": "average precision (step-wise area under precision-recall)",
        "aggregation": "rank statistic",
        "direction": "higher is better; compare with prevalence",
    },
    "brier": {
        "task": "classification",
        "definition": "mean((p − y)²); valid only for probabilities",
        "aggregation": "mean",
        "direction": "lower is better",
    },
    "log_loss": {
        "task": "classification",
        "definition": f"−mean(y·ln p + (1−y)·ln(1−p)), p clipped to [{LOG_LOSS_EPS}, 1−{LOG_LOSS_EPS}]",
        "aggregation": "mean; valid only for probabilities",
        "direction": "lower is better",
    },
    "ece": {
        "task": "calibration",
        "definition": "sum over equal-frequency bins of (n_bin / n) · |mean predicted − observed rate|",
        "aggregation": "weighted over bins",
        "direction": "lower is better",
    },
    "coverage": {
        "task": "uncertainty",
        "definition": "share of actuals inside [lower, upper]",
        "aggregation": "proportion with Wilson 95% interval",
        "direction": "close to the nominal level is better",
    },
    "width": {
        "task": "uncertainty",
        "definition": "mean(upper − lower)",
        "aggregation": "mean",
        "direction": "narrower is better at equal coverage",
    },
}


def m(value: float | None, n: int, note: str | None = None, *, insufficient: bool = False) -> dict[str, Any]:
    if value is not None and not math.isfinite(value):
        value, note = None, note or "undefined: non-finite result"
    status = "ok" if value is not None else ("insufficient_data" if insufficient else "undefined")
    return {"value": value, "n": n, "status": status, "note": note}


def clean_pairs(pred: list[Any], actual: list[Any]) -> tuple[list[float], list[float], dict[str, int]]:
    """Drop pairs with a missing or non-finite side; the exclusions are counted, never hidden."""
    if len(pred) != len(actual):
        raise ValueError("predictions and actuals must have the same length")
    p_out: list[float] = []
    a_out: list[float] = []
    for p, a in zip(pred, actual, strict=True):
        if p is None or a is None or isinstance(p, bool) or isinstance(a, bool):
            continue
        try:
            fp, fa = float(p), float(a)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fp) and math.isfinite(fa):
            p_out.append(fp)
            a_out.append(fa)
    return (
        p_out,
        a_out,
        {"input": len(pred), "used": len(p_out), "excluded_missing_or_non_finite": len(pred) - len(p_out)},
    )


def regression(pred: list[Any], actual: list[Any], *, scale: float | None = None, min_n: int = 1) -> dict[str, Any]:
    p, a, cnt = clean_pairs(pred, actual)
    n = len(p)
    if n < max(1, min_n):
        why = "no usable evaluation pairs" if n == 0 else f"needs at least {min_n} pairs; has {n}"
        return {
            "counts": cnt,
            **{k: m(None, n, why, insufficient=True) for k in ("mae", "rmse", "median_ae", "bias", "wape", "mase")},
        }
    e = [x - y for x, y in zip(p, a, strict=True)]
    sa = sum(abs(y) for y in a)
    mae = sum(abs(x) for x in e) / n
    return {
        "counts": cnt,
        "mae": m(mae, n),
        "rmse": m(math.sqrt(sum(x * x for x in e) / n), n),
        "median_ae": m(med([abs(x) for x in e]), n),
        "bias": m(sum(e) / n, n, "prediction − actual; positive = over-prediction"),
        "wape": m(sum(abs(x) for x in e) / sa, n) if sa > 1e-12 else m(None, n, "undefined: actuals sum to zero"),
        "mase": m(mae / scale, n) if (scale is not None and scale > 1e-12) else m(None, n, "undefined: no valid positive scaling baseline was supplied"),
    }  # fmt: skip


def interval_coverage(
    lower: list[Any], upper: list[Any], actual: list[Any], level: float | None = None
) -> dict[str, Any]:
    rows = [(float(lo), float(hi), float(a)) for lo, hi, a in zip(lower, upper, actual, strict=True) if None not in (lo, hi, a) and lo <= hi]  # fmt: skip
    n = len(rows)
    if n == 0:
        return {"n": 0, "status": "insufficient_data", "coverage": m(None, 0, "no usable intervals", insufficient=True), "width": m(None, 0, "no usable intervals", insufficient=True), "nominal_level": level}  # fmt: skip
    k = sum(1 for lo, hi, a in rows if lo <= a <= hi)
    return {
        "n": n, "status": "ok", "nominal_level": level, "inside": k, "coverage": m(k / n, n), "coverage_ci95": wilson(k, n),
        "width": m(sum(hi - lo for lo, hi, _ in rows) / n, n),
        "note": "Coverage is an empirical estimate from this sample; a prediction interval is not the probability that an individual outcome falls inside it.",
    }  # fmt: skip


def _prf_class(c: dict[str, int], positive: bool) -> dict[str, Any]:
    tp, fp, fn, tn = (c["tp"], c["fp"], c["fn"], c["tn"]) if positive else (c["tn"], c["fn"], c["fp"], c["tp"])
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if (p is not None and r is not None and p + r > 0) else None
    return {"support": tp + fn, "precision": m(p, tp + fp, None if p is not None else "undefined: nothing predicted in this class"), "recall": m(r, tp + fn, None if r is not None else "undefined: no actual members of this class"), "f1": m(f, tp + fp + fn, None if f is not None else "undefined: precision or recall is undefined or zero")}  # fmt: skip


def classification(
    score: list[Any], y: list[Any], threshold: float, *, probabilistic: bool, train_prevalence: float | None = None
) -> dict[str, Any]:
    """Binary classification. ``probabilistic`` must be True only for calibrated probabilities (or documented probability estimates)."""
    pairs = [
        (float(s), int(t))
        for s, t in zip(score, y, strict=True)
        if s is not None and t in (0, 1) and math.isfinite(float(s))
    ]
    n = len(pairs)
    cnt = {"input": len(score), "used": n, "excluded_missing_or_invalid": len(score) - n}
    if n == 0:
        return {"counts": cnt, "n": 0, "status": "insufficient_data", "note": "no usable evaluation examples"}
    s = [a for a, _ in pairs]
    t = [b for _, b in pairs]
    pos = sum(t)
    conf = _lm.confusion(s, t, threshold)
    prev = pos / n
    out: dict[str, Any] = {
        "counts": cnt, "n": n, "status": "ok", "positives": pos, "negatives": n - pos, "prevalence": prev, "threshold": threshold,
        "confusion": conf, "per_class": {"positive": _prf_class(conf, True), "negative": _prf_class(conf, False)},
        "accuracy": m((conf["tp"] + conf["tn"]) / n, n),
    }  # fmt: skip
    single = pos in (0, n)
    out["single_class"] = single
    out["roc_auc"] = (
        m(None, n, "undefined: the evaluation set contains only one class") if single else m(_lm.roc_auc(s, t), n)
    )
    out["pr_auc"] = m(None, n, "undefined: there are no positive examples") if pos == 0 else m(_lm.pr_auc(s, t), n, "compare with prevalence, which is the no-skill level")  # fmt: skip
    out["majority_class_baseline"] = {"predicts": 1 if prev >= 0.5 else 0, "accuracy": max(prev, 1 - prev)}
    if probabilistic:
        inside = all(0.0 <= x <= 1.0 for x in s)
        if not inside:
            out["brier"] = m(None, n, "undefined: values are outside [0, 1], so they are not probabilities")
            out["log_loss"] = m(None, n, "undefined: values are outside [0, 1], so they are not probabilities")
        else:
            out["brier"] = m(_lm.brier(s, t), n)
            ll = (
                -sum(
                    math.log(min(max(x, LOG_LOSS_EPS), 1 - LOG_LOSS_EPS))
                    if y_ == 1
                    else math.log(1 - min(max(x, LOG_LOSS_EPS), 1 - LOG_LOSS_EPS))
                    for x, y_ in pairs
                )
                / n
            )
            out["log_loss"] = m(ll, n)
            base = train_prevalence if train_prevalence is not None else None
            if base is not None:
                out["brier_baseline_constant_prevalence"] = m(
                    _lm.brier([base] * n, t), n, f"constant prediction {base:.4f} (training prevalence)"
                )
    else:
        out["brier"] = m(None, n, "not reported: the output is a score, not a probability")
        out["log_loss"] = m(None, n, "not reported: the output is a score, not a probability")
    return out


def reliability(
    prob: list[float], y: list[int], *, bins: int = 10, min_n: int = 100, min_per_bin: int = 10
) -> dict[str, Any]:
    """Equal-frequency reliability bins; ECE only with enough data. The method is stated in the result."""
    n = len(y)
    method = f"equal-frequency bins (up to {bins}), ECE = sum(n_bin/n · |mean predicted − observed rate|); requires n ≥ {min_n} and ≥ {min_per_bin} per bin"
    if n < min_n:
        return {"status": "insufficient_data", "n": n, "method": method, "bins": [], "ece": m(None, n, f"needs at least {min_n} examples; has {n}", insufficient=True)}  # fmt: skip
    k = min(bins, n // min_per_bin)
    if k < 2:
        return {
            "status": "insufficient_data",
            "n": n,
            "method": method,
            "bins": [],
            "ece": m(None, n, "too few examples per bin", insufficient=True),
        }
    r = _lm.reliability(prob, y, max_bins=k, min_per_bin=min_per_bin)
    return {"status": r["status"], "n": n, "method": method, "bins": r["bins"], "ece": m(r["ece"], n)}


def segment_metrics(keys: list[Any], pred: list[Any], actual: list[Any], *, min_n: int) -> dict[str, Any]:
    """Regression metrics per segment. Segments below ``min_n`` are suppressed and pooled into a count only."""
    groups: dict[Any, list[int]] = {}
    for i, k in enumerate(keys):
        groups.setdefault(k, []).append(i)
    shown, suppressed_n, suppressed_groups = [], 0, 0
    for k, idx in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        if len(idx) < min_n:
            suppressed_groups += 1
            suppressed_n += len(idx)
            continue
        r = regression([pred[i] for i in idx], [actual[i] for i in idx])
        shown.append({"segment": k, "n": len(idx), "mae": r["mae"], "bias": r["bias"], "rmse": r["rmse"]})
    return {"min_segment_n": min_n, "segments": shown, "suppressed": {"segments": suppressed_groups, "records": suppressed_n, "reason": f"fewer than {min_n} records; not shown to avoid conclusions from tiny samples and to limit exposure of small groups"}}  # fmt: skip
