"""Sourcing engine: eligibility -> suitability scoring -> exact allocation -> independent re-check -> metrics and explanation.

Pure with respect to the database (it works on a list of ``Candidate``). Hard constraints are enforced by eligibility and by the
solver; the objective only orders allocations that already satisfy them. Nothing is relaxed: a plan that cannot meet the demand
is reported as infeasible (or, only when explicitly requested, as a labelled partial plan).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from app.sourcing import eligibility as E
from app.sourcing import scoring as S
from app.sourcing.config import STATEMENTS, Limits, SourcingRequest
from app.sourcing.model import Candidate
from app.sourcing.solver import EPS, Item, Solution, check_allocation, largest_fulfilment, solve

MIN_MEANINGFUL_SHARE = 0.01
PRICE_TIE = 1e-6


def validate_request(req: SourcingRequest) -> list[str]:
    k, bad = req.constraints, []
    if req.strategy == "SINGLE" and (k.min_suppliers or 1) > 1:
        bad.append("strategy SINGLE conflicts with min_suppliers > 1")
    if req.strategy == "DUAL" and ((k.min_suppliers or 1) > 2 or (k.max_suppliers is not None and k.max_suppliers < 2)):
        bad.append("strategy DUAL needs exactly two suppliers, which conflicts with min_suppliers/max_suppliers")
    if req.strategy == "MULTI" and k.max_suppliers is not None and k.max_suppliers < 2:
        bad.append("strategy MULTI needs at least two suppliers, which conflicts with max_suppliers")
    if k.max_supplier_share is not None and req.strategy == "SINGLE" and k.max_supplier_share < 1:
        bad.append("strategy SINGLE needs one supplier to take 100%, which conflicts with max_supplier_share < 1")
    if (
        k.max_supplier_share is not None
        and k.max_suppliers is not None
        and k.max_supplier_share * k.max_suppliers < 1 - 1e-9
    ):
        bad.append("max_supplier_share x max_suppliers is below 100%: the demand can never be fully allocated")
    if (
        k.min_supplier_share is not None
        and k.min_suppliers is not None
        and k.min_supplier_share * k.min_suppliers > 1 + 1e-9
    ):
        bad.append("min_supplier_share x min_suppliers exceeds 100%")
    return bad


def bounds(strategy: str, req: SourcingRequest) -> tuple[int, int | None, dict[str, Any]]:
    k = req.constraints
    mn, mx = k.min_suppliers or 1, k.max_suppliers
    if strategy == "SINGLE":
        mn, mx = 1, 1
    elif strategy == "DUAL":
        mn, mx = 2, 2
    elif strategy == "MULTI":
        mn = max(2, mn)
    defaults: dict[str, Any] = {}
    if mn > 1 and k.min_supplier_share is None:
        defaults["min_supplier_share"] = MIN_MEANINGFUL_SHARE
    return mn, mx, defaults


def _cost(
    c: Candidate, objective: str, scores: dict[str, dict[str, Any]], max_price: float
) -> tuple[float | None, str | None]:
    if objective in ("LOWEST_COST", "RESILIENCE"):
        return (c.price, None) if c.price is not None else (None, "no unit price")
    if objective == "LOWEST_LANDED_COST":
        if c.price is None or c.freight is None:
            return (
                None,
                "no explicit freight per unit: landed cost cannot be stated, so the offer is not costed at zero freight",
            )
        return c.price + c.freight, None
    if objective == "RELIABILITY":
        if c.reliability.value is None:
            return None, f"no measured on-time rate ({c.reliability.note or 'insufficient deliveries'})"
        return 1.0 - c.reliability.value, None
    if objective == "LOWEST_RISK":
        return (c.risk, None) if c.risk is not None else (None, "no current supplier risk assessment")
    pen = 1.0 - scores[c.uid]["score"] / 100.0
    return pen + PRICE_TIE * ((c.price or 0.0) / max_price if max_price > 0 else 0.0), None


def _min_max_share(items: list[Item], demand: float, kw: dict[str, Any]) -> tuple[Solution, float | None]:
    def capped(s: float) -> list[Item]:
        return [replace(it, hi=min(it.hi, s * demand)) for it in items]

    top = solve(items, demand, **kw)
    if top.status != "OPTIMAL":
        return top, None
    hi_s = max(top.quantities.values()) / demand
    lo_s = 1.0 / max(1, len(items))
    best = top
    for _ in range(28):
        mid = (lo_s + hi_s) / 2
        trial = solve(capped(mid), demand, **kw)
        if trial.status == "OPTIMAL":
            hi_s, best = max(trial.quantities.values()) / demand, trial
        elif trial.status == "BOUND_EXCEEDED":
            best = trial
            break
        else:
            lo_s = mid
        if hi_s - lo_s < 1e-7:
            break
    return best, hi_s


def _metrics(rows: list[dict[str, Any]], by: dict[str, Candidate], demand: float, currency: str) -> dict[str, Any]:
    total = sum(r["quantity"] for r in rows)
    sh = [r["quantity"] / total for r in rows] if total > 0 else []
    hhi = sum(s * s for s in sh)
    ctry: dict[str, float] = {}
    for r in rows:
        ctry[by[r["offer_uid"]].country] = ctry.get(by[r["offer_uid"]].country, 0.0) + r["quantity"] / total
    chhi = sum(v * v for v in ctry.values())

    def wavg(attr: Callable[[Candidate], float | None]) -> float | None:
        vals = [(r["quantity"], attr(by[r["offer_uid"]])) for r in rows]
        if not rows or any(v is None for _, v in vals):
            return None
        return sum(q * v for q, v in vals) / total

    purchase = sum(r["quantity"] * by[r["offer_uid"]].price for r in rows)
    freight_known = bool(rows) and all(by[r["offer_uid"]].freight is not None for r in rows)
    freight = sum(r["quantity"] * by[r["offer_uid"]].freight for r in rows) if freight_known else None
    lead = [by[r["offer_uid"]].lead_days for r in rows]
    return {
        "currency": currency,
        "allocated_quantity": round(total, 9),
        "demand": demand,
        "unmet_quantity": round(max(0.0, demand - total), 9),
        "fulfilment_pct": round(100.0 * min(total, demand) / demand, 6),
        "purchase_cost": round(purchase, 6),
        "freight_cost": None if freight is None else round(freight, 6),
        "landed_cost_stated": None if freight is None else round(purchase + freight, 6),
        "cost_basis": "purchase + stated freight"
        if freight_known
        else "purchase only (freight not stated for every allocated offer)",
        "average_unit_cost": round(purchase / total, 6) if total > 0 else None,
        "average_lead_time_days": wavg(lambda c: c.lead_days),
        "slowest_lead_time_days": None if not rows or any(x is None for x in lead) else max(lead),
        "expected_defective_quantity": (lambda v: None if v is None else round(v * total, 6))(
            wavg(lambda c: c.defect_rate)
        ),
        "weighted_risk": wavg(lambda c: c.risk),
        "weighted_measured_reliability": wavg(lambda c: c.reliability.value),
        "supplier_count": len(rows),
        "hhi": round(hhi, 6) if rows else None,
        "effective_suppliers": round(1 / hhi, 4) if hhi > 0 else None,
        "max_supplier_share": round(max(sh), 6) if sh else None,
        "country_hhi": round(chhi, 6) if rows else None,
        "country_shares": {k: round(v, 6) for k, v in sorted(ctry.items())},
        "concentration_note": STATEMENTS["concentration"],
    }


def _checks(
    items: list[Item],
    q: dict[str, float],
    by: dict[str, Candidate],
    demand: float,
    k: Any,
    mn: int,
    mx: int | None,
    req: SourcingRequest,
    full: bool,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    target = demand if full else sum(q.values())
    viol = check_allocation(items, q, target, overage_pct=k.overage_pct if full else 0.0, min_n=mn, max_n=mx)
    names = (
        "demand/total",
        "capacity & contractual maximum",
        "minimum order quantity / minimum share",
        "order increment",
        "supplier count",
    )
    keys = (
        ("total", "outside the allowed"),
        ("exceeds the maximum", "capacity"),
        ("below the minimum", "moq"),
        ("multiple", "lot"),
        ("supplier(s) used", "n"),
    )
    for name, (needle, _) in zip(names, keys, strict=True):
        v = [x for x in viol if needle in x]
        out.append({"check": name, "passed": not v, "detail": "; ".join(v) if v else "satisfied"})
    bad = [uid for uid, v in q.items() if v > EPS and by[uid].price is None]
    ok = all(
        by[u].unit == req.unit and by[u].currency == req.currency and by[u].approval == "APPROVED"
        for u, v in q.items()
        if v > EPS
    )
    out.append({"check": "approved supplier, unit and currency", "passed": ok and not bad, "detail": "every allocated offer is APPROVED and stated in the requirement unit and currency" if ok and not bad else "an allocated offer is not approved or not in the requirement unit/currency"})  # fmt: skip
    if k.max_supplier_share is not None and q:
        mxs = max(q.values()) / max(sum(q.values()), EPS)
        out.append(
            {
                "check": "maximum supplier share",
                "passed": mxs <= k.max_supplier_share + 1e-7,
                "detail": f"largest share {mxs:.4f} (limit {k.max_supplier_share:.4f})",
            }
        )
    return out


def _explain(cands: list[Candidate], ass: dict[str, dict[str, Any]], items_info: dict[str, Any], demand: float, k: Any, mn: int, mx: int | None, sol: Solution | None, excluded_obj: dict[str, str], largest: float | None, req: SourcingRequest) -> dict[str, Any]:  # fmt: skip
    reasons: list[dict[str, Any]] = []
    by_status = Counter(a["status"] for a in ass.values())
    elig = [c for c in cands if ass[c.uid]["status"] == "ELIGIBLE"]
    usable: list[Item] = items_info["items"]
    if not cands:
        reasons.append(
            {
                "code": "NO_CANDIDATES",
                "message": "No supplier-product relationship or commercial offer is registered for this product and destination.",
                "facts": {},
            }
        )
    elif not elig:
        reasons.append(
            {
                "code": "NO_ELIGIBLE_SUPPLIER",
                "message": "No candidate passed every hard eligibility rule.",
                "facts": dict(by_status),
            }
        )
    for uid, why in excluded_obj.items():
        reasons.append(
            {
                "code": "OBJECTIVE_DATA_MISSING",
                "message": f"{uid} cannot take part in this objective: {why}.",
                "facts": {"offer": uid},
            }
        )
    if elig and not usable:
        reasons.append(
            {
                "code": "NO_USABLE_SUPPLIER",
                "message": "Eligible suppliers exist, but none can accept any quantity once minimums, capacities and share limits are applied.",
                "facts": {"eligible": len(elig)},
            }
        )
    cap_total = sum(it.hi for it in usable)
    if usable and cap_total < demand - 1e-9:
        reasons.append(
            {
                "code": "INSUFFICIENT_CAPACITY",
                "message": f"Aggregate usable capacity {cap_total:g} {req.unit} is below the demand {demand:g} {req.unit}.",
                "facts": {"capacity": cap_total, "demand": demand, "shortfall": demand - cap_total},
            }
        )
    if k.max_supplier_share is not None and elig:
        capped = sum(min(c.effective_capacity or 0.0, k.max_supplier_share * demand) for c in elig)
        if capped < demand - 1e-9:
            reasons.append(
                {
                    "code": "SHARE_CAP",
                    "message": f"With no supplier above {k.max_supplier_share:.0%} of the demand, at most {capped:g} {req.unit} can be placed.",
                    "facts": {"placeable": capped},
                }
            )
    if mn > len(usable) and usable:
        reasons.append(
            {
                "code": "TOO_FEW_SUPPLIERS",
                "message": f"{mn} suppliers are required but only {len(usable)} are usable.",
                "facts": {"required": mn, "usable": len(usable)},
            }
        )
    if req.strategy == "SINGLE" and usable and max(it.hi for it in usable) < demand:
        reasons.append(
            {
                "code": "NO_SINGLE_SOURCE",
                "message": "No single supplier can supply the whole demand.",
                "facts": {"largest_capacity": max(it.hi for it in usable)},
            }
        )
    moq_blocked = [i for i in items_info["unusable"]]
    if moq_blocked:
        reasons.append(
            {
                "code": "MINIMUM_EXCEEDS_MAXIMUM",
                "message": "Some eligible offers have a minimum order quantity or share above their usable maximum.",
                "facts": {"offers": moq_blocked},
            }
        )
    if sol is not None and sol.note:
        reasons.append(
            {
                "code": "SOLVER_LIMIT" if sol.status == "BOUND_EXCEEDED" else "LOT_OR_MINIMUM_CONFLICT",
                "message": sol.note,
                "facts": {},
            }
        )
    if not reasons:
        reasons.append(
            {
                "code": "CONSTRAINT_COMBINATION",
                "message": "Minimum quantities, order increments and supplier-count limits together admit no allocation that totals the demand.",
                "facts": {},
            }
        )
    pending = [c for c in cands if ass[c.uid]["status"] == "ELIGIBLE_AFTER_QUALIFICATION"]
    missing = [c for c in cands if ass[c.uid]["status"] == "MISSING_INFORMATION"]
    would = []
    if pending:
        cap = sum(c.effective_capacity or 0.0 for c in pending)
        would.append(
            f"{len(pending)} supplier(s) pending qualification (stated capacity {cap:g} {req.unit}) could be considered once qualified."
        )
    if missing:
        would.append(
            f"{len(missing)} supplier(s) cannot be verified for lack of: "
            + "; ".join(sorted({m for c in missing for m in ass[c.uid]["missing"]}))
            + "."
        )
    rules = Counter(e["rule"] for a in ass.values() for e in a["evidence"] if e["outcome"] == "FAIL")
    out = {
        "reasons": reasons,
        "exclusions": dict(by_status),
        "failed_rules": dict(rules),
        "information_that_would_help": would,
        "partial_fulfilment_possible_quantity": largest,
    }
    return out


def plan(cands: list[Candidate], ctx: E.Context, req: SourcingRequest, limits: Limits, *, strategy: str | None = None, objective: str | None = None, stop: Callable[[], None] | None = None, solve_plan: bool = True) -> dict[str, Any]:  # fmt: skip
    strategy = strategy or req.strategy
    objective = objective or req.objective
    k, demand = req.constraints, req.quantity
    ass = {c.uid: E.assess(c, ctx) for c in cands}
    elig = [c for c in cands if ass[c.uid]["status"] == "ELIGIBLE"]
    scores, swarn, basis = S.score_all(elig, req.weights, demand)
    order = S.rank(elig, scores) if elig else []
    rank_of = {uid: i + 1 for i, uid in enumerate(order)}
    mn, mx, defaults = bounds(strategy, req)
    share_lo = k.min_supplier_share if k.min_supplier_share is not None else defaults.get("min_supplier_share")
    top = demand * (1 + k.overage_pct / 100.0)
    max_price = max((c.price or 0.0 for c in elig), default=0.0)
    items: list[Item] = []
    excluded_obj: dict[str, str] = {}
    unusable: list[str] = []
    by = {c.uid: c for c in cands}
    for c in elig:
        cost, why = _cost(c, objective, scores, max_price)
        if cost is None:
            excluded_obj[c.uid] = why or "missing input"
            continue
        lo = max(c.moq or 0.0, (share_lo or 0.0) * demand)
        hi = min(c.effective_capacity or 0.0, top)
        if k.max_supplier_share is not None:
            hi = min(hi, k.max_supplier_share * demand)
        if lo > hi + EPS:
            unusable.append(c.uid)
            continue
        items.append(Item(c.uid, float(cost), lo, hi, c.lot))
    # the objective used by the solver may differ from price (RESILIENCE solves on price), so keep a price view for cost reporting
    kw = dict(overage_pct=k.overage_pct, min_n=mn, max_n=mx, max_nodes=limits.max_nodes, max_grid_units=limits.max_grid_units, max_dp_ops=limits.max_dp_operations, stop=stop)  # fmt: skip
    achieved_share: float | None = None

    def run(its: list[Item], d: float) -> Solution:
        nonlocal achieved_share
        if objective == "RESILIENCE":
            s, share = _min_max_share(its, d, kw)
            achieved_share = share
            return s
        return solve(its, d, **kw)

    sol: Solution | None = None
    largest: float | None = None
    full = True
    result = "infeasible" if solve_plan else "not_solved"
    alloc_q: dict[str, float] = {}
    if items and solve_plan:
        sol = run(items, demand)
        if sol.status == "OPTIMAL":
            alloc_q, result = sol.quantities, "feasible"
        elif sol.status == "BOUND_EXCEEDED":
            result = "incomplete"
        else:
            largest, exact = largest_fulfilment(items, demand, min_n=mn, max_n=mx, max_nodes=limits.max_nodes, max_grid_units=limits.max_grid_units, max_dp_ops=limits.max_dp_operations, stop=stop)  # fmt: skip
            if not exact:
                result = "incomplete"
            elif req.fulfillment == "PARTIAL" and largest > EPS:
                kw2 = {**kw, "overage_pct": 0.0}
                psol = (
                    solve(items, largest, **kw2)
                    if objective != "RESILIENCE"
                    else _min_max_share(items, largest, kw2)[0]
                )
                if psol.status == "OPTIMAL":
                    alloc_q, result, full, sol = psol.quantities, "partial", False, psol
    elif (
        solve_plan
        and cands
        and any(a["status"] in ("MISSING_INFORMATION", "ELIGIBLE_AFTER_QUALIFICATION") for a in ass.values())
        and not elig
    ):
        result = "incomplete"
    elif solve_plan and elig and excluded_obj and len(excluded_obj) == len(elig):
        result = "incomplete"
    rows: list[dict[str, Any]] = []
    for uid, q in sorted(alloc_q.items(), key=lambda kv: (-kv[1], kv[0])):
        if q <= EPS:
            continue
        c = by[uid]
        rows.append({
            "offer_uid": uid, "supplier_id": c.supplier_id, "supplier_code": c.supplier_code, "supplier_name": c.supplier_name, "country": c.country,
            "quantity": round(q, 9), "unit": req.unit, "unit_price": c.price, "freight_per_unit": c.freight, "purchase_cost": round(q * c.price, 6),
            "lead_time_days": c.lead_days, "capacity": c.effective_capacity, "capacity_utilisation": None if not c.effective_capacity else round(q / c.effective_capacity, 6),
            "suitability_score": scores[uid]["score"], "rank": rank_of[uid],
        })  # fmt: skip
    total = sum(r["quantity"] for r in rows)
    for r in rows:
        r["share"] = round(r["quantity"] / total, 6) if total > 0 else None
    metrics = _metrics(rows, by, demand, req.currency) if rows else None
    if metrics is not None and achieved_share is not None:
        metrics["minimised_max_share"] = round(achieved_share, 6)
    checks = (
        _checks(items, {r["offer_uid"]: r["quantity"] for r in rows}, by, demand, k, mn, mx, req, full) if rows else []
    )
    if rows and any(not x["passed"] for x in checks):
        # defensive: the independent re-check disagrees with the solver. Never return such a plan.
        result, rows, metrics, checks = "incomplete", [], None, []
        excluded_obj["_internal"] = "the independent hard-constraint re-check rejected the solver result"
    infeas = None
    if solve_plan and result in ("infeasible", "incomplete", "partial"):
        infeas = _explain(
            cands, ass, {"items": items, "unusable": unusable}, demand, k, mn, mx, sol, excluded_obj, largest, req
        )
    if result == "partial" and infeas is not None:
        infeas["unmet_quantity"] = round(demand - total, 9)
    cand_out = []
    for c in cands:
        a = ass[c.uid]
        sc = scores.get(c.uid)
        cand_out.append({
            "offer_uid": c.uid, "supplier_id": c.supplier_id, "supplier_code": c.supplier_code, "supplier_name": c.supplier_name, "country": c.country,
            "status": a["status"], "reasons": a["reasons"], "missing": a["missing"], "evidence": a["evidence"], "rank": rank_of.get(c.uid),
            "score": sc, "objective_excluded": excluded_obj.get(c.uid), "allocated_quantity": next((r["quantity"] for r in rows if r["offer_uid"] == c.uid), 0.0),
            "terms": {"unit": c.unit, "unit_price": c.price, "currency": c.currency, "freight_per_unit": c.freight, "capacity": c.capacity, "effective_capacity": c.effective_capacity, "capacity_observed_at": c.capacity_observed_at.isoformat() if c.capacity_observed_at else None, "min_order_quantity": c.moq, "lot_size": c.lot, "max_allocation": c.max_allocation, "lead_time_days": c.lead_days, "lead_time_std_days": c.lead_std_days, "defect_rate": c.defect_rate, "approval": c.approval, "relationship": c.link, "offer_status": c.offer_status, "observed_at": c.observed_at.isoformat() if c.observed_at else None, "provenance": c.provenance, "quality": {"price": c.price_quality, "capacity": c.capacity_quality, "lead_time": c.lead_quality, "defect_rate": c.quality_quality}},  # fmt: skip
            "risk": None if c.risk is None else {"score": c.risk, "source": c.risk_source},
            "reliability": {"value": c.reliability.value, "n": c.reliability.n, "interval": c.reliability.interval, "note": c.reliability.note},
            "transport": c.transport, "scenario_notes": list(c.notes),
        })  # fmt: skip
    cand_out.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["supplier_code"], r["offer_uid"]))
    ranking_incomplete = any(not s["complete"] for s in scores.values())
    return {
        "result_status": result,
        "strategy": strategy,
        "objective": objective,
        "allocations": rows,
        "metrics": metrics,
        "hard_constraint_checks": checks,
        "infeasibility": infeas,
        "candidates": cand_out,
        "price_basis": basis,
        "ranking_incomplete": ranking_incomplete,
        "ranking_warnings": swarn,
        "bounds": {"min_suppliers": mn, "max_suppliers": mx, "applied_defaults": defaults},
        "solver": {
            "method": None if sol is None else sol.method,
            "status": None if sol is None else sol.status,
            "nodes_or_operations": None if sol is None else sol.nodes,
            "exact": True if sol is None else sol.exact,
            "grid_step": None if sol is None else sol.grid_step,
            "note": None if sol is None else sol.note,
            "optimality": "EXACT" if (sol is None or sol.exact) else "NOT_PROVEN",
            "objective_value": None if sol is None or sol.status != "OPTIMAL" else sol.objective,
        },  # fmt: skip
        "counts": dict(Counter(a["status"] for a in ass.values())),
    }


def exposure(base: dict[str, Any], disrupted: list[Candidate]) -> dict[str, Any]:
    """How much of an existing plan's quantity would be lost if it were executed unchanged under the disrupted terms."""
    cur = {c.uid: c for c in disrupted}
    lost, detail = 0.0, []
    for r in base["allocations"]:
        c = cur.get(r["offer_uid"])
        if c is None or c.unavailable_reason:
            loss = r["quantity"]
        else:
            cap = c.effective_capacity
            loss = max(0.0, r["quantity"] - cap) if cap is not None else 0.0
        if loss > EPS:
            detail.append(
                {"offer_uid": r["offer_uid"], "supplier_code": r["supplier_code"], "lost_quantity": round(loss, 9)}
            )
        lost += loss
    tot = sum(r["quantity"] for r in base["allocations"])
    return {"lost_quantity": round(lost, 9), "lost_share": round(lost / tot, 6) if tot else None, "by_supplier": detail}


