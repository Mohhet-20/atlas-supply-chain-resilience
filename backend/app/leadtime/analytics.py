"""Historical lead-time and delivery-reliability analytics (pure; descriptive statistics, not predictions).

Every metric states its denominator. A delivery is counted in exactly one bucket. Censored (in-transit / open) orders are
counted separately and never as completed deliveries. Lead-time statistics (one definition, one unit) and punctuality
statistics (against one commitment) are separate blocks with separate denominators and are never merged. A missing promised
date is *unknown*, never on time.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from app.forecast.stats import mean, med, quantile, std
from app.leadtime import semantics as sem
from app.leadtime.config import LeadTimeConfig
from app.leadtime.semantics import Calendar, Delivery

PCTS = {"p50": 0.5, "p80": 0.8, "p90": 0.9, "p95": 0.95}


def calendar_of(cfg: LeadTimeConfig) -> Calendar:
    return Calendar(cfg.timezone, tuple(cfg.weekend_days), frozenset(date.fromisoformat(h) for h in cfg.holidays))


def pct_required_n(p: float) -> int:
    return max(10, math.ceil(1 / (1 - p)))


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


@dataclass
class Obs:
    d: Delivery
    state: str
    value: float | None  # lead time in the configured unit (None when the definition has no value for this delivery)
    ref_at: datetime | None  # when the outcome became known (arrival/receipt/dispatch)
    late: bool | None
    deviation: float | None  # signed, in the configured unit
    deviation_hours: float | None


def observe(d: Delivery, cfg: LeadTimeConfig, as_of: datetime, cal: Calendar | None = None) -> tuple[Obs, str | None]:
    """Classify one delivery for the configured definition. Returns (observation, exclusion_reason or None)."""
    state, _reasons = sem.classify(d, as_of, cfg.chronology_tolerance_hours)
    cal = cal or calendar_of(cfg)
    late = sem.is_late(d, cfg.commitment, cfg.grace_hours) if state == sem.COMPLETED else None
    dev_h = sem.deviation_hours(d, cfg.commitment) if state == sem.COMPLETED else None
    dev = None
    c = sem.commitment_of(d, cfg.commitment)
    if state == sem.COMPLETED and c is not None and d.arrived_at is not None:
        dev = sem.elapsed(c, d.arrived_at, cfg.unit, cal)
    value, ref_at = None, d.arrived_at or d.received_at
    if state == sem.COMPLETED and cfg.definition != "PROMISE_DEVIATION":
        s, e, _ = sem.endpoints(d, cfg.definition, cfg.start_event)
        if s is not None and e is not None:
            value, ref_at = sem.elapsed(s, e, cfg.unit, cal), e
    elif state == sem.COMPLETED:
        value = dev
    o = Obs(d, state, value, ref_at, late, dev, dev_h)
    if state == sem.INVALID:
        return o, "INVALID_CHRONOLOGY"
    if state == sem.COMPLETED and d.exception_note and not cfg.include_documented_exceptions:
        return o, "DOCUMENTED_EXCEPTION"
    if state != sem.COMPLETED:
        return o, {sem.CANCELLED: "CANCELLED", sem.PARTIAL: "PARTIAL_RECEIPT", sem.IN_TRANSIT: "CENSORED_IN_TRANSIT", sem.OPEN: "CENSORED_OPEN", sem.UNKNOWN: "UNKNOWN_STATUS"}[state]  # fmt: skip
    return o, None


def partition(deliveries: list[Delivery], cfg: LeadTimeConfig, as_of: datetime) -> dict[str, Any]:
    cal = calendar_of(cfg)
    excluded: Counter[str] = Counter()
    complete: list[Obs] = []
    all_obs: list[Obs] = []
    for d in deliveries:
        o, why = observe(d, cfg, as_of, cal)
        all_obs.append(o)
        if why:
            excluded[why] += 1
        else:
            complete.append(o)
    with_value = [o for o in complete if o.value is not None]
    missing_ts = len(complete) - len(with_value)
    punct = [o for o in complete if o.late is not None]
    return {
        "complete": complete, "with_value": with_value, "punct": punct, "all": all_obs, "excluded": dict(excluded),
        "missing_timestamps": missing_ts, "missing_commitment": len(complete) - len(punct),
    }  # fmt: skip


def _lead_block(vals: list[float]) -> dict[str, Any]:
    n = len(vals)
    pcts: dict[str, float | None] = {}
    unsupported: dict[str, str] = {}
    for k, p in PCTS.items():
        need = pct_required_n(p)
        if n >= need:
            pcts[k] = quantile(vals, p)
        else:
            unsupported[k] = f"needs at least {need} observations; has {n}"
    m = mean(vals)
    s = std(vals)
    return {
        "n": n, "mean": m, "median": med(vals), "std": s, "min": min(vals) if vals else None, "max": max(vals) if vals else None,
        "percentiles": pcts, "percentiles_unsupported": unsupported,
        "coefficient_of_variation": (s / m) if (s is not None and m not in (None, 0) and n >= 8) else None,
        "note_std": None if s is not None else "undefined: fewer than 2 observations",
        "note_max": "The maximum observed lead time is not an upper bound or a guarantee." if n else None,
    }  # fmt: skip


def _punct_block(obs: list[Obs], cfg: LeadTimeConfig, missing: int) -> dict[str, Any]:
    n = len(obs)
    late = [o for o in obs if o.late]
    k = len(late)
    devs = [o.deviation for o in obs if o.deviation is not None]
    delays = [o.deviation for o in late if o.deviation is not None]
    return {
        "commitment": cfg.commitment, "grace_hours": cfg.grace_hours, "n": n, "n_missing_commitment": missing,
        "on_time": n - k, "late": k, "on_time_rate": ((n - k) / n) if n else None, "late_rate": (k / n) if n else None,
        "late_rate_ci95": wilson(k, n), "mean_delay_when_late": mean(delays), "median_deviation": med(devs),
        "mean_absolute_deviation": mean([abs(x) for x in devs]) if devs else None, "deviation_unit": cfg.unit,
        "note": "Deliveries with no promised date are counted in n_missing_commitment, never as on time.",
    }  # fmt: skip


def summarize(
    deliveries: list[Delivery], cfg: LeadTimeConfig, as_of: datetime, min_n: int | None = None
) -> dict[str, Any]:
    p = partition(deliveries, cfg, as_of)
    min_n = min_n or cfg.min_group_n
    cnt = Counter(o.state for o in p["all"])
    vals = [o.value for o in p["with_value"] if o.value is not None]
    arrivals = [o.ref_at for o in p["complete"] if o.ref_at]
    latest = max(arrivals) if arrivals else None
    warnings = []
    if len(p["punct"]) < min_n:
        warnings.append(
            f"Only {len(p['punct'])} deliveries have both an arrival and a promised date (minimum {min_n}); punctuality rates are not reliable."
        )
    if len(vals) < min_n:
        warnings.append(
            f"Only {len(vals)} completed deliveries support this lead-time definition (minimum {min_n}); statistics are descriptive only."
        )
    if p["excluded"].get("CENSORED_IN_TRANSIT", 0) + p["excluded"].get("CENSORED_OPEN", 0):
        warnings.append(
            "In-transit and open orders are censored: they are not completed deliveries, so observed lead times are biased toward faster deliveries."
        )
    return {
        "definition": cfg.definition, "unit": cfg.unit, "start_event": cfg.start_event, "timezone": cfg.timezone,
        "description": _describe(cfg), "as_of": as_of.isoformat(),
        "counts": {"deliveries": len(deliveries), "by_state": dict(cnt), "eligible_completed": len(p["complete"]), "with_lead_time": len(vals),
                   "excluded_by_reason": p["excluded"], "completed_missing_timestamps_for_definition": p["missing_timestamps"],
                   "censored": cnt.get(sem.IN_TRANSIT, 0) + cnt.get(sem.OPEN, 0)},  # fmt: skip
        "lead_time": _lead_block(vals), "punctuality": _punct_block(p["punct"], cfg, p["missing_commitment"]),
        "freshness": {"latest_outcome_at": latest.isoformat() if latest else None, "age_days": ((as_of - latest).total_seconds() / 86400) if latest else None},
        "coverage": {"share_of_deliveries_with_lead_time": (len(vals) / len(deliveries)) if deliveries else None,
                     "share_with_commitment": (len(p["punct"]) / len(p["complete"])) if p["complete"] else None},  # fmt: skip
        "min_sample": min_n, "sufficient": len(vals) >= min_n, "warnings": warnings,
    }  # fmt: skip


def _describe(cfg: LeadTimeConfig) -> str:
    base = {"ORDER_TO_RECEIPT": ("order placement" if cfg.start_event == "ORDER" else "order confirmation") + " to receipt",
            "DISPATCH_TO_ARRIVAL": "dispatch to arrival", "ORDER_TO_DISPATCH": ("order placement" if cfg.start_event == "ORDER" else "order confirmation") + " to dispatch",
            "PROMISE_DEVIATION": f"actual arrival minus the {cfg.commitment.lower()} promised time (positive = late)"}[cfg.definition]  # fmt: skip
    return f"{base}, in {cfg.unit.lower().replace('_', ' ')}"


GROUPS = ("supplier", "destination", "product", "lane", "product_lane", "mode", "month")


def _gkey(o: Obs, by: str) -> Any:
    d = o.d
    return {"supplier": (d.supplier_id,), "destination": (d.destination_id,), "product": (d.product_id,), "lane": (d.supplier_id, d.destination_id),
            "product_lane": (d.supplier_id, d.destination_id, d.product_id), "mode": (d.mode or "UNKNOWN",),
            "month": ((o.ref_at.strftime("%Y-%m") if o.ref_at else "unknown"),)}[by]  # fmt: skip


def group_summary(
    deliveries: list[Delivery], cfg: LeadTimeConfig, as_of: datetime, by: str, min_n: int | None = None
) -> dict[str, Any]:
    if by not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")
    min_n = min_n or cfg.min_group_n
    p = partition(deliveries, cfg, as_of)
    buckets: dict[Any, dict[str, list[Obs]]] = defaultdict(lambda: {"lt": [], "pu": [], "all": []})
    for o in p["all"]:
        buckets[_gkey(o, by)]["all"].append(o)
    for o in p["with_value"]:
        buckets[_gkey(o, by)]["lt"].append(o)
    for o in p["punct"]:
        buckets[_gkey(o, by)]["pu"].append(o)
    overall_late = (sum(1 for o in p["punct"] if o.late) / len(p["punct"])) if p["punct"] else None
    rows = []
    for k, b in buckets.items():
        vals = [o.value for o in b["lt"] if o.value is not None]
        n_p = len(b["pu"])
        k_late = sum(1 for o in b["pu"] if o.late)
        delays = [o.deviation for o in b["pu"] if o.late and o.deviation is not None]
        lt = _lead_block(vals) if vals else None
        rows.append({
            "key": list(k), "deliveries": len(b["all"]), "censored": sum(1 for o in b["all"] if o.state in sem.CENSORED), "n_lead_time": len(vals),
            "median_lead_time": lt["median"] if lt else None, "mean_lead_time": lt["mean"] if lt else None, "std_lead_time": lt["std"] if lt else None,
            "p90_lead_time": (lt["percentiles"].get("p90") if lt else None), "n_punctuality": n_p, "late": k_late,
            "late_rate": (k_late / n_p) if n_p else None, "late_rate_ci95": wilson(k_late, n_p), "on_time_rate": ((n_p - k_late) / n_p) if n_p else None,
            "mean_delay_when_late": mean(delays), "sufficient_sample": n_p >= min_n, "difference_from_overall": None,
        })  # fmt: skip
        r = rows[-1]
        ci = r["late_rate_ci95"]
        if r["sufficient_sample"] and overall_late is not None and ci is not None:
            r["difference_from_overall"] = {
                "late_rate_minus_overall": r["late_rate"] - overall_late,
                "ci_excludes_overall": not (ci[0] <= overall_late <= ci[1]),
            }
    rows.sort(key=lambda r: (not r["sufficient_sample"], -(r["late_rate"] or 0), str(r["key"])))
    return {
        "group_by": by, "definition": cfg.definition, "unit": cfg.unit, "commitment": cfg.commitment, "min_sample": min_n,
        "overall": {"late_rate": overall_late, "n_punctuality": len(p["punct"])}, "groups": rows,
        "ranking_note": f"Groups with fewer than {min_n} deliveries carrying both an arrival and a promised date are listed after the others and marked insufficient_sample; they are not ranked.",
    }  # fmt: skip


def recent_vs_history(
    deliveries: list[Delivery], cfg: LeadTimeConfig, as_of: datetime, window_days: int = 90, min_n: int | None = None
) -> dict[str, Any]:
    min_n = min_n or cfg.min_group_n
    p = partition(deliveries, cfg, as_of)
    cut = as_of - timedelta(days=window_days)
    recent = [o for o in p["punct"] if o.ref_at and o.ref_at >= cut]
    prior = [o for o in p["punct"] if o.ref_at and o.ref_at < cut]

    def blk(xs: list[Obs]) -> dict[str, Any]:
        k = sum(1 for o in xs if o.late)
        vals = [o.value for o in xs if o.value is not None]
        return {
            "n": len(xs),
            "late": k,
            "late_rate": (k / len(xs)) if xs else None,
            "late_rate_ci95": wilson(k, len(xs)),
            "median_lead_time": med(vals),
        }

    r, h = blk(recent), blk(prior)
    out: dict[str, Any] = {
        "window_days": window_days,
        "recent": r,
        "history": h,
        "min_sample": min_n,
        "status": "ok",
        "distinguishable": None,
    }
    if r["n"] < min_n or h["n"] < min_n:
        out["status"] = "insufficient_evidence"
        out["note"] = (
            f"Both periods need at least {min_n} deliveries with an arrival and a promised date (recent {r['n']}, history {h['n']}); no change is reported."
        )
        return out
    a, b = r["late_rate_ci95"], h["late_rate_ci95"]
    out["late_rate_change"] = r["late_rate"] - h["late_rate"]
    out["distinguishable"] = not (a[0] <= b[1] and b[0] <= a[1])
    out["note"] = "The two 95% Wilson intervals do not overlap, so the change is distinguishable from noise at this sample size." if out["distinguishable"] else "The intervals overlap: no distinguishable change in the late-delivery rate."  # fmt: skip
    return out
