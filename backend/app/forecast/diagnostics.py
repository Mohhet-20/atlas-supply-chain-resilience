"""Series diagnostics and demand-pattern classification. Every statistic states when it is undefined."""

from __future__ import annotations

import math
from typing import Any

from app.forecast.prep import Prepared
from app.forecast.stats import acf, mean, med, ols_trend, std

MIN_CV_N = 8
MIN_TREND_N = 12
CRITERIA = {
    "insufficient_history": "fewer than 2 seasonal cycles or fewer than 14 periods of usable history",
    "poor_data_quality": "missing share > 20%, imputed share > 20%, or stale by more than 3 periods",
    "sparse": "observed coverage of the lookback span < 60%",
    "intermittent": "zero proportion >= 0.30 with at least 3 non-zero periods (ADI = periods per non-zero demand)",
    "trending": "OLS slope t-statistic magnitude > 2.5 and total drift over the window > 20% of the mean",
    "seasonal": "ACF at the seasonal lag > 2/sqrt(n) and > 0.3 with at least two full cycles",
    "volatile": "coefficient of variation > 0.5 on a non-intermittent series",
    "stable": "none of the above and CV <= 0.3",
}


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def diagnose(p: Prepared, season: int | None) -> dict[str, Any]:
    y = p.values
    n = len(y)
    out: dict[str, Any] = {"n": n, "frequency": p.frequency, "unit": p.unit}
    if not n:
        return {**out, "status": "no_data"}
    m, s = mean(y), std(y)
    nz = [v for v in y if v > 0]
    zero_prop = 1 - len(nz) / n
    out.update(
        mean=_r(m), median=_r(med(y)), std=_r(s), min=_r(min(y)), max=_r(max(y)),
        cv=_r(s / m) if (s is not None and m and m > 0 and n >= MIN_CV_N) else None,
        cv_note=None if (m and m > 0 and n >= MIN_CV_N) else "undefined: mean is zero or fewer than 8 periods",
        zero_proportion=_r(zero_prop, 4),
        missing_proportion=_r(p.counts.get("missing_periods", 0) / n, 4),
        imputed_proportion=_r(len(p.imputations) / n, 4),
        observed_periods=sum(p.observed),
        coverage=_r(sum(p.observed) / n, 4),
    )  # fmt: skip
    # trend
    tr = ols_trend(y) if n >= MIN_TREND_N else None
    drift = (tr["slope"] * (n - 1) / m) if (tr and m and m > 0) else None
    out["trend"] = {
        "slope_per_period": _r(tr["slope"]) if tr else None, "t_stat": _r(tr["t"], 3) if tr else None,
        "relative_drift": _r(drift, 4), "r2": _r(tr["r2"], 4) if tr else None,
        "note": None if tr else "undefined: fewer than 12 periods",
    }  # fmt: skip
    # seasonality / ACF
    maxlag = min(max(14, (season or 0) + 1), n // 2)
    out["acf"] = [{"lag": k, "value": _r(acf(y, k), 4)} for k in range(1, maxlag + 1)]
    seas = None
    if season and n >= 2 * season:
        a = acf(y, season)
        seas = {
            "period": season,
            "acf": _r(a, 4),
            "threshold": _r(max(0.3, 2 / math.sqrt(n)), 4),
            "cycles": n // season,
        }
    out["seasonality"] = seas or {
        "period": season,
        "acf": None,
        "note": "undefined: fewer than two full cycles or no period",
    }
    # recent vs historical
    k = max(4, n // 4)
    if n >= 2 * k:
        rec, his = mean(y[-k:]), mean(y[:-k])
        out["recent_vs_historical"] = {
            "recent_mean": _r(rec), "historical_mean": _r(his), "window": k,
            "relative_change": _r((rec - his) / his, 4) if (his and his > 0) else None,
        }  # fmt: skip
    else:
        out["recent_vs_historical"] = {"relative_change": None, "note": "undefined: too few periods"}
    # intermittency (Syntetos–Boylan style)
    if len(nz) >= 3:
        adi = n / len(nz)
        mz = mean(nz) or 0
        cv2 = (std(nz) or 0) ** 2 / mz**2 if mz > 0 else None
        out["intermittency"] = {"adi": _r(adi, 4), "cv2_nonzero": _r(cv2, 4), "nonzero_periods": len(nz)}
    else:
        out["intermittency"] = {"adi": None, "cv2_nonzero": None, "nonzero_periods": len(nz), "note": "undefined: fewer than 3 non-zero periods"}  # fmt: skip
    out["outliers"] = {"count": len(p.outliers), "proportion": _r(len(p.outliers) / n, 4)}
    out["freshness"] = {"periods_behind_origin": p.freshness_gap_periods, "last_period": p.dates[-1].isoformat()}
    return out


def classify(diag: dict[str, Any], limits_min: int = 14) -> dict[str, Any]:
    """Multi-label demand pattern with the evidence for each label."""
    labels: list[str] = []
    ev: dict[str, Any] = {}
    n = diag.get("n", 0)
    season = (diag.get("seasonality") or {}).get("period")
    short = n < limits_min or (season is not None and n < 2 * season and n < 28)
    if short:
        labels.append("insufficient_history")
        ev["insufficient_history"] = {"n": n, "minimum": limits_min}
    if not n:
        return {"labels": labels or ["insufficient_history"], "evidence": ev, "criteria": CRITERIA}
    miss, imp = diag.get("missing_proportion") or 0, diag.get("imputed_proportion") or 0
    stale = (diag.get("freshness") or {}).get("periods_behind_origin") or 0
    if miss > 0.2 or imp > 0.2 or stale > 3:
        labels.append("poor_data_quality")
        ev["poor_data_quality"] = {"missing": miss, "imputed": imp, "stale_periods": stale}
    if (diag.get("coverage") or 0) < 0.6:
        labels.append("sparse")
        ev["sparse"] = {"coverage": diag.get("coverage")}
    inter = diag.get("intermittency") or {}
    intermittent = (diag.get("zero_proportion") or 0) >= 0.30 and inter.get("nonzero_periods", 0) >= 3
    if intermittent:
        adi, cv2 = inter.get("adi"), inter.get("cv2_nonzero")
        sub = ("intermittent" if (cv2 is not None and cv2 < 0.49) else "lumpy") if adi and adi >= 1.32 else "erratic"
        labels.append("intermittent")
        ev["intermittent"] = {
            "zero_proportion": diag.get("zero_proportion"),
            "adi": adi,
            "cv2": cv2,
            "syntetos_boylan_class": sub,
        }
    tr = diag.get("trend") or {}
    if tr.get("t_stat") is not None and tr.get("relative_drift") is not None and abs(tr["t_stat"]) > 2.5 and abs(tr["relative_drift"]) > 0.2:  # fmt: skip
        labels.append("trending")
        ev["trending"] = {"t_stat": tr["t_stat"], "relative_drift": tr["relative_drift"], "direction": "up" if tr["relative_drift"] > 0 else "down"}  # fmt: skip
    sea = diag.get("seasonality") or {}
    if sea.get("acf") is not None and sea["acf"] > sea["threshold"] and sea["acf"] > 0.3:
        labels.append("seasonal")
        ev["seasonal"] = {"period": sea["period"], "acf": sea["acf"], "threshold": sea["threshold"]}
    cv = diag.get("cv")
    if cv is not None and cv > 0.5 and not intermittent:
        labels.append("volatile")
        ev["volatile"] = {"cv": cv}
    if not labels and cv is not None and cv <= 0.3:
        labels.append("stable")
        ev["stable"] = {"cv": cv}
    if not labels:
        labels.append("unclassified")
        ev["unclassified"] = {"note": "no criterion met with enough evidence"}
    return {"labels": labels, "evidence": ev, "criteria": CRITERIA}
