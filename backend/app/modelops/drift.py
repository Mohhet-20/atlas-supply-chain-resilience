"""Distribution-change diagnostics with documented thresholds. Drift is a signal, never proof of deterioration.

PSI (population stability index) bins the reference sample at its own quantiles; thresholds 0.10 (warning) and 0.25 (alert) are
conventional rules of thumb, not statistical tests. Each function returns ``insufficient_data`` below the minimum sample.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

PSI_WARN, PSI_ALERT = 0.10, 0.25
MIN_N = 30
EPS = 1e-4


def _status(value: float, warn: float, alert: float) -> tuple[str, str]:
    if value >= alert:
        return "alert", "warning"
    if value >= warn:
        return "warning", "warning"
    return "ok", "info"


def psi_numeric(ref: list[float], cur: list[float], bins: int = 10, min_n: int = MIN_N) -> dict[str, Any]:
    ref = [x for x in ref if x is not None and math.isfinite(x)]
    cur = [x for x in cur if x is not None and math.isfinite(x)]
    base = {"method": f"PSI over {bins} reference-quantile bins (heuristic thresholds {PSI_WARN}/{PSI_ALERT})", "n_reference": len(ref), "n_comparison": len(cur)}  # fmt: skip
    if len(ref) < min_n or len(cur) < min_n:
        return {
            **base,
            "status": "insufficient_data",
            "severity": "info",
            "value": None,
            "note": f"needs at least {min_n} values in each period",
        }
    s = sorted(ref)
    cuts = sorted({s[min(len(s) - 1, (i * len(s)) // bins)] for i in range(1, bins)})
    if not cuts:
        same = all(x == s[0] for x in cur)
        return {**base, "status": "ok" if same else "alert", "severity": "info" if same else "warning", "value": 0.0 if same else None, "note": "the reference is constant" + ("" if same else "; the comparison period contains other values")}  # fmt: skip

    def share(xs: list[float]) -> list[float]:
        c = [0] * (len(cuts) + 1)
        for x in xs:
            c[sum(1 for q in cuts if x > q)] += 1
        return [max(v / len(xs), EPS) for v in c]

    a, b = share(ref), share(cur)
    v = sum((y - x) * math.log(y / x) for x, y in zip(a, b, strict=True))
    st, sev = _status(v, PSI_WARN, PSI_ALERT)
    return {**base, "status": st, "severity": sev, "value": v, "note": None}


def psi_categorical(ref: list[Any], cur: list[Any], min_n: int = MIN_N) -> dict[str, Any]:
    ref = [x for x in ref if x is not None]
    cur = [x for x in cur if x is not None]
    base = {
        "method": f"PSI over category shares (heuristic thresholds {PSI_WARN}/{PSI_ALERT})",
        "n_reference": len(ref),
        "n_comparison": len(cur),
    }
    if len(ref) < min_n or len(cur) < min_n:
        return {
            **base,
            "status": "insufficient_data",
            "severity": "info",
            "value": None,
            "note": f"needs at least {min_n} values in each period",
        }
    cr, cc = Counter(ref), Counter(cur)
    cats = set(cr) | set(cc)
    unseen = sorted(str(c) for c in cc if c not in cr)
    v = sum(
        (max(cc[c] / len(cur), EPS) - max(cr[c] / len(ref), EPS))
        * math.log(max(cc[c] / len(cur), EPS) / max(cr[c] / len(ref), EPS))
        for c in cats
    )
    st, sev = _status(v, PSI_WARN, PSI_ALERT)
    note = (
        f"{len(unseen)} category value(s) not seen in the reference period: {', '.join(unseen[:5])}" if unseen else None
    )
    if unseen and st == "ok":
        st, sev = "warning", "warning"
    return {**base, "status": st, "severity": sev, "value": v, "note": note, "unseen_categories": unseen[:20]}


def missingness_change(
    ref_missing: int,
    ref_n: int,
    cur_missing: int,
    cur_n: int,
    min_n: int = MIN_N,
    warn: float = 0.05,
    alert: float = 0.15,
) -> dict[str, Any]:
    base = {
        "method": f"absolute change in missing share (warning ≥ {warn}, alert ≥ {alert})",
        "n_reference": ref_n,
        "n_comparison": cur_n,
    }
    if ref_n < min_n or cur_n < min_n:
        return {
            **base,
            "status": "insufficient_data",
            "severity": "info",
            "value": None,
            "note": f"needs at least {min_n} records in each period",
        }
    a, b = ref_missing / ref_n, cur_missing / cur_n
    st, sev = _status(abs(b - a), warn, alert)
    return {
        **base,
        "status": st,
        "severity": sev,
        "value": b - a,
        "reference_share": a,
        "comparison_share": b,
        "note": None,
    }


def ks_statistic(ref: list[float], cur: list[float], min_n: int = MIN_N) -> dict[str, Any]:
    """Two-sample Kolmogorov–Smirnov distance with the large-sample 5% critical value (a screening statistic)."""
    ref = sorted(x for x in ref if x is not None and math.isfinite(x))
    cur = sorted(x for x in cur if x is not None and math.isfinite(x))
    n, k = len(ref), len(cur)
    base = {
        "method": "two-sample KS distance; 5% large-sample critical value 1.358·sqrt((n+m)/(n·m))",
        "n_reference": n,
        "n_comparison": k,
    }
    if n < min_n or k < min_n:
        return {
            **base,
            "status": "insufficient_data",
            "severity": "info",
            "value": None,
            "note": f"needs at least {min_n} values in each period",
        }
    i = j = 0
    d = 0.0
    while i < n and j < k:
        x = min(ref[i], cur[j])
        while i < n and ref[i] <= x:
            i += 1
        while j < k and cur[j] <= x:
            j += 1
        d = max(d, abs(i / n - j / k))
    crit = 1.358 * math.sqrt((n + k) / (n * k))
    return {
        **base,
        "status": "warning" if d > crit else "ok",
        "severity": "warning" if d > crit else "info",
        "value": d,
        "critical_value": crit,
        "note": None,
    }
