"""Route evaluation: aggregate metrics, three-state constraint checks, objective scoring and explanation.

Aggregation rules (also in docs/models.md):
* duration  = sum of lane transit hours + transfer_hours x intermediate facilities; unknown if any lane has no duration.
* cost      = sum of lane charges, only when every lane has a cost and all share one currency (no FX is ever assumed).
* reliability = product of lane on-time probabilities (independence assumed); unknown if any lane has none.
* risk      = maximum known supplier-risk score over the route's facilities, with the covered share reported.
A constraint is SATISFIED, VIOLATED or UNVERIFIABLE; a route is infeasible only on VIOLATED. Missing data is never read as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.routing.config import CRITERIA, DIRECTION, STATEMENTS, Constraints, RouteRequest
from app.routing.network import Lane

QUALITY_ORDER = {"VERIFIED": 0, "ESTIMATED": 1, "UNKNOWN": 2}
SATISFIED, VIOLATED, UNVERIFIABLE = "SATISFIED", "VIOLATED", "UNVERIFIABLE"


@dataclass
class Rel:
    value: float | None
    source: str  # MEASURED | PRIOR | UNKNOWN
    n: int | None = None
    interval: tuple[float, float] | None = None
    note: str | None = None


@dataclass
class Context:
    rel: dict[str, Rel] = field(default_factory=dict)
    risk: dict[int, float | None] = field(default_factory=dict)
    now: datetime | None = None
    transfer_hours: float = 0.0


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


def _worst(qs: list[str]) -> str:
    return max(qs, key=lambda q: QUALITY_ORDER.get(q, 2)) if qs else "UNKNOWN"


def lane_view(ln: Lane, ctx: Context | None = None) -> dict[str, Any]:
    rel = ctx.rel.get(ln.uid) if ctx else None
    return {
        "lane_uid": ln.uid, "origin_facility_id": ln.origin, "destination_facility_id": ln.dest, "mode": ln.mode, "distance_km": ln.distance_km,
        "duration_hours": ln.duration_hours, "duration_quality": ln.duration_quality, "cost": ln.cost, "currency": ln.currency,
        "cost_quality": ln.cost_quality, "capacity": ln.capacity, "capacity_unit": ln.capacity_unit, "provenance": ln.provenance,
        "observed_at": ln.observed_at.isoformat() if ln.observed_at else None, "flags": list(ln.flags), "degraded": list(ln.degraded),
        "reliability": None if rel is None else {"value": _r(rel.value), "source": rel.source, "n": rel.n, "interval": None if rel.interval is None else [_r(rel.interval[0]), _r(rel.interval[1])], "note": rel.note},
    }  # fmt: skip


def metrics(path: list[Lane], ctx: Context) -> dict[str, Any]:
    nodes = [path[0].origin, *[ln.dest for ln in path]]
    durs = [ln.duration_hours for ln in path]
    transit = None if any(d is None for d in durs) else float(sum(d for d in durs if d is not None))
    transfer = ctx.transfer_hours * (len(path) - 1)
    known_dur = float(sum(d for d in durs if d is not None))
    duration = {
        "transit_hours": _r(transit), "transfer_hours": _r(transfer), "total_hours": _r(None if transit is None else transit + transfer),
        "known_hours": _r(known_dur + transfer), "status": "COMPLETE" if transit is not None else "INCOMPLETE",
        "quality": _worst([ln.duration_quality for ln in path]), "missing_lanes": [ln.uid for ln in path if ln.duration_hours is None],
        "scope": "travel time between registered facilities; not door-to-door",
    }  # fmt: skip
    by_ccy: dict[str, float] = {}
    for ln in path:
        if ln.cost is not None and ln.currency:
            by_ccy[ln.currency] = by_ccy.get(ln.currency, 0.0) + ln.cost
    miss_cost = [ln.uid for ln in path if ln.cost is None]
    if miss_cost:
        c_status, c_total, c_ccy = "INCOMPLETE", None, None
    elif len(by_ccy) > 1:
        c_status, c_total, c_ccy = "MIXED_CURRENCY", None, None
    else:
        c_status, (c_ccy, c_total) = "COMPLETE", next(iter(by_ccy.items()))
    cost = {
        "total": _r(c_total, 4), "currency": c_ccy, "status": c_status, "by_currency": {k: _r(v, 4) for k, v in sorted(by_ccy.items())},
        "missing_lanes": miss_cost, "quality": _worst([ln.cost_quality for ln in path]), "scope": "transport charges on listed lanes only; not landed cost",
    }  # fmt: skip
    rels = [ctx.rel.get(ln.uid) for ln in path]
    rvals = [r.value if r is not None else None for r in rels]
    rel_known = [v for v in rvals if v is not None]
    rel_value = None if len(rel_known) != len(path) else math.prod(rel_known)
    reliability = {
        "value": _r(rel_value), "upper_bound": _r(math.prod(rel_known) if rel_known else None), "status": "COMPLETE" if rel_value is not None else "INCOMPLETE",
        "missing_lanes": [ln.uid for ln, v in zip(path, rvals, strict=True) if v is None],
        "sources": sorted({r.source for r in rels if r is not None}), "assumption": "lane outcomes treated as independent",
    }  # fmt: skip
    rk = [(n, ctx.risk.get(n)) for n in nodes]
    known = [v for _n, v in rk if v is not None]
    risk = {
        "value": _r(max(known) if known else None), "coverage": _r(len(known) / len(nodes)),
        "facilities_without_score": [n for n, v in rk if v is None], "basis": "max current supplier-risk score of owner suppliers, scaled 0-1",
    }  # fmt: skip
    caps = [(ln.capacity, ln.capacity_unit) for ln in path]
    units = {u for c, u in caps if c is not None}
    if any(c is None for c, _u in caps):
        cap = {"bottleneck": None, "unit": None, "status": "INCOMPLETE", "known_min": _r(min([c for c, _u in caps if c is not None], default=None)), "known_units": sorted(units)}  # fmt: skip
    elif len(units) > 1:
        cap = {
            "bottleneck": None,
            "unit": None,
            "status": "MIXED_UNIT",
            "known_min": None,
            "known_units": sorted(units),
        }
    else:
        cap = {"bottleneck": _r(min(c for c, _u in caps if c is not None)), "unit": next(iter(units)), "status": "COMPLETE", "known_min": None, "known_units": sorted(units)}  # fmt: skip
    obs = [ln.observed_at for ln in path if ln.observed_at is not None]
    fresh = {
        "oldest_observed_at": min(obs).isoformat() if obs else None,
        "undated_lanes": [ln.uid for ln in path if ln.observed_at is None],
    }
    prov = sorted({ln.provenance for ln in path})
    if "SYNTHETIC_DEMO" in prov:
        level = "SYNTHETIC"
    elif all(ln.duration_quality == "VERIFIED" and ln.cost_quality == "VERIFIED" for ln in path) and not {
        "MODEL_ESTIMATE",
        "SIMULATED",
    } & set(prov):
        level = "VERIFIED_FIGURES"
    else:
        level = "ESTIMATED_FIGURES"
    return {
        "lane_uids": [ln.uid for ln in path], "facility_ids": nodes, "hops": len(path), "transfers": len(path) - 1, "modes": [ln.mode for ln in path],
        "mode_changes": sum(1 for a, b in zip(path, path[1:], strict=False) if a.mode != b.mode), "distance_km": _r(None if any(ln.distance_km is None for ln in path) else sum(ln.distance_km for ln in path if ln.distance_km is not None), 3),
        "duration": duration, "cost": cost, "reliability": reliability, "risk": risk, "capacity": cap, "freshness": fresh,
        "provenance": prov, "evidence_level": level, "data_flags": sorted({f for ln in path for f in ln.flags}),
        "disruption_notes": [n for ln in path for n in ln.degraded],
    }  # fmt: skip


def check(path: list[Lane], m: dict[str, Any], c: Constraints, ctx: Context) -> list[dict[str, Any]]:
    """Evaluate every active hard constraint on a complete route."""
    out: list[dict[str, Any]] = []

    def add(name: str, state: str, limit: Any, observed: Any, detail: str) -> None:
        out.append({"constraint": name, "state": state, "limit": limit, "observed": observed, "detail": detail})

    d, cost, rel, risk, cap = m["duration"], m["cost"], m["reliability"], m["risk"], m["capacity"]
    lim = c.transit_limit_hours()
    if lim is not None:
        if d["total_hours"] is not None:
            ok = d["total_hours"] <= lim + 1e-9
            add(
                "max_transit_hours",
                SATISFIED if ok else VIOLATED,
                lim,
                d["total_hours"],
                "Total transit plus transfer time against the limit.",
            )
        elif d["known_hours"] > lim + 1e-9:
            add(
                "max_transit_hours",
                VIOLATED,
                lim,
                d["known_hours"],
                "Known lane times alone already exceed the limit (some lanes have no duration).",
            )
        else:
            add(
                "max_transit_hours",
                UNVERIFIABLE,
                lim,
                None,
                f"Lanes without a duration: {', '.join(d['missing_lanes'])}.",
            )
    if c.max_cost is not None:
        sub = cost["by_currency"].get(c.currency)
        if sub is not None and sub > c.max_cost + 1e-9:
            add("max_cost", VIOLATED, c.max_cost, sub, f"Known {c.currency} charges already exceed the budget.")
        elif cost["status"] == "COMPLETE" and cost["currency"] == c.currency:
            add(
                "max_cost",
                SATISFIED,
                c.max_cost,
                cost["total"],
                "Transport charges on the listed lanes against the budget.",
            )
        else:
            why = {"INCOMPLETE": f"lanes without a cost: {', '.join(cost['missing_lanes'])}", "MIXED_CURRENCY": "lanes are priced in several currencies"}.get(cost["status"], f"route is priced in {cost['currency']}, budget is in {c.currency} (no exchange rate is assumed)")  # fmt: skip
            add("max_cost", UNVERIFIABLE, c.max_cost, cost["total"], f"Cost cannot be verified: {why}.")
    if c.min_capacity is not None:
        if cap["status"] == "COMPLETE" and cap["unit"] == c.capacity_unit:
            add("min_capacity", SATISFIED if cap["bottleneck"] >= c.min_capacity - 1e-9 else VIOLATED, c.min_capacity, cap["bottleneck"], f"Smallest lane capacity ({cap['unit']}).")  # fmt: skip
        elif any(
            ln.capacity is not None and ln.capacity_unit == c.capacity_unit and ln.capacity < c.min_capacity - 1e-9
            for ln in path
        ):
            add(
                "min_capacity",
                VIOLATED,
                c.min_capacity,
                cap["known_min"],
                "A lane with a known capacity is below the required size.",
            )
        else:
            add("min_capacity", UNVERIFIABLE, c.min_capacity, cap["bottleneck"], f"Capacity is {cap['status'].lower().replace('_', ' ')} or in a different unit than {c.capacity_unit}.")  # fmt: skip
    modes = set(m["modes"])
    if c.allowed_modes is not None:
        bad = sorted(modes - set(c.allowed_modes))
        add(
            "allowed_modes",
            VIOLATED if bad else SATISFIED,
            sorted(c.allowed_modes),
            sorted(modes),
            f"Modes not allowed: {', '.join(bad)}." if bad else "All modes allowed.",
        )
    if c.required_modes:
        miss = sorted(set(c.required_modes) - modes)
        add(
            "required_modes",
            VIOLATED if miss else SATISFIED,
            sorted(c.required_modes),
            sorted(modes),
            f"Required modes missing: {', '.join(miss)}." if miss else "All required modes are used.",
        )
    if c.prohibited_modes:
        bad = sorted(modes & set(c.prohibited_modes))
        add(
            "prohibited_modes",
            VIOLATED if bad else SATISFIED,
            sorted(c.prohibited_modes),
            sorted(modes),
            f"Prohibited modes used: {', '.join(bad)}." if bad else "No prohibited mode is used.",
        )
    if c.max_transfers is not None:
        add(
            "max_transfers",
            SATISFIED if m["transfers"] <= c.max_transfers else VIOLATED,
            c.max_transfers,
            m["transfers"],
            "Intermediate facilities on the route.",
        )
    if c.excluded_lanes:
        bad = sorted(set(m["lane_uids"]) & set(c.excluded_lanes))
        add(
            "excluded_lanes",
            VIOLATED if bad else SATISFIED,
            sorted(c.excluded_lanes),
            m["lane_uids"],
            f"Uses excluded lanes: {', '.join(bad)}." if bad else "No excluded lane is used.",
        )
    if c.excluded_facilities:
        bad = sorted(set(m["facility_ids"]) & set(c.excluded_facilities))
        add(
            "excluded_facilities",
            VIOLATED if bad else SATISFIED,
            sorted(c.excluded_facilities),
            m["facility_ids"],
            f"Passes through excluded facilities: {bad}." if bad else "No excluded facility is used.",
        )
    if c.required_facilities:
        miss = sorted(set(c.required_facilities) - set(m["facility_ids"]))
        add(
            "required_facilities",
            VIOLATED if miss else SATISFIED,
            sorted(c.required_facilities),
            m["facility_ids"],
            f"Required facilities not on the route: {miss}." if miss else "All required facilities are on the route.",
        )
    if c.min_reliability is not None:
        if rel["value"] is not None:
            add(
                "min_reliability",
                SATISFIED if rel["value"] >= c.min_reliability - 1e-9 else VIOLATED,
                c.min_reliability,
                rel["value"],
                "Product of lane on-time probabilities.",
            )
        elif rel["upper_bound"] is not None and rel["upper_bound"] < c.min_reliability - 1e-9:
            add(
                "min_reliability",
                VIOLATED,
                c.min_reliability,
                rel["upper_bound"],
                "Even with perfect reliability on the lanes without a value, the route would fall short.",
            )
        else:
            add(
                "min_reliability",
                UNVERIFIABLE,
                c.min_reliability,
                None,
                f"Lanes without a reliability value: {', '.join(rel['missing_lanes'])}.",
            )
    if c.max_risk is not None:
        if risk["value"] is not None and risk["value"] > c.max_risk + 1e-9:
            add(
                "max_risk",
                VIOLATED,
                c.max_risk,
                risk["value"],
                "A facility on the route has a supplier-risk score above the limit.",
            )
        elif risk["value"] is not None and risk["coverage"] >= 1.0 - 1e-9:
            add(
                "max_risk",
                SATISFIED,
                c.max_risk,
                risk["value"],
                "Every facility on the route has a score at or below the limit.",
            )
        else:
            add(
                "max_risk",
                UNVERIFIABLE,
                c.max_risk,
                risk["value"],
                f"Facilities without a risk score: {risk['facilities_without_score']}.",
            )
    if c.cargo_tags:
        bad = sorted({f"{t}_PROHIBITED" for t in c.cargo_tags for ln in path if f"{t}_PROHIBITED" in ln.restrictions})
        add("cargo_tags", VIOLATED if bad else SATISFIED, sorted(c.cargo_tags), bad, f"Lane restrictions apply: {', '.join(bad)}." if bad else "No recorded lane restriction applies. Absence of a recorded restriction is not proof that none exists.")  # fmt: skip
    if c.max_data_age_days is not None and ctx.now is not None:
        cutoff = ctx.now - timedelta(days=c.max_data_age_days)
        old = [ln.uid for ln in path if ln.observed_at is not None and ln.observed_at < cutoff]
        und = m["freshness"]["undated_lanes"]
        state = VIOLATED if old else (UNVERIFIABLE if und else SATISFIED)
        add("max_data_age_days", state, c.max_data_age_days, m["freshness"]["oldest_observed_at"], f"Older than the limit: {', '.join(old)}." if old else (f"Undated lanes: {', '.join(und)}." if und else "All lane figures are recent enough."))  # fmt: skip
    return out


def classify(results: list[dict[str, Any]]) -> str:
    if any(r["state"] == VIOLATED for r in results):
        return "INFEASIBLE"
    if any(r["state"] == UNVERIFIABLE for r in results):
        return "UNVERIFIABLE"
    return "FEASIBLE"


def value_of(m: dict[str, Any], crit: str) -> float | None:
    return {"cost": m["cost"]["total"], "duration": m["duration"]["total_hours"], "reliability": m["reliability"]["value"], "risk": m["risk"]["value"]}[crit]  # fmt: skip


def objective_gaps(m: dict[str, Any], weights: dict[str, float], currency: str | None) -> list[str]:
    """Reasons a route cannot be scored by the objective (data it needs is missing or not comparable)."""
    gaps = []
    for k in weights:
        if value_of(m, k) is None:
            gaps.append(f"{k}: " + {"cost": f"cost is {m['cost']['status'].lower()}", "duration": "a lane has no duration", "reliability": "a lane has no reliability value", "risk": "no facility on the route has a risk score"}[k])  # fmt: skip
        elif k == "cost" and currency and m["cost"]["currency"] != currency:
            gaps.append(f"cost: priced in {m['cost']['currency']} but ranking uses {currency}")
    return gaps


def rank(items: list[dict[str, Any]], weights: dict[str, float]) -> list[dict[str, Any]]:
    """Order scorable routes. Min-max normalisation over the candidate set puts every criterion on 0 (best) to 1 (worst)."""
    if not items:
        return []
    ranges = {}
    for k in weights:
        vals = [value_of(it["metrics"], k) for it in items]
        ranges[k] = (min(vals), max(vals))
    for it in items:
        parts = []
        for k, w in weights.items():
            lo, hi = ranges[k]
            v = value_of(it["metrics"], k)
            if hi - lo < 1e-12:
                norm = 0.0
            else:
                norm = (v - lo) / (hi - lo) if DIRECTION[k] == "min" else (hi - v) / (hi - lo)
            parts.append({"criterion": k, "value": v, "direction": DIRECTION[k], "normalised": _r(norm), "weight": _r(w), "contribution": _r(w * norm)})  # fmt: skip
        it["score"] = {"composite": _r(sum(p["contribution"] for p in parts)), "parts": parts, "normalisation": {k: {"min": _r(a), "max": _r(b)} for k, (a, b) in ranges.items()}}  # fmt: skip

    def key(it: dict[str, Any]) -> tuple:
        m = it["metrics"]
        return (round(it["score"]["composite"], 9), m["duration"]["total_hours"] if m["duration"]["total_hours"] is not None else math.inf, m["cost"]["total"] if m["cost"]["total"] is not None else math.inf, m["hops"], tuple(m["lane_uids"]))  # fmt: skip

    ordered = sorted(items, key=key)
    for i, it in enumerate(ordered, 1):
        it["rank"] = i
    return ordered


def pareto(items: list[dict[str, Any]], crits: tuple[str, ...] = CRITERIA) -> set[tuple[str, ...]]:
    """Routes that no other route beats on every available criterion (ties do not dominate)."""
    use = [k for k in crits if all(value_of(it["metrics"], k) is not None for it in items)]

    def vec(it: dict[str, Any]) -> list[float]:
        return [value_of(it["metrics"], k) * (1 if DIRECTION[k] == "min" else -1) for k in use]

    front = set()
    for a in items:
        va = vec(a)
        dominated = any(all(x <= y + 1e-12 for x, y in zip(vec(b), va, strict=True)) and any(x < y - 1e-12 for x, y in zip(vec(b), va, strict=True)) for b in items if b is not a)  # fmt: skip
        if not dominated:
            front.add(tuple(a["metrics"]["lane_uids"]))
    return front


def explain(
    it: dict[str, Any], req: RouteRequest, best: dict[str, Any] | None, constraint_results: list[dict[str, Any]]
) -> dict[str, Any]:
    m, score = it["metrics"], it["score"]
    w = req.effective_weights()
    crit_txt = ", ".join(f"{k} {int(round(v * 100))}%" for k, v in w.items())
    binding = [r["constraint"] for r in constraint_results if r["state"] == SATISFIED and _tight(r)]
    caveats = []
    if m["evidence_level"] == "SYNTHETIC":
        caveats.append("At least one lane is synthetic demonstration data.")
    if m["duration"]["quality"] != "VERIFIED" or m["cost"]["quality"] != "VERIFIED":
        caveats.append("Some duration or cost figures are estimates or unlabelled, not verified.")
    if m["reliability"]["sources"] and "PRIOR" in m["reliability"]["sources"]:
        caveats.append("Some lane reliabilities are documented priors, not measured from delivery history.")
    if m["data_flags"]:
        caveats.append("A lane has conflicting or duplicate records: " + ", ".join(m["data_flags"]) + ".")
    if m["disruption_notes"]:
        caveats.append("The route uses lanes degraded by the scenario or request.")
    if m["risk"]["coverage"] < 1.0:
        caveats.append(f"Only {int(round(m['risk']['coverage'] * 100))}% of the route's facilities have a risk score.")
    if best is None or best is it:
        why = f"Ranked first for objective {req.objective} (weights: {crit_txt}); composite score {score['composite']} (0 = best)."
    else:
        diffs = []
        for p, q in zip(score["parts"], best["score"]["parts"], strict=True):
            diffs.append(f"{p['criterion']} {p['value']} vs {q['value']}")
        why = (
            f"Ranked {it['rank']} behind the top route (composite {score['composite']} vs {best['score']['composite']}); "
            + "; ".join(diffs)
            + "."
        )
    return {
        "summary": why,
        "criteria": score["parts"],
        "binding_constraints": binding,
        "caveats": caveats,
        "statements": [STATEMENTS["decision_support"]],
    }


def _tight(r: dict[str, Any]) -> bool:
    lim, obs = r.get("limit"), r.get("observed")
    if isinstance(lim, (int, float)) and isinstance(obs, (int, float)) and lim:
        return abs(lim - obs) / abs(lim) <= 0.10
    return False
