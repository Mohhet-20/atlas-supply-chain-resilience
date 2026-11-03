"""Data preparation: resolve → window → validate → dedupe/conflict → aggregate → missing vs zero → impute → outliers.

The order is fixed and every step is recorded. Absent periods are *missing*, not zero, unless the source contract says
so (``missing_means_zero``). Every imputation and every outlier decision is listed in the result.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.forecast.config import DEMAND_KINDS, PERIOD_DAYS, ForecastConfig, ForecastLimits
from app.forecast.stats import med, quantile


def norm_unit(unit: str | None) -> str | None:
    u = (unit or "").strip().lower()
    return u or None


@dataclass(frozen=True)
class RawObs:
    id: int
    kind: str
    start: date
    end: date  # exclusive
    quantity: float
    unit: str
    row_hash: str = ""


@dataclass
class Prepared:
    status: str = "ok"
    frequency: str = "daily"
    unit: str | None = None
    basis: str | None = None
    origin: date | None = None
    dates: list[date] = field(default_factory=list)  # bucket start dates
    values: list[float] = field(default_factory=list)  # cleaned values, same length as dates
    observed: list[bool] = field(default_factory=list)  # True = backed by a real observation (not imputed)
    raw: list[float | None] = field(default_factory=list)
    imputations: list[dict[str, Any]] = field(default_factory=list)
    outliers: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    observation_ids: list[int] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    snapshot_hash: str = ""
    freshness_gap_periods: int | None = None

    @property
    def usable(self) -> bool:
        return self.status == "ok"

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status, "frequency": self.frequency, "unit": self.unit, "basis": self.basis,
            "origin": self.origin.isoformat() if self.origin else None, "periods": len(self.values),
            "first_period": self.dates[0].isoformat() if self.dates else None,
            "last_period": self.dates[-1].isoformat() if self.dates else None,
            "counts": self.counts, "imputations": self.imputations, "outliers": self.outliers,
            "warnings": self.warnings, "steps": self.steps, "snapshot_hash": self.snapshot_hash,
            "freshness_gap_periods": self.freshness_gap_periods,
        }  # fmt: skip


def bucket_start(d: date, freq: str) -> date:
    return d if freq == "daily" else d - timedelta(days=d.weekday())


def detect_outliers(
    values: list[float], idx: list[int], *, method: str, threshold: float, period: int | None
) -> tuple[list[dict], str | None]:
    """Robust outlier detection on deviations from a seasonal-position (or global) median. Detection only; no removal."""
    if len(values) < 8:
        return [], "too few observations for outlier detection"
    groups: dict[int, list[float]] = {}
    use_season = bool(period) and len(values) >= 2 * period
    key = (lambda i: i % period) if use_season else (lambda i: 0)  # type: ignore[misc]
    for v, i in zip(values, idx, strict=True):
        groups.setdefault(key(i), []).append(v)
    if use_season and min(len(g) for g in groups.values()) < 4:
        use_season = False
        groups = {0: list(values)}
        key = lambda i: 0  # noqa: E731
    centre = {g: med(v) for g, v in groups.items()}
    dev = [v - centre[key(i)] for v, i in zip(values, idx, strict=True)]  # type: ignore[operator]
    out: list[dict] = []
    if method == "mad":
        mad = med([abs(d) for d in dev])
        if not mad:
            return [], "MAD is zero (majority of values equal their median); detection not applied"
        for d, v, i in zip(dev, values, idx, strict=True):
            z = 0.6745 * d / mad
            if abs(z) > threshold:
                out.append({"index": i, "value": v, "score": round(z, 4), "method": "mad"})
        return out, None
    q1, q3 = quantile(dev, 0.25), quantile(dev, 0.75)
    iqr = (q3 or 0) - (q1 or 0)
    if iqr <= 0:
        return [], "IQR is zero; detection not applied"
    lo, hi = q1 - threshold * 0.4286 * iqr, q3 + threshold * 0.4286 * iqr  # threshold 3.5 ≈ 1.5 IQR fence
    for d, v, i in zip(dev, values, idx, strict=True):
        if d < lo or d > hi:
            out.append({"index": i, "value": v, "score": round(d / iqr, 4), "method": "iqr"})
    return out, None


def prepare(obs: list[RawObs], cfg: ForecastConfig, limits: ForecastLimits | None = None) -> Prepared:  # noqa: C901
    limits = limits or ForecastLimits()
    freq = cfg.frequency
    pd_ = PERIOD_DAYS[freq]
    res = Prepared(frequency=freq, counts={})
    c = res.counts
    c.update(received=len(obs), outside_window=0, partial_window=0, invalid=0, unit_mismatch=0, other_kind=0, adjustments_ignored=0, used=0)  # fmt: skip

    def step(name: str, **kw: Any) -> None:
        res.steps.append({"step": name, **kw})

    step("resolve", frequency=freq, basis_requested=cfg.basis)
    # 1. resolve demand kind (one kind per series; never mixed), ignoring adjustments.
    base_pool = [o for o in obs if o.kind in DEMAND_KINDS]
    c["adjustments_ignored"] = sum(1 for o in obs if o.kind == "ADJUSTMENT")
    kinds = [k for k in DEMAND_KINDS if any(o.kind == k for o in base_pool)]
    if cfg.basis != "AUTO":
        kinds = [k for k in kinds if k == cfg.basis]
    if not kinds:
        res.status = "no_observations"
        res.warnings.append("No demand observations of a supported kind exist for this series.")
        return res
    res.basis = kinds[0]
    c["other_kind"] = sum(1 for o in base_pool if o.kind != res.basis)
    pool = [o for o in base_pool if o.kind == res.basis]
    if len(pool) > limits.max_observations:
        res.status = "too_many_observations"
        res.warnings.append(f"More than {limits.max_observations} observations; narrow the lookback.")
        return res

    # 2. validate quantities / periods
    valid: list[RawObs] = []
    for o in pool:
        if o.end <= o.start or not math.isfinite(o.quantity) or o.quantity < 0:
            c["invalid"] += 1
        else:
            valid.append(o)
    # 3. unit
    units: dict[str, int] = {}
    for o in valid:
        units[norm_unit(o.unit) or ""] = units.get(norm_unit(o.unit) or "", 0) + 1
    if cfg.unit:
        want = norm_unit(cfg.unit)
    else:
        want = sorted(units.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if units else None
    keep = [o for o in valid if (norm_unit(o.unit) or "") == (want or "")]
    c["unit_mismatch"] = len(valid) - len(keep)
    res.unit = next((o.unit for o in keep), want)
    if not keep:
        res.status = "incompatible_units"
        res.warnings.append("No observation uses the requested unit; nothing is converted.")
        return res
    if c["unit_mismatch"]:
        res.warnings.append(
            f"{c['unit_mismatch']} observation(s) in other units were excluded (no conversion applied)."
        )
    step("validate", invalid=c["invalid"], unit=res.unit, unit_mismatch=c["unit_mismatch"])

    # 4. origin + window
    latest_end = max(o.end for o in keep)
    origin = cfg.origin or bucket_start(latest_end, freq)
    if bucket_start(origin, freq) != origin:
        res.warnings.append(f"origin {origin.isoformat()} is not on a {freq} boundary and was aligned down.")
        origin = bucket_start(origin, freq)
    if cfg.origin is None and bucket_start(latest_end, freq) != latest_end:
        res.warnings.append("The last observed period ends mid-bucket; the incomplete trailing bucket is not used.")
    res.origin = origin
    w0 = origin - timedelta(days=cfg.lookback_periods * pd_)
    inwin: list[RawObs] = []
    for o in keep:
        if o.end <= w0 or o.start >= origin:
            c["outside_window"] += 1
        elif o.start < w0 or o.end > origin:
            c["partial_window"] += 1
        else:
            inwin.append(o)
    if c["partial_window"]:
        res.warnings.append(f"{c['partial_window']} observation(s) straddling the window or origin were excluded.")
    step("window", start=w0.isoformat(), origin=origin.isoformat(), excluded_outside=c["outside_window"], excluded_partial=c["partial_window"])  # fmt: skip
    if not inwin:
        res.status = "no_observations"
        res.warnings.append("No observations fall inside the lookback window before the origin.")
        return res

    # 5. dedupe / conflict
    inwin.sort(key=lambda o: (o.start, o.end, o.id))
    uniq: list[RawObs] = []
    dups = 0
    for o in inwin:
        if uniq and (o.start, o.end, o.quantity) == (uniq[-1].start, uniq[-1].end, uniq[-1].quantity):
            dups += 1
            continue
        uniq.append(o)
    c["duplicates_dropped"] = dups
    for a, b in zip(uniq, uniq[1:], strict=False):
        if b.start < a.end:
            res.status = "conflicting_observations"
            res.warnings.append("Demand periods overlap with different content, so quantities would be double counted; no series is built.")  # fmt: skip
            return res
    c["used"] = len(uniq)
    res.observation_ids = [o.id for o in uniq]
    step("deduplicate", duplicates_dropped=dups, kept=len(uniq))

    # 6. aggregate onto the bucket grid by pro-rata daily allocation; weekly buckets need all 7 days covered.
    daily: dict[date, float] = {}
    spread = 0
    for o in uniq:
        n = (o.end - o.start).days
        if n > 1:
            spread += 1
        for k in range(n):
            daily[o.start + timedelta(days=k)] = o.quantity / n
    if spread:
        res.warnings.append(
            f"{spread} multi-day observation(s) were allocated evenly across their days (pro-rata assumption)."
        )
    first = bucket_start(min(daily), freq)
    if first < min(daily) and freq == "weekly":
        first += timedelta(days=7)  # the first bucket would be only partly covered
    n_b = (origin - first).days // pd_
    # the series ends at the last bucket that contains observed data
    last_obs = max(bucket_start(d, freq) for d in daily)
    last_idx = min(n_b - 1, (last_obs - first).days // pd_)
    if last_idx < 0:
        res.status = "insufficient_data"
        res.warnings.append("No complete bucket of observed demand before the origin.")
        return res
    dates = [first + timedelta(days=pd_ * i) for i in range(last_idx + 1)]
    raw: list[float | None] = []
    for b in dates:
        days = [b + timedelta(days=k) for k in range(pd_)]
        if all(d in daily for d in days):
            raw.append(sum(daily[d] for d in days))
        else:
            raw.append(None)
    gap = (origin - (dates[-1] + timedelta(days=pd_))).days // pd_
    res.freshness_gap_periods = max(0, gap)
    step("aggregate", periods=len(dates), method="pro-rata daily allocation" if freq == "daily" else "sum of 7 fully covered days")  # fmt: skip
    if gap > cfg.max_stale_periods:
        res.status = "stale_data"
        res.dates, res.raw = dates, raw
        res.warnings.append(
            f"The latest observation is {gap} {freq} periods before the origin (limit {cfg.max_stale_periods})."
        )
        return res
    if gap > 0:
        res.warnings.append(f"The latest observed period is {gap} {freq} period(s) before the origin.")

    # 7. missing vs zero
    miss = [i for i, v in enumerate(raw) if v is None]
    c["periods"] = len(dates)
    c["missing_periods"] = len(miss)
    res.dates, res.raw = dates, raw
    res.observed = [v is not None for v in raw]
    step("missing", missing=len(miss), policy=cfg.missing_policy)
    if len(dates) < limits.min_history_periods:
        res.status = "insufficient_data"
        res.warnings.append(f"{len(dates)} periods of history; at least {limits.min_history_periods} are needed.")
        return res
    values: list[float] = [v if v is not None else math.nan for v in raw]
    if miss:
        frac = len(miss) / len(dates)
        if cfg.missing_policy == "reject":
            res.status = "missing_periods"
            res.warnings.append(f"{len(miss)} missing period(s) and missing_policy is 'reject'.")
            return res
        if cfg.missing_policy == "zero_fill_by_contract":
            for i in miss:
                values[i] = 0.0
                res.imputations.append(
                    {"index": i, "period": dates[i].isoformat(), "method": "zero_fill_by_contract", "value": 0.0}
                )
        else:
            if frac > cfg.max_missing_fraction:
                res.status = "too_many_missing"
                res.warnings.append(f"{frac:.0%} of periods are missing (limit {cfg.max_missing_fraction:.0%}); no values are invented.")  # fmt: skip
                return res
            runs, i = [], 0
            while i < len(miss):
                j = i
                while j + 1 < len(miss) and miss[j + 1] == miss[j] + 1:
                    j += 1
                runs.append((miss[i], miss[j]))
                i = j + 1
            if any(b - a + 1 > cfg.max_gap_periods for a, b in runs):
                res.status = "too_many_missing"
                res.warnings.append(f"A gap longer than {cfg.max_gap_periods} periods exists; no values are invented.")
                return res
            for a, b in runs:
                lo = raw[a - 1] if a > 0 else None
                hi = raw[b + 1] if b + 1 < len(raw) else None
                for i in range(a, b + 1):
                    if cfg.missing_policy == "interpolate_linear" and lo is not None and hi is not None:
                        v, m = lo + (hi - lo) * (i - a + 1) / (b - a + 2), "interpolate_linear"
                    elif lo is not None:
                        v, m = lo, "carry_forward"
                    else:
                        res.status = "too_many_missing"
                        res.warnings.append("A gap at the start of the series cannot be imputed.")
                        return res
                    values[i] = v
                    res.imputations.append({"index": i, "period": dates[i].isoformat(), "method": m, "value": v})
            res.warnings.append(f"{len(miss)} missing period(s) were imputed ({cfg.missing_policy}); imputed points are not scored in backtests.")  # fmt: skip
    step("impute", count=len(res.imputations))

    # 8. outliers: detect (always), then handle per policy
    obs_idx = [i for i in range(len(values)) if res.observed[i]]
    found, why = detect_outliers([values[i] for i in obs_idx], obs_idx, method=cfg.outlier_method, threshold=cfg.outlier_threshold, period=cfg.season())  # fmt: skip
    res.outliers = found
    c["outliers_detected"] = len(found)
    handled = 0
    if found and cfg.outlier_handling in ("transform", "exclude"):
        base = [values[i] for i in obs_idx]
        m_ = med(base) or 0.0
        mad = med([abs(v - m_) for v in base]) or 0.0
        cap = m_ + cfg.outlier_threshold / 0.6745 * mad
        for o in found:
            i = o["index"]
            if cfg.outlier_handling == "transform":
                new = min(values[i], cap) if values[i] > m_ else max(values[i], max(0.0, m_ - (cap - m_)))
                o.update(
                    action="transform", before=values[i], after=new, rule="winsorize at median ± threshold·MAD/0.6745"
                )
            else:
                prev = values[i - 1] if i > 0 else None
                nxt = values[i + 1] if i + 1 < len(values) else None
                new = (
                    (prev + nxt) / 2
                    if prev is not None and nxt is not None
                    else (prev if prev is not None else values[i])
                )
                o.update(action="exclude", before=values[i], after=new, rule="excluded then replaced by neighbour mean")
                res.observed[i] = False  # no longer scored as a real observation
            values[i] = new
            handled += 1
    else:
        for o in found:
            o["action"] = "flag" if cfg.outlier_handling == "flag" else "preserve"
    if found:
        res.warnings.append(
            f"{len(found)} outlier(s) detected; handling={cfg.outlier_handling} ({handled} value(s) changed)."
        )
    elif why:
        res.warnings.append(f"Outlier detection: {why}.")
    c["outliers_changed"] = handled
    step("outliers", detected=len(found), handling=cfg.outlier_handling, note=why)

    res.values = values
    payload = {
        "obs": [(o.id, o.row_hash, o.start.isoformat(), o.end.isoformat(), o.quantity) for o in uniq],
        "unit": res.unit, "freq": freq, "origin": origin.isoformat(),
        "values": [round(v, 9) for v in values],
    }  # fmt: skip
    res.snapshot_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    if len(set(values)) == 1 and values[0] == 0:
        res.warnings.append("Every period is zero; a forecast of zero carries no information beyond the history.")
    return res