def compare_results(
    base: dict[str, Any], scen: dict[str, Any], applied: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> dict[str, Any]:
    bm, sm = base["metrics"] or {}, scen["metrics"] or {}

    def delta(key: str) -> dict[str, Any] | None:
        a, b = bm.get(key), sm.get(key)
        if a is None or b is None:
            return None
        return {
            "baseline": a,
            "scenario": b,
            "change": round(b - a, 6),
            "change_pct": None if a == 0 else round(100 * (b - a) / a, 4),
        }

    bq = {r["offer_uid"]: r for r in base["allocations"]}
    sq = {r["offer_uid"]: r for r in scen["allocations"]}
    changes = []
    for uid in sorted(set(bq) | set(sq)):
        a, b = bq.get(uid, {}).get("quantity", 0.0), sq.get(uid, {}).get("quantity", 0.0)
        r = bq.get(uid) or sq.get(uid)
        changes.append(
            {
                "offer_uid": uid,
                "supplier_code": r["supplier_code"],
                "baseline_quantity": a,
                "scenario_quantity": b,
                "change": round(b - a, 9),
            }
        )
    return {
        "feasibility_changed": base["result_status"] != scen["result_status"], "status": {"baseline": base["result_status"], "scenario": scen["result_status"]},
        "unmet_quantity": delta("unmet_quantity"), "purchase_cost": delta("purchase_cost"), "average_unit_cost": delta("average_unit_cost"),
        "average_lead_time_days": delta("average_lead_time_days"), "hhi": delta("hhi"), "supplier_count": delta("supplier_count"),
        "weighted_risk": delta("weighted_risk"), "reallocation": changes, "suppliers_lost": [c["supplier_code"] for c in changes if c["baseline_quantity"] > 0 and c["scenario_quantity"] == 0],
        "suppliers_gained": [c["supplier_code"] for c in changes if c["baseline_quantity"] == 0 and c["scenario_quantity"] > 0],
        "changed_assumptions": [{"source": a["source"], "effect": a["effect"], "offers_affected": a.get("offers_affected")} for a in applied],
        "not_applied": skipped,
        "note": "Cost and lead-time differences are shown only where both plans state them; an unmet quantity under the scenario is a real shortfall, not a modelling artefact.",
    }  # fmt: skip


def strategy_comparison(cands: list[Candidate], ctx: E.Context, req: SourcingRequest, limits: Limits, disrupted: list[Candidate] | None, stop: Callable[[], None] | None = None) -> dict[str, Any]:  # fmt: skip
    out: dict[str, Any] = {}
    plans: dict[str, dict[str, Any]] = {}
    for s in ("SINGLE", "DUAL", "MULTI", "AUTO"):
        p = plan(cands, ctx, req, limits, strategy=s, stop=stop)
        plans[s] = p
        m = p["metrics"] or {}
        row: dict[str, Any] = {"result_status": p["result_status"], "supplier_count": m.get("supplier_count"), "purchase_cost": m.get("purchase_cost"), "average_unit_cost": m.get("average_unit_cost"), "hhi": m.get("hhi"), "max_supplier_share": m.get("max_supplier_share"), "unmet_quantity": m.get("unmet_quantity", req.quantity if p["result_status"] != "feasible" else 0.0), "average_lead_time_days": m.get("average_lead_time_days"), "allocations": p["allocations"], "reasons": [r["code"] for r in (p["infeasibility"] or {}).get("reasons", [])]}  # fmt: skip
        if disrupted is not None:
            row["exposure_if_executed_unchanged"] = exposure(p, disrupted)
            rp = plan(disrupted, ctx, req, limits, strategy=s, stop=stop)
            rm = rp["metrics"] or {}
            row["reoptimised_under_scenario"] = {"result_status": rp["result_status"], "purchase_cost": rm.get("purchase_cost"), "unmet_quantity": rm.get("unmet_quantity", req.quantity if rp["result_status"] != "feasible" else 0.0), "supplier_count": rm.get("supplier_count")}  # fmt: skip
        out[s] = row
    costs = {
        s: r["purchase_cost"]
        for s, r in out.items()
        if r["result_status"] == "feasible" and r["purchase_cost"] is not None
    }
    if costs:
        cheapest = min(costs.values())
        for s, r in out.items():
            if s in costs:
                r["cost_of_diversification_vs_cheapest"] = {
                    "amount": round(costs[s] - cheapest, 6),
                    "pct": None if cheapest == 0 else round(100 * (costs[s] - cheapest) / cheapest, 4),
                }
    return {"strategies": out, "note": "Cost of diversification is measured against the cheapest feasible strategy for the same objective and inputs. A resilience benefit is stated only under a scenario or what-if (exposure and re-optimised columns); otherwise only shares and HHI are shown.", "statements": [STATEMENTS["resilience"]]}  # fmt: skip
