"""Orchestration: problem -> objectives/constraints -> candidate plans -> evaluation -> trade-off analysis -> explanation.

``compute`` is a pure function of (snapshot, request, limits): no database, no clock, no randomness, so a stored run replays exactly.
The solver here is a bounded grid enumeration with an independent validator; it is NOT a proof of global optimality.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from typing import Any

from app.resilience import objectives as O
from app.resilience import pareto as PA
from app.resilience import plan as PL
from app.resilience.config import ALGORITHM_VERSION, STATEMENTS, Limits, ResilienceRequest, stable_hash
from app.resilience.model import EPS, build_problem, metrics

SOLVER = {
    "name": "grid-enumeration",
    "version": ALGORITHM_VERSION,
    "type": "APPROXIMATE_GRID",
    "deterministic": True,
    "random_seed": None,
}


def _w(code: str, sev: str, msg: str) -> dict[str, str]:
    return {"code": code, "severity": sev, "message": msg}


def _result(status: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "result_status": status, "objectives": None, "constraints": None, "scenarios": None, "alternatives": [], "pareto": None, "recommendation": None,
        "comparison": None, "sensitivity": None, "diagnostics": None, "explanation": [], "warnings": [], "solver": dict(SOLVER), "statistics": None,
        "statements": {k: STATEMENTS[k] for k in ("decision_support", "pareto", "weights", "scenarios", "adaptive", "probabilities", "cost", "missing", "tenancy")},  # fmt: skip
    }  # fmt: skip
    base.update(kw)
    return base


def _fmt(oid: str, v: float | None) -> str:
    if v is None:
        return "n/a"
    u = O.BY_ID[oid]["unit"]
    if u == "currency":
        return f"{v:,.2f}"
    if u.startswith("fraction") or u.startswith("index"):
        return f"{v:.3f}"
    return f"{v:,.2f} {u}"


def _plan_row(pid: str, plan: dict[str, float], m: dict[str, Any], P: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": pid, "allocation": [{"offer_uid": u, "supplier_id": P["sup"][u]["supplier_id"], "supplier_code": P["sup"][u]["code"], "supplier_name": P["sup"][u]["name"], "country": P["sup"][u].get("country"), "quantity": round(q, 6), "share": round(q / m["total_quantity"], 6), "unit_price": P["sup"][u]["price"], "lead_time_days": P["sup"][u]["lead_days"]} for u, q in sorted(plan.items(), key=lambda kv: (-kv[1], kv[0])) if q > EPS],  # fmt: skip
        "total_quantity": round(m["total_quantity"], 6), "values": {k: (None if v is None else round(v, 6)) for k, v in m["values"].items()},
        "cost": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m["cost"].items()}, "baseline_fulfilment": round(m["baseline_fulfilment"], 6),
        "country_hhi": m["countries"], "scenarios": [{**r, **{k: round(r[k], 6) for k in ("demand", "delivered", "supply_lost", "from_stock", "recovered", "unmet", "fulfilment")}} for r in m["scenarios"]],  # fmt: skip
        "expected_unmet": m["expected_unmet"],
    }  # fmt: skip


def _compare(base: dict[str, Any], cand: dict[str, Any], oids: list[str]) -> list[dict[str, Any]]:
    rows = []
    for oid in O.BY_ID:
        if oid not in oids and oid not in ("COST", "UNMET_DEMAND"):
            continue
        b, c = base["values"].get(oid), cand["values"].get(oid)
        row: dict[str, Any] = {"objective": oid, "name": O.BY_ID[oid]["name"], "unit": O.BY_ID[oid]["unit"], "direction": O.BY_ID[oid]["direction"], "baseline": b, "candidate": c, "difference": None, "relative_pct": None, "verdict": "n/a", "caveat": None}  # fmt: skip
        if b is None or c is None:
            row["caveat"] = "the metric is unavailable for one of the plans"
        else:
            d = c - b
            row["difference"] = round(d, 6)
            if abs(b) > EPS:
                row["relative_pct"] = round(d / abs(b) * 100, 4)
            else:
                row["caveat"] = "the baseline value is zero, so a percentage change is undefined"
            better = (d < -1e-9) if O.BY_ID[oid]["direction"] == "min" else (d > 1e-9)
            worse = (d > 1e-9) if O.BY_ID[oid]["direction"] == "min" else (d < -1e-9)
            row["verdict"] = "better" if better else "worse" if worse else "unchanged"
        rows.append(row)
    return rows


def compute(snap: dict[str, Any], req: ResilienceRequest, limits: Limits, *, stop: Callable[[], None] | None = None, _inner: bool = False) -> dict[str, Any]:  # fmt: skip
    t0 = time.monotonic()
    P = build_problem(snap, req)
    warnings: list[dict[str, str]] = list(snap.get("warnings", []))
    if not P["eligible"]:
        counts: dict[str, int] = {}
        for s in snap["suppliers"]:
            counts[s["status"]] = counts.get(s["status"], 0) + 1
        why = "no offers are registered for this product and destination" if not snap["suppliers"] else "no offer is eligible (" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) + ")"  # fmt: skip
        return _result("insufficient_data", warnings=warnings + [_w("NO_ELIGIBLE_SUPPLIER", "ERROR", f"No plan can be built: {why}.")], diagnostics={"eligibility_counts": counts, "suppliers": [{"offer_uid": s["uid"], "supplier_code": s["code"], "status": s["status"], "reasons": s.get("reasons", [])} for s in snap["suppliers"]]})  # fmt: skip
    inexact = False
    if len(P["order"]) > limits.max_eligible_suppliers:
        keep = set(P["order"][: limits.max_eligible_suppliers])
        P["order"] = [u for u in P["order"] if u in keep]
        P["sup"] = {u: s for u, s in P["sup"].items() if u in keep}
        P["eligible"] = [s for s in P["eligible"] if s["uid"] in keep]
        inexact = True
        warnings.append(_w("SUPPLIERS_TRUNCATED", "WARNING", f"Only the {limits.max_eligible_suppliers} lowest-priced eligible offers were enumerated; the others were not considered, so the result is not exhaustive."))  # fmt: skip
    avail = O.availability({"eligible": P["eligible"], "has_stress": P["has_stress"]})
    listed = [o for o in req.objectives]
    enabled = [o for o in listed if avail[o.id]["available"]]
    unavailable = [{"id": o.id, "reason": avail[o.id]["reason"]} for o in listed if not avail[o.id]["available"]]
    registry = [{**d, "availability": avail[d["id"]], "enabled": any(o.id == d["id"] for o in enabled), "weight": next((o.weight for o in listed if o.id == d["id"]), None)} for d in O.REGISTRY]  # fmt: skip
    pc = req.plan_constraints
    for need, oid, label in ((pc.max_delay_days, "DELIVERY_DELAY", "max_delay_days"), (pc.min_worst_case_fulfilment, "WORST_CASE_SERVICE", "min_worst_case_fulfilment"), (pc.budget_ceiling, "COST", "budget_ceiling")):  # fmt: skip
        if need is not None and not avail[oid]["available"]:
            return _result("invalid_input", objectives={"registry": registry, "unavailable": unavailable}, warnings=warnings + [_w("CONSTRAINT_NEEDS_UNAVAILABLE_INPUT", "ERROR", f"{label} cannot be checked: {avail[oid]['reason']}.")])  # fmt: skip
    oids = [o.id for o in enabled]
    if unavailable:
        warnings.append(_w("OBJECTIVES_UNAVAILABLE", "WARNING", "Unavailable objectives were left out, not replaced by zero: " + "; ".join(f"{u['id']} ({u['reason']})" for u in unavailable) + "."))  # fmt: skip
    if not oids or (req.strategy == "WEIGHTED_SUM" and not any(o.weight > 0 for o in enabled)):
        return _result("insufficient_data", objectives={"registry": registry, "unavailable": unavailable}, warnings=warnings + [_w("NO_USABLE_OBJECTIVE", "ERROR", "None of the requested weighted objectives can be computed from the available data.")])  # fmt: skip
    if req.strategy == "EPSILON_CONSTRAINT":
        bad = [k for k in [req.primary_objective, *req.epsilon_limits] if k not in oids]
        if bad:
            return _result("insufficient_data", objectives={"registry": registry, "unavailable": unavailable}, warnings=warnings + [_w("EPSILON_OBJECTIVE_UNAVAILABLE", "ERROR", f"Objective(s) {', '.join(bad)} are unavailable for this problem, so the epsilon-constraint strategy cannot run.")])  # fmt: skip
    unknown = [b.offer_uid for b in req.baseline if b.offer_uid not in P["sup"]]
    if unknown:
        return _result("invalid_input", warnings=warnings + [_w("BASELINE_OFFER_NOT_ELIGIBLE", "ERROR", f"The baseline uses offer(s) that are unknown or not eligible: {', '.join(unknown)}. Correct the baseline or the constraints.")])  # fmt: skip
    for s in P["scenarios"]:
        if s.get("error"):
            warnings.append(_w("SCENARIO_NOT_APPLIED", "WARNING", f"Scenario '{s['name']}': {s['error']}"))
    if not P["stock_known"]:
        warnings.append(_w("NO_OPENING_STOCK", "INFO", "No opening stock was supplied or found, so no inventory buffer is assumed (this is not the same as zero stock)."))  # fmt: skip
    if any(s["lead_days"] is None for s in P["eligible"]):
        warnings.append(
            _w(
                "LEAD_TIME_UNKNOWN",
                "WARNING",
                "An eligible offer has no stated lead time; its deliveries are assumed to fit inside the horizon.",
            )
        )
    if not P["freight_basis"]:
        warnings.append(
            _w(
                "COST_PURCHASE_ONLY",
                "INFO",
                "Freight is not stated by every eligible offer, so cost is purchase price only for every plan.",
            )
        )
    if any(s["provenance"] == "SYNTHETIC_DEMO" for s in P["eligible"] if s.get("provenance")):
        warnings.append(_w("SYNTHETIC_OFFERS", "WARNING", "Some offers are synthetic demonstration data: results verify the software, not real sourcing options."))  # fmt: skip

    # ------------------------------------------------------------------ candidate generation and evaluation
    steps = req.grid_steps
    try:
        plans, gstats = PL.generate(P, req, steps, limits.max_candidates, stop)
    except PL.Budget:
        return _result("timed_out", objectives={"registry": registry, "unavailable": unavailable}, warnings=warnings + [_w("TIME_BUDGET", "ERROR", "The time budget ran out while generating candidate plans; no recommendation is made.")], statistics={"phase": "generation"})  # fmt: skip
    anchors = []
    for a in snap.get("anchors", []):
        al = {u: q for u, q in a["allocation"].items() if u in P["sup"]}
        if not al:
            continue
        if al in plans:
            anchors.append(
                {"name": a["name"], "optimality": a.get("optimality"), "index": plans.index(al), "on_grid": True}
            )
        else:
            plans.append(al)
            anchors.append(
                {"name": a["name"], "optimality": a.get("optimality"), "index": len(plans) - 1, "on_grid": False}
            )
    if gstats["truncated"]:
        inexact = True
        warnings.append(_w("ENUMERATION_TRUNCATED", "WARNING", f"The candidate limit ({limits.max_candidates}) was reached; the grid was not fully enumerated. Lower grid_steps or add constraints."))  # fmt: skip
    ids = [f"C{i + 1:05d}" for i in range(len(plans))]
    for a in anchors:
        a["id"] = ids[a["index"]]
    evals: dict[str, dict[str, Any]] = {}
    failures: dict[str, int] = {}
    feasible: list[str] = []
    struct_ok: list[str] = []
    try:
        for n, (pid, plan) in enumerate(zip(ids, plans)):
            if n % 200 == 0 and stop:
                stop()
            m = metrics(P, plan)
            chk = PL.metric_constraints(P, req, m)
            struct_ok.append(pid)
            bad = [c["id"] for c in chk if not c["passed"]]
            for b in bad:
                failures[b] = failures.get(b, 0) + 1
            evals[pid] = {
                "values": m["values"],
                "baseline_fulfilment": m["baseline_fulfilment"],
                "bad": bad,
                "n": len(plan),
            }
            if not bad:
                feasible.append(pid)
    except PL.Budget:
        return _result("timed_out", objectives={"registry": registry, "unavailable": unavailable}, warnings=warnings + [_w("TIME_BUDGET", "ERROR", "The time budget ran out while evaluating candidate plans; no recommendation is made.")], statistics={"phase": "evaluation", "generated": len(plans), "evaluated": len(evals)})  # fmt: skip
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "_Budget":
            return _result("timed_out", objectives={"registry": registry, "unavailable": unavailable}, warnings=warnings + [_w("TIME_BUDGET", "ERROR", "The time budget ran out while evaluating candidate plans; no recommendation is made.")], statistics={"phase": "evaluation", "generated": len(plans), "evaluated": len(evals)})  # fmt: skip
        raise
    stats = {
        **gstats,
        "generated": len(plans),
        "structurally_valid": len(struct_ok),
        "feasible": len(feasible),
        "metric_constraint_failures": failures,
    }
    top_cap = sum(float(s["capacity"]) for s in P["eligible"])
    obj_block = {"registry": registry, "unavailable": unavailable, "enabled": oids, "strategy": req.strategy}
    if not feasible:
        diag: dict[str, Any] = {"reason": "", "eligible_capacity_total": top_cap, "demand": P["D"], "statistics": stats, "relaxations": [], "note": "Diagnostic only: nothing below was applied. Constraints were not relaxed."}  # fmt: skip
        if not struct_ok:
            diag["reason"] = "No allocation satisfies the hard allocation rules (capacity, minimum order, lot size, supplier count and share, demand balance)." + (" Total eligible capacity is below the demand." if top_cap < P["D"] - EPS and not P["allow_partial"] else "")  # fmt: skip
        else:
            diag["reason"] = "Allocations exist, but none satisfies every configured limit at once."
            rel = []
            for cid, oid, want, label in (("budget_ceiling", "COST", pc.budget_ceiling, "budget ceiling"), ("max_hhi / max_delay_days", "CONCENTRATION", pc.max_hhi, "max HHI"), ("max_hhi / max_delay_days", "DELIVERY_DELAY", pc.max_delay_days, "max lead time (days)"), ("min_worst_case_fulfilment", "WORST_CASE_SERVICE", pc.min_worst_case_fulfilment, "min worst-case fulfilment")):  # fmt: skip
                if want is None:
                    continue
                vals = [evals[p]["values"][oid] for p in struct_ok if evals[p]["values"][oid] is not None]
                best = (max(vals) if O.BY_ID[oid]["direction"] == "max" else min(vals)) if vals else None
                rel.append({"constraint": label, "requested": want, "best_achievable_among_valid_allocations": best, "failing_plans": failures.get(cid, 0), "diagnostic_only": True})  # fmt: skip
            if pc.min_baseline_fulfilment is not None:
                rel.append({"constraint": "min baseline fulfilment", "requested": pc.min_baseline_fulfilment, "best_achievable_among_valid_allocations": max(evals[p]["baseline_fulfilment"] for p in struct_ok), "diagnostic_only": True})  # fmt: skip
            diag["relaxations"] = rel
        return _result("infeasible", objectives=obj_block, diagnostics=diag, warnings=warnings, statistics=stats)

    # ------------------------------------------------------------------ strategy
    w_raw = {o.id: o.weight for o in enabled}
    vec = {p: PA.vector(evals[p]["values"], oids) for p in feasible}
    rg = PA.ranges(list(vec.values()))
    flat = [oids[k] for k, (lo, hi) in enumerate(rg) if hi - lo <= 1e-12]
    wsum = sum(w_raw[o] for o in oids) or 1.0
    w = [w_raw[o] / wsum for o in oids]
    scores = {p: PA.score(vec[p], w, rg) for p in feasible}
    front_ids, ties = PA.nondominated([(p, vec[p]) for p in feasible])
    pool = feasible
    eps_info = None
    if req.strategy == "EPSILON_CONSTRAINT":

        def ok_eps(p: str) -> bool:
            for k, lim in req.epsilon_limits.items():
                v = evals[p]["values"][k]
                if v is None or (v > lim + 1e-9 if O.BY_ID[k]["direction"] == "min" else v < lim - 1e-9):
                    return False
            return True

        pool = [p for p in feasible if ok_eps(p)]
        eps_info = {"primary": req.primary_objective, "limits": req.epsilon_limits, "plans_within_limits": len(pool)}
        if not pool:
            rel = []
            for k, lim in req.epsilon_limits.items():
                vals = [evals[p]["values"][k] for p in feasible if evals[p]["values"][k] is not None]
                rel.append({"constraint": f"{k} limit", "requested": lim, "best_achievable_among_feasible_plans": (max(vals) if O.BY_ID[k]["direction"] == "max" else min(vals)) if vals else None, "diagnostic_only": True})  # fmt: skip
            return _result("infeasible", objectives=obj_block, statistics=stats, warnings=warnings, diagnostics={"reason": "Plans exist, but none keeps every secondary objective within its limit. The limits were not relaxed.", "relaxations": rel, "statistics": stats, "note": "Diagnostic only: nothing below was applied."})  # fmt: skip
    pk = (
        oids.index(req.primary_objective)
        if req.strategy == "EPSILON_CONSTRAINT" and req.primary_objective in oids
        else None
    )
    if req.strategy == "WEIGHTED_SUM":
        rec_id = min(pool, key=lambda p: (round(scores[p], 12), p))
    elif req.strategy == "EPSILON_CONSTRAINT":
        rec_id = min(pool, key=lambda p: (round(vec[p][pk], 12), round(scores[p], 12), p))  # type: ignore[index]
    else:
        rec_id = None
    # reference / baseline plan
    ref_label, ref_plan, ref_id = None, None, None
    if req.baseline:
        ref_plan, ref_label = {b.offer_uid: b.quantity for b in req.baseline if b.quantity > EPS}, "SUPPLIED_BASELINE"
    elif "COST" in oids:
        ref_id = min(feasible, key=lambda p: (evals[p]["values"]["COST"], p))
        ref_plan, ref_label = plans[ids.index(ref_id)], "REFERENCE_COST_MINIMAL"
    keep = {rec_id} if rec_id else set()
    show = PA.thin(front_ids, vec, rg, min(req.max_alternatives, limits.max_alternatives), keep)
    if rec_id and rec_id not in show:
        show.append(rec_id)
    final_ids = sorted(set(show) | ({rec_id} if rec_id else set()))
    index = {pid: plans[ids.index(pid)] for pid in final_ids}
    alts = []
    for pid in final_ids:
        m = metrics(P, index[pid])
        row = _plan_row(pid, index[pid], m, P)
        row.update({"pareto": pid in front_ids, "score": round(scores[pid], 6), "normalised": [round(x, 6) for x in PA.normalise(vec[pid], rg)], "recommended": pid == rec_id, "feasible": True, "equivalent_plans": ties.get(pid, []), "anchor": next((a["name"] for a in anchors if a["id"] == pid), None), "constraint_checks": PL.structural(P, req, index[pid]) + PL.metric_constraints(P, req, m)})  # fmt: skip
        alts.append(row)
    # ------------------------------------------------------------------ baseline comparison
    comparison = None
    ref_row = None
    if ref_plan is not None:
        mb = metrics(P, ref_plan)
        ref_row = _plan_row(ref_id or "BASELINE", ref_plan, mb, P)
        chk = PL.structural(P, req, ref_plan) + PL.metric_constraints(P, req, mb)
        ref_row["constraint_checks"] = chk
        ref_row["feasible"] = all(c["passed"] for c in chk)
        target = next((a for a in alts if a["recommended"]), None)
        comparison = {"reference": ref_label, "reference_plan": ref_row, "candidate_id": target["id"] if target else None, "rows": _compare(ref_row, target, oids) if target else [], "note": None if ref_label == "SUPPLIED_BASELINE" else "No baseline was supplied: the reference is the lowest-cost feasible plan of this run, not the current operation.", "reference_feasible": ref_row["feasible"]}  # fmt: skip
        if not ref_row["feasible"]:
            warnings.append(_w("BASELINE_VIOLATES_CONSTRAINTS", "WARNING", "The supplied baseline violates at least one configured constraint; it is shown for comparison only."))  # fmt: skip
    # ------------------------------------------------------------------ scenarios
    sc_block = None
    if P["scenarios"]:
        rec = next((a for a in alts if a["recommended"]), alts[0])
        sc_block = {"mode": P["mode"], "response_days": P["response"], "definitions": [{k: v for k, v in s.items() if k != "effects"} | {"affected_offers": sorted(s.get("effects", {}))} for s in P["scenarios"]], "results_for": rec["id"], "weighted": rec["expected_unmet"] is not None, "label": ("Adaptive plan: spare capacity of other suppliers may cover a shortfall after the stated response time." if P["mode"] == "ADAPTIVE" else "Fixed plan: the allocation is not changed after the disruption."), "baseline_results": ref_row["scenarios"] if ref_row else None, "outcomes": rec["scenarios"], "expected_unmet": rec["expected_unmet"], "worst": min(rec["scenarios"], key=lambda r: (r["fulfilment"], r["name"]))["name"] if rec["scenarios"] else None}  # fmt: skip
    # ------------------------------------------------------------------ explanation
    explanation: list[str] = []
    if rec_id:
        r = next(a for a in alts if a["recommended"])
        used = ", ".join(f"{x['supplier_code']} {x['share'] * 100:.0f}%" for x in r["allocation"])
        explanation.append(f"Recommended alternative {rec_id}: {used}.")
        if comparison and comparison["rows"]:
            imp = [x["name"] for x in comparison["rows"] if x["verdict"] == "better"]
            wor = [x["name"] for x in comparison["rows"] if x["verdict"] == "worse"]
            explanation.append(f"Against the {ref_label.lower().replace('_', ' ')}: better on {', '.join(imp) or 'no objective'}; worse on {', '.join(wor) or 'no objective'}.")  # fmt: skip
        binding = sorted({c["id"] for c in r["constraint_checks"] if c.get("binding")})
        if binding:
            explanation.append("Constraints at their limit for this plan: " + ", ".join(binding) + ".")
        explanation.append(f"{len(front_ids)} non-dominated alternative(s) were found among {len(feasible)} feasible plans (of {len(plans)} generated).")  # fmt: skip
    else:
        explanation.append(f"{len(front_ids)} non-dominated alternatives were found among {len(feasible)} feasible plans. No single winner is declared: choose along the trade-off.")  # fmt: skip
    if flat:
        explanation.append(
            "These objectives do not differ between feasible plans and so do not influence the choice: "
            + ", ".join(flat)
            + "."
        )
    if P["scenarios"]:
        explanation.append(f"{len(P['scenarios'])} scenario(s) were evaluated in {P['mode']} mode" + ("; probabilities were supplied by the requester." if sc_block and sc_block["weighted"] else "; no probabilities were supplied, so only individual and worst-case outcomes are reported."))  # fmt: skip
    explanation.append(
        "Grid enumeration approximates the continuous frontier; plans between grid points were not examined."
    )
    # conflicts between objectives (best plan per objective)
    conflicts = []
    for k, o in enumerate(oids):
        b = min(front_ids, key=lambda p: (vec[p][k], p))
        conflicts.append({"objective": o, "best_plan": b, "best_value": evals[b]["values"][o], "values_of_other_objectives_in_that_plan": {x: evals[b]["values"][x] for x in oids if x != o}})  # fmt: skip
    pareto_block = {"front_size": len(front_ids), "shown": len(show), "ids": sorted(front_ids), "objectives": oids, "ranges": [{"objective": o, "ideal": O.sign(o) * lo, "nadir": O.sign(o) * hi} for o, (lo, hi) in zip(oids, rg)], "non_discriminating": flat, "conflicts": conflicts, "tie_groups": ties, "approximation": "Exact for the enumerated share grid; an approximation of the continuous frontier.", "dominated_removed": len(feasible) - len(front_ids) - sum(len(v) for v in ties.values())}  # fmt: skip
    obj_block.update({"weights": dict(zip(oids, [round(x, 6) for x in w])) if req.strategy == "WEIGHTED_SUM" else None, "normalisation": {"method": "min-max over the feasible candidates", "ranges": pareto_block["ranges"]}, "epsilon": eps_info})  # fmt: skip
    cons_block = {"hard": [{"id": c[0], "kind": c[1], "description": c[2]} for c in PL.CATALOGUE], "configured": req.constraints.model_dump(mode="json") | {"plan": pc.model_dump(mode="json")}, "recommended_checks": next((a["constraint_checks"] for a in alts if a["recommended"]), None)}  # fmt: skip
    sens = None
    if req.sensitivity.enabled and not _inner:
        sens = _sensitivity(snap, req, limits, stop, rec_id, alts, oids)
    status = "completed_with_warnings" if any(x["severity"] in ("WARNING", "ERROR") for x in warnings) else "completed"
    rec_row = next((a for a in alts if a["recommended"]), None)
    out = _result(status, objectives=obj_block, constraints=cons_block, scenarios=sc_block, alternatives=alts, pareto=pareto_block, recommendation=None if rec_row is None else {"id": rec_row["id"], "basis": req.strategy, "allocation": rec_row["allocation"], "values": rec_row["values"], "decision_support_only": True, "statement": STATEMENTS["decision_support"]}, comparison=comparison, sensitivity=sens, explanation=explanation, warnings=warnings, statistics={**stats, "anchors": anchors, "inexact": inexact, "elapsed_ms": int((time.monotonic() - t0) * 1000)})  # fmt: skip
    out["inexact"] = inexact
    out["recommendation_hash"] = stable_hash({"status": status, "rec": None if rec_row is None else [(x["offer_uid"], round(x["quantity"], 6)) for x in rec_row["allocation"]], "front": sorted(front_ids)})  # fmt: skip
    return out


def _sensitivity(snap: dict[str, Any], req: ResilienceRequest, limits: Limits, stop: Any, base_id: str | None, base_alts: list[dict[str, Any]], oids: list[str]) -> dict[str, Any]:  # fmt: skip
    cfg = req.sensitivity
    variants: list[dict[str, Any]] = []
    base_rec = next((a for a in base_alts if a["recommended"]), None)
    if req.strategy == "WEIGHTED_SUM" and cfg.weights:
        for o in req.objectives:
            if o.id in oids and o.weight > 0:
                for f in (0.5, 2.0):
                    r2 = req.model_copy(update={"objectives": [ob.model_copy(update={"weight": ob.weight * f}) if ob.id == o.id else ob for ob in req.objectives]})  # fmt: skip
                    variants.append(
                        {"parameter": "weight", "label": f"weight of {o.id} x{f:g}", "snap": snap, "req": r2}
                    )
    for m in cfg.demand_multipliers:
        s2 = copy.deepcopy(snap)
        s2["demand"] = snap["demand"] * m
        variants.append({"parameter": "demand", "label": f"demand x{m:g}", "snap": s2, "req": req})
    for m in cfg.capacity_multipliers:
        s2 = copy.deepcopy(snap)
        for s in s2["suppliers"]:
            if s["capacity"] is not None:
                s["capacity"] *= m
        for sc in s2["scenarios"]:
            for e in sc.get("effects", {}).values():
                if e.get("capacity") is not None:
                    e["capacity"] *= m
        variants.append({"parameter": "capacity", "label": f"all supplier capacities x{m:g}", "snap": s2, "req": req})
    for d in cfg.add_lead_days:
        s2 = copy.deepcopy(snap)
        for s in s2["suppliers"]:
            if s["lead_days"] is not None:
                s["lead_days"] += d
        for sc in s2["scenarios"]:
            for e in sc.get("effects", {}).values():
                if e.get("lead_days") is not None:
                    e["lead_days"] += d
        variants.append({"parameter": "lead_time", "label": f"all lead times +{d:g} d", "snap": s2, "req": req})
    for m in cfg.budget_multipliers:
        if req.plan_constraints.budget_ceiling is not None:
            r2 = req.model_copy(
                update={
                    "plan_constraints": req.plan_constraints.model_copy(
                        update={"budget_ceiling": req.plan_constraints.budget_ceiling * m}
                    )
                }
            )
            variants.append({"parameter": "budget", "label": f"budget ceiling x{m:g}", "snap": snap, "req": r2})
    for t in cfg.service_targets:
        key = "min_worst_case_fulfilment" if req.stress else "min_baseline_fulfilment"
        r2 = req.model_copy(update={"plan_constraints": req.plan_constraints.model_copy(update={key: t})})
        variants.append({"parameter": "service_target", "label": f"{key} = {t:g}", "snap": snap, "req": r2})
    skipped = max(0, len(variants) - limits.max_sensitivity_variants)
    variants = variants[: limits.max_sensitivity_variants]
    rows = []
    base_key = None if base_rec is None else [(x["offer_uid"], round(x["quantity"], 4)) for x in base_rec["allocation"]]
    truncated = False
    for v in variants:
        try:
            if stop:
                stop()
            res = compute(v["snap"], v["req"], limits, stop=stop, _inner=True)
        except PL.Budget:
            truncated = True
            break
        rec = res.get("recommendation")
        key = None if not rec else [(x["offer_uid"], round(x["quantity"], 4)) for x in rec["allocation"]]
        rows.append({"parameter": v["parameter"], "label": v["label"], "result_status": res["result_status"], "feasible": res["result_status"] in ("completed", "completed_with_warnings"), "same_as_base": key is not None and key == base_key, "recommended_id": rec["id"] if rec else None, "allocation": rec["allocation"] if rec else None, "values": rec["values"] if rec else None})  # fmt: skip
    ok_rows = [r for r in rows if r["allocation"]]
    used_sets = [{a["offer_uid"] for a in r["allocation"]} for r in ok_rows]
    stable = sorted(set.intersection(*used_sets, {a["offer_uid"] for a in (base_rec or {"allocation": []})["allocation"]})) if used_sets and base_rec else []  # fmt: skip
    return {"base_recommended": None if not base_rec else base_rec["id"], "variants": rows, "variants_skipped_by_limit": skipped, "stopped_by_time_budget": truncated, "stable_suppliers": stable, "first_infeasible": [r["label"] for r in rows if not r["feasible"]], "note": "Each variant re-solves the whole problem with one input changed. The base recommendation 'is stable' only for the changes listed; no other range is implied.", "bounded": {"max_variants": limits.max_sensitivity_variants}}  # fmt: skip
