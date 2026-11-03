"""Replenishment engine: a pure function of an input snapshot and a request (no database, no clock, seeded randomness).

Pipeline: inventory position -> demand and lead-time model -> safety stock -> policy parameters (s, Q, S) under hard constraints ->
candidate policies evaluated by paired simulation -> comparison with the current policy -> projection -> disruption what-ifs and
mitigations (applied to copies) -> explanation. 'Best' means lowest simulated relevant cost among the listed candidates that meet the
constraints and the service target (see ``config.STATEMENTS['optimal']``).
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from typing import Any

from app.inventory import calc
from app.invopt import policies as pol
from app.invopt import safety
from app.invopt import simulate as sim
from app.invopt.config import STATEMENTS, Limits, ReplenishRequest, WhatIf

EPS = 1e-9
r6 = safety.r6


def _flag(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


# ---------------------------------------------------------------------------------------------------------- inputs


def _effective_costs(snap: dict[str, Any]) -> sim.SimCosts:
    v = snap["costs"]["values"]
    return sim.SimCosts(v["holding_cost_per_unit_day"], v["ordering_cost"], v["shortage_cost_per_unit"], v["unit_cost"])


def _constraints(snap: dict[str, Any], req: ReplenishRequest) -> dict[str, Any]:
    c, offer = req.constraints, snap.get("offer")
    off = offer if offer and not offer.get("ambiguous") else {}
    notes: list[str] = []
    moq = max(c.min_order_quantity or 0.0, off.get("moq") or 0.0) or None
    lots = [x for x in (c.order_increment, off.get("lot_size")) if x]
    lot = None
    if len(lots) == 2 and lots[0] != lots[1]:
        a, b = lots
        if abs(a - round(a)) < 1e-9 and abs(b - round(b)) < 1e-9:
            lot = float(a * b // math.gcd(int(round(a)), int(round(b))))
            notes.append(
                f"The request increment {a:g} and the offer lot size {b:g} differ; their least common multiple {lot:g} satisfies both."
            )
        else:
            lot = max(a, b)
            notes.append(f"The request increment {a:g} and the offer lot size {b:g} differ and are not whole numbers; the larger is used and may not satisfy both.")  # fmt: skip
    elif lots:
        lot = lots[0]
    caps = [x for x in (c.max_order_quantity, off.get("max_allocation")) if x]
    storage = c.storage_capacity or snap["costs"].get("storage_capacity")
    return {"moq": moq, "lot": lot, "max_q": min(caps) if caps else None, "storage_capacity": storage, "budget": c.budget_per_order, "required_within_days": c.required_within_days, "notes": notes, "offer_eligible": off.get("eligible") if off else None, "ineligible_reasons": off.get("ineligible_reasons", []) if off else []}  # fmt: skip


def _demand_model(snap: dict[str, Any], lead: float | None, horizon: int, dq: list) -> dict[str, Any]:
    obs = snap["demand"]["observed"]
    fc = snap["demand"]["forecast"]
    st = obs.get("stats") or {}
    out: dict[str, Any] = {"mean": obs.get("rate_per_day"), "sd": st.get("sd_per_day"), "source": "OBSERVED_HISTORY" if obs.get("rate_per_day") is not None else None, "intermittent": bool(st.get("intermittent")), "resolution_days": st.get("resolution_days"), "daily": obs.get("daily"), "series": None, "forecast": None, "sd_source": "OBSERVED_HISTORY" if st.get("sd_per_day") is not None else None, "observed_mean": obs.get("rate_per_day")}  # fmt: skip
    if fc and fc.get("status") == "usable" and fc.get("series"):
        series = fc["series"]
        span = max(1, math.ceil((lead or 1) + 1))
        out["mean"] = sum(series[:span]) / len(series[:span])
        out["series"] = series
        out["source"] = "FORECAST"
        out["forecast"] = fc["block"]
        if len(series) < span:
            dq.append(
                _flag(
                    "FORECAST_SHORTER_THAN_PROTECTION",
                    "WARNING",
                    f"The forecast covers {len(series)} days, less than the {span}-day protection interval.",
                )
            )
        if fc.get("sigma_per_day") is not None:
            out["sd"], out["sd_source"] = fc["sigma_per_day"], "FORECAST_INTERVAL"
    if (
        out["sd"] is not None
        and out["mean"]
        and out["mean"] > 0
        and out["sd"] > 0.5 * out["mean"]
        and not out["intermittent"]
    ):
        dq.append(_flag("DEMAND_HIGH_VARIABILITY", "INFO", "Daily demand is highly variable (sd above half the mean); a normal model truncated at zero understates the mean, so treat simulated service as approximate."))  # fmt: skip
    return out


# --------------------------------------------------------------------------------------------------------- design


def _design(*, kind: str, d: float, sd: float | None, lead: float, lead_sd: float | None, review: int, daily: list[float] | None, resolution: int | None, intermittent: bool, lead_samples: list[float], service_type: str, target: float, q: float | None, method: str, seed: int, min_boot: int, stats: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    """Safety stock and reorder level from the supplied statistics only (used for both the live design and the train-only backtest)."""
    eff_review = (
        review if kind == "R_S" else 1
    )  # continuous review is simulated as daily review: the protection interval is L + 1
    demand_stats = {"mean_per_day": d, "sd_per_day": sd, "intermittent": intermittent, "resolution_days": resolution}
    chosen, why = safety.choose_method(requested=method, demand=demand_stats, lead_sd_known=lead_sd is not None, daily_len=len(daily or []) if resolution == 1 else 0, min_bootstrap_days=min_boot)  # fmt: skip
    if chosen is None:
        return {"ok": False, "reason": why, "method": None}
    if chosen == "BOOTSTRAP":
        samples = lead_samples or [lead]
        res = safety.bootstrap_safety_stock(
            daily=daily or [],
            lead_samples=samples,
            review=eff_review,
            service_type=service_type,
            target=target,
            q=q,
            seed=seed,
        )
    else:
        res = safety.normal_safety_stock(d=d, sd=sd or 0.0, lead=lead, lead_sd=lead_sd if chosen == "NORMAL_LT" else None, review=eff_review, service_type=service_type, target=target, q=q)  # fmt: skip
    if res.get("value") is None:
        return {"ok": False, "reason": res.get("reason"), "method": chosen}
    mean_prot = d * (lead + eff_review)
    res["method"] = chosen
    return {"ok": True, "ss": res["value"], "mean_protection": mean_prot, "protection_days": lead + eff_review, "detail": res, "method": chosen, "review_used": eff_review, "level": mean_prot + res["value"], "reason": None}  # fmt: skip


def _bounds(cons: dict[str, Any], snap: dict[str, Any], on_hand: float, on_order: float) -> dict[str, Any]:
    cap = cons["storage_capacity"]
    headroom = None if cap is None else cap - on_hand - on_order
    return pol.bounds(moq=cons["moq"], lot=cons["lot"], max_q=cons["max_q"], storage_headroom=headroom, budget=cons["budget"], unit_cost=snap["costs"]["values"]["unit_cost"])  # fmt: skip


def _policy_from(kind: str, design_level: float, mean_prot: float, ss: float, q: float | None, review: int, cons: dict[str, Any]) -> tuple[sim.SimPolicy | None, list[str]]:  # fmt: skip
    """Build the simulated policy; the storage capacity caps the order-up-to level and the reorder level plus Q."""
    flags: list[str] = []
    cap = cons["storage_capacity"]
    s = mean_prot + ss
    if kind == "R_S":
        big_s = s
        if cap is not None and big_s > cap:
            big_s = cap
            flags.append("S_CAPPED_BY_STORAGE")
        return sim.SimPolicy(
            "R_S", s=0.0, big_s=big_s, review=review, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"]
        ), flags
    if cap is not None and s >= cap:
        return None, ["REORDER_LEVEL_EXCEEDS_STORAGE"]
    qq = q or 0.0
    if cap is not None and s + qq > cap:
        qq = max(0.0, cap - s)
        flags.append("Q_CAPPED_BY_STORAGE")
    if qq <= EPS:
        return None, flags + ["NO_ROOM_TO_ORDER"]
    if kind == "S_Q":
        return sim.SimPolicy("S_Q", s=s, q=qq, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"]), flags
    return sim.SimPolicy(
        "S_UPTO", s=s, big_s=s + qq, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"]
    ), flags


def _policy_dict(p: sim.SimPolicy) -> dict[str, Any]:
    return {"type": p.kind, "reorder_point": r6(p.s) if p.kind != "R_S" else None, "order_quantity": r6(p.q), "order_up_to": r6(p.big_s), "review_period_days": p.review if p.kind == "R_S" else 1, "min_order_quantity": p.moq or None, "order_increment": p.lot, "max_order_quantity": p.max_q}  # fmt: skip


def _current_policy(snap: dict[str, Any], req: ReplenishRequest, cons: dict[str, Any], d: float | None, lead: float | None, rec_q: float | None) -> tuple[sim.SimPolicy | None, dict[str, Any]]:  # fmt: skip
    cp = snap.get("policy")
    info: dict[str, Any] = {"available": False, "policy_id": None, "reason": None, "label": None}
    if not cp:
        info["reason"] = "No valid current inventory policy is registered."
        return None, info
    info["policy_id"], info["policy_version"] = cp["id"], cp["version"]
    rop, label = cp.get("reorder_point"), "registered reorder point"
    if rop is None and cp.get("safety_stock") is not None and d is not None and (cp.get("lead_time_days") or lead):
        rop, label = (
            d * (cp.get("lead_time_days") or lead) + cp["safety_stock"],
            "computed from registered safety stock: demand x lead time + safety stock",
        )
    if rop is None:
        info["reason"] = "The current policy has neither a reorder point nor a safety stock to compare."
        return None, info
    big = cp.get("maximum") if cp.get("maximum") is not None else cp.get("target")
    kind_label = (
        "(s, S) with the registered reorder point and maximum"
        if cp.get("maximum") is not None
        else "(s, S) with the registered reorder point and target"
    )
    if big is None:
        if not rec_q:
            info["reason"] = (
                "The current policy states no maximum or target and no order quantity is available to complete it."
            )
            return None, info
        big = rop + rec_q
        kind_label = "HYBRID: registered reorder point with the recommended order quantity (the registered policy states no maximum or target)"
    if big <= rop:
        info["reason"] = "The registered maximum/target is not above the reorder point."
        return None, info
    info.update(
        available=True, label=f"{kind_label}; reorder point {label}", reorder_point=r6(rop), order_up_to=r6(big)
    )
    return sim.SimPolicy("S_UPTO", s=rop, big_s=big, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"]), info


# ------------------------------------------------------------------------------------------------------- sampling


def _samplers(dm: dict[str, Any], lead: float, lead_sd: float | None, samples: list[float], d_mult: float = 1.0, lt_extra: float = 0.0, lt_mult: float = 1.0) -> tuple[Callable, Callable]:  # fmt: skip
    mean, sd, daily, series = dm["mean"] * d_mult, (dm["sd"] or 0.0) * d_mult, dm.get("daily"), dm.get("series")

    def demand(rng: random.Random, days: int) -> list[float]:
        if dm["intermittent"] and daily and dm.get("resolution_days") == 1:
            return [rng.choice(daily) * d_mult for _ in range(days)]
        out = []
        for t in range(days):
            m = (series[t] * d_mult) if series and t < len(series) else mean
            out.append(max(0.0, rng.gauss(m, sd)) if sd > 0 else m)
        return out

    def lead_s(rng: random.Random) -> int:
        if len(samples) >= 8:
            x = samples[rng.randrange(len(samples))]
        elif lead_sd:
            x = rng.gauss(lead, lead_sd)
        else:
            x = lead
        return max(1, int(round(x * lt_mult + lt_extra)))

    return demand, lead_s


def _pipeline(snap: dict[str, Any], slip: int = 0) -> list[tuple[int, float]]:
    return [(r["day"] + slip, r["quantity"]) for r in snap["receipts"]["counted"]]


# ------------------------------------------------------------------------------------------------------ metrics


def _brief(agg: dict[str, Any]) -> dict[str, Any]:
    return {k: agg[k] for k in ("fill_rate", "cycle_service_level", "stockout_days", "stockout_events", "units_short", "avg_on_hand", "orders", "turnover", "costs", "n_without_value")}  # fmt: skip


def _mean(m: dict[str, Any] | None) -> float | None:
    return None if m is None else m.get("mean")


def _meets(agg: dict[str, Any], service_type: str, target: float) -> bool:
    v = _mean(agg["cycle_service_level"] if service_type == "CYCLE" else agg["fill_rate"])
    return v is not None and v + 1e-12 >= target


def _score(agg: dict[str, Any], has_cost: bool) -> float:
    c = agg["costs"].get("known_subtotal")
    return (c["mean"] if c else agg["avg_on_hand"]["mean"]) if has_cost else agg["avg_on_hand"]["mean"]


# ------------------------------------------------------------------------------------------------ main computation


def compute(snap: dict[str, Any], req: ReplenishRequest, limits: Limits, *, stop: Callable[[], None] | None = None) -> dict[str, Any]:  # fmt: skip
    dq: list[dict[str, str]] = list(snap.get("data_quality", []))
    warnings: list[dict[str, str]] = []
    stopper = stop or (lambda: None)
    pos, lt = snap["position"], snap["lead_time"]
    result: dict[str, Any] = {"result_status": "not_solved", "position": None, "demand": None, "lead_time": lt, "safety_stock": None, "recommendation": None, "projection": None, "evaluation": None, "scenario_results": None, "mitigations": None, "infeasibility": [], "costs": None, "explanation": [], "warnings": warnings, "data_quality": dq, "statements": {k: STATEMENTS[k] for k in ("decision_support", "service", "optimal", "cost", "missing")}}  # fmt: skip
    # ---- inventory position
    on_order = sum(r["quantity"] for r in snap["receipts"]["counted"])
    avail = pos.get("available")
    backlog = pos.get("backordered")
    position: dict[str, Any] = {"status": pos["status"], "unit": snap["unit"], "on_hand_reported": pos.get("on_hand"), "usable": pos.get("usable"), "available": avail, "backordered": backlog, "on_order_counted": r6(on_order), "inventory_position": None, "stale": pos.get("stale"), "age_days": pos.get("age_days"), "formula": "inventory position = available - backordered + dated open receipts counted (in-transit on the record is never added to on-hand or double-counted)", "receipts_counted": snap["receipts"]["counted"], "receipts_excluded": snap["receipts"]["excluded"], "backordered_note": None}  # fmt: skip
    if backlog is None and avail is not None:
        position["backordered_note"] = (
            "Backorders are not reported; they are treated as unknown, and the inventory position excludes them (it may be overstated)."
        )
        dq.append(_flag("BACKORDERS_UNKNOWN", "INFO", position["backordered_note"]))
    if avail is not None:
        position["inventory_position"] = r6(avail - (backlog or 0.0) + on_order)
    result["position"] = position
    # ---- lead time and demand
    L = lt.get("days")
    dm = _demand_model(snap, L, req.horizon_days, dq)
    result["demand"] = {k: dm[k] for k in ("mean", "sd", "source", "sd_source", "intermittent", "resolution_days", "observed_mean", "forecast")} | {"unit": snap["unit"], "window": snap["demand"]["observed"].get("window"), "coverage_ratio": snap["demand"]["observed"].get("coverage_ratio"), "statement": "Mean is per day. Missing days are not read as zero."}  # fmt: skip
    problems = [i["message"] for i in dq if i["code"] in ("INVENTORY_MISSING", "INVENTORY_MISSING_INVENTORY", "LEAD_TIME_UNKNOWN") or i["code"].startswith("INVENTORY_") and i["severity"] == "ERROR"]  # fmt: skip
    if dm["mean"] is None:
        problems.append("Demand per day is not available (no usable observations or forecast); it is not assumed.")
    if L is None:
        problems.append("Lead time is not available; it is not assumed.")
    if avail is None:
        problems.append("Inventory is not available at the as-of time; it is not assumed.")
    cons = _constraints(snap, req)
    result["constraints"] = cons
    if problems:
        result["result_status"] = "incomplete"
        result["infeasibility"] = [{"code": "MISSING_INPUT", "message": m} for m in dict.fromkeys(problems)]
        result["explanation"] = [
            {
                "step": "inputs",
                "text": "A recommendation needs inventory, demand and lead time. " + " ".join(dict.fromkeys(problems)),
            }
        ]
        return result
    assert L is not None and avail is not None and dm["mean"] is not None  # narrowing for type checkers
    d, ip = dm["mean"], position["inventory_position"]
    lead_sd = lt.get("std")
    # ---- eligibility and deadline (hard)
    infeasible: list[dict[str, Any]] = []
    if cons["offer_eligible"] is False:
        infeasible.append(
            {
                "code": "SUPPLIER_NOT_ELIGIBLE",
                "message": "The registered offer is not usable: " + "; ".join(cons["ineligible_reasons"]) + ".",
            }
        )
    if cons["required_within_days"] is not None and L > cons["required_within_days"] + EPS:
        infeasible.append({"code": "LEAD_TIME_EXCEEDS_DEADLINE", "message": f"The lead time ({L:g} days, {lt['source']}) is longer than the {cons['required_within_days']:g}-day requirement; no replenishment can arrive in time without expediting, which is not modelled."})  # fmt: skip
    kind, review = req.policy_type, req.review_period_days
    # ---- order quantity
    b = _bounds(cons, snap, pos.get("on_hand") or avail, on_order)
    if b["unverified"]:
        for u in b["unverified"]:
            warnings.append(_flag("CONSTRAINT_UNVERIFIED", "WARNING", f"Not enforced: {u}."))
    for n in cons["notes"]:
        warnings.append(_flag("CONSTRAINT_NOTE", "INFO", n))
    if b["infeasible"]:
        infeasible.append(
            {
                "code": "CONSTRAINTS_CONFLICT",
                "message": "The hard order constraints leave no feasible quantity.",
                "details": b["infeasible"],
            }
        )
    cv = snap["costs"]["values"]
    q_info: dict[str, Any] = {"method": None, "raw": None, "eoq": None, "reason": None}
    if req.order_quantity_method == "FIXED":
        q_raw, q_info["method"] = req.fixed_order_quantity, "FIXED"
    else:
        e = pol.eoq(d, cv["ordering_cost"], cv["holding_cost_per_unit_day"], intermittent=dm["intermittent"])
        q_info["eoq"] = e
        if e["value"] is not None:
            q_raw, q_info["method"] = e["value"], "EOQ"
        elif req.order_quantity_method == "EOQ":
            q_raw, q_info["method"], q_info["reason"] = None, "EOQ_UNAVAILABLE", e["reason"]
        else:
            q_raw = max(d * max(L, 1.0), EPS)
            q_info["method"] = "LEAD_TIME_COVER"
            q_info["reason"] = (
                "EOQ was not applicable ("
                + (e["reason"] or "")
                + "); the order quantity is set to one lead time of demand. This is a labelled heuristic, not a cost-optimal quantity."
            )
            warnings.append(_flag("ORDER_QUANTITY_HEURISTIC", "INFO", q_info["reason"]))
    if q_raw is None:
        infeasible.append(
            {
                "code": "ORDER_QUANTITY_UNAVAILABLE",
                "message": q_info["reason"] or "No order quantity can be determined.",
            }
        )
        q_fit: dict[str, Any] = {"quantity": None, "status": "INFEASIBLE", "flags": [], "reasons": []}
        q = None
    else:
        q_fit = pol.fit_quantity(q_raw, b)
        q = q_fit["quantity"] if q_fit["status"] != "INFEASIBLE" else None
        q_info["raw"], q_info["fitted"] = r6(q_raw), q_fit
    result["order_quantity"] = q_info
    # ---- safety stock
    seed = req.seed if req.seed is not None else int(snap["hash"][:8], 16) % (2**31 - 1)
    samples = lt.get("samples") or []
    st = snap["demand"]["observed"].get("stats") or {}
    design = _design(kind=kind, d=d, sd=dm["sd"], lead=L, lead_sd=lead_sd, review=review, daily=dm["daily"], resolution=dm["resolution_days"], intermittent=dm["intermittent"], lead_samples=samples, service_type=req.service_type, target=req.service_target, q=q, method=req.safety_stock_method, seed=seed, min_boot=limits.min_bootstrap_days, stats=st)  # fmt: skip
    cp_sim, cp_info = _current_policy(snap, req, cons, d, L, q)
    result["safety_stock"] = {"requested_method": req.safety_stock_method, "service_type": req.service_type, "service_target": req.service_target, "statement": STATEMENTS["safety_stock"], "current": (snap.get("policy") or {}).get("safety_stock"), **({"calculated": design["detail"], "method": design["method"], "value": design["ss"], "mean_protection_demand": r6(design["mean_protection"]), "protection_days": r6(design["protection_days"]), "reorder_level": r6(design["level"])} if design["ok"] else {"calculated": None, "method": design.get("method"), "value": None, "reason": design["reason"]})}  # fmt: skip
    if not design["ok"]:
        result["result_status"] = "infeasible" if infeasible else "incomplete"
        result["infeasibility"] = infeasible + [{"code": "SAFETY_STOCK_UNAVAILABLE", "message": design["reason"]}]
        result["explanation"] = [
            {
                "step": "safety_stock",
                "text": design["reason"] + " No policy is recommended; the missing input is never replaced by zero.",
            }
        ]
        _attach_projection(result, snap, dm, ip, avail, None, None, d, L, lead_sd, req)
        return result
    # ---- candidates
    reps_cap = req.replications
    n_cands_est = 18
    days = req.simulation_days
    mode_req = req.evaluation
    daily = dm["daily"] if dm["resolution_days"] == 1 and dm["daily"] else None
    has_sim_model = dm["sd"] is not None or dm["intermittent"]
    want_mc = mode_req in ("AUTO", "MONTE_CARLO") and has_sim_model and not infeasible
    want_bt = mode_req in ("AUTO", "BACKTEST") and daily is not None and len(daily) >= 56
    if mode_req == "MONTE_CARLO" and not has_sim_model:
        warnings.append(
            _flag(
                "SIMULATION_NOT_POSSIBLE",
                "WARNING",
                "Monte Carlo needs a demand variability estimate; none is available, so the analytic recommendation is shown without simulation.",
            )
        )
    if mode_req == "BACKTEST" and not want_bt:
        warnings.append(
            _flag(
                "BACKTEST_NOT_POSSIBLE",
                "WARNING",
                "A backtest needs at least 56 days of daily demand history; the analytic recommendation is shown without a backtest.",
            )
        )
    scen_list = _scenarios(snap, req)
    runs_total = n_cands_est + 1 + 5 * len(scen_list)
    reps = max(0, min(reps_cap, limits.max_sim_steps // max(1, days * runs_total)))
    budget_note = None
    if want_mc and reps < min(reps_cap, 10):
        want_mc = False
        warnings.append(_flag("SIMULATION_BUDGET", "WARNING", f"The request would need more than the {limits.max_sim_steps:,} simulated days allowed; reduce simulation_days, replications or scenarios. No simulation was run."))  # fmt: skip
    elif want_mc and reps < reps_cap:
        budget_note = f"replications reduced from {reps_cap} to {reps} to stay within the {limits.max_sim_steps:,}-day simulation budget"
        warnings.append(_flag("SIMULATION_REDUCED", "INFO", budget_note))
    costs = _effective_costs(snap)
    has_cost = any(v is not None for v in (costs.holding, costs.ordering, costs.shortage))
    if req.objective == "MIN_TOTAL_COST" and (costs.holding is None or costs.shortage is None):
        result["result_status"] = "not_solved"
        result["infeasibility"] = infeasible + [{"code": "COST_OBJECTIVE_NEEDS_COSTS", "message": "MIN_TOTAL_COST needs at least a holding cost and a shortage cost; neither is assumed. Supply them or use the SERVICE_TARGET objective."}]  # fmt: skip
        result["explanation"] = [{"step": "objective", "text": result["infeasibility"][-1]["message"]}]
        return result
    dem_s, lead_s = _samplers(dm, L, lead_sd, samples)
    ss0, mean_prot = design["ss"], design["mean_protection"]
    cands: list[dict[str, Any]] = []
    searching = want_mc and req.proposed is None
    ss_mults = [1.0, 0.6, 0.8, 1.2, 1.5, 2.0] if searching else [1.0]
    q_mults = [1.0, 0.5, 2.0] if (searching and kind != "R_S" and req.order_quantity_method != "FIXED") else [1.0]
    invalid: list[dict[str, Any]] = []
    for sm in ss_mults:
        for qm in q_mults:
            qq = None
            if kind != "R_S":
                if q is None:
                    continue
                qf = pol.fit_quantity(q * qm, b)
                if qf["status"] == "INFEASIBLE" or not qf["quantity"]:
                    continue
                qq = qf["quantity"]
            p, fl = _policy_from(kind, design["level"], mean_prot, ss0 * sm, qq, review, cons)
            if p is None:
                invalid.append({"ss_multiplier": sm, "q_multiplier": qm, "flags": fl})
                continue
            if any(c["policy"] == _policy_dict(p) for c in cands):
                continue
            cands.append(
                {
                    "id": f"C{len(cands) + 1}",
                    "ss_multiplier": sm,
                    "q_multiplier": qm,
                    "ss": r6(ss0 * sm),
                    "flags": fl,
                    "policy": _policy_dict(p),
                    "_p": p,
                }
            )
    if req.proposed is not None:
        pr = req.proposed
        mk = {"S_Q": lambda: sim.SimPolicy("S_Q", s=pr.reorder_point, q=pr.order_quantity, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"]), "S_UPTO": lambda: sim.SimPolicy("S_UPTO", s=pr.reorder_point, big_s=pr.order_up_to, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"]), "R_S": lambda: sim.SimPolicy("R_S", s=0.0, big_s=pr.order_up_to, review=review, moq=cons["moq"] or 0.0, lot=cons["lot"], max_q=cons["max_q"])}[kind]()  # fmt: skip
        pfl = []
        cap = cons["storage_capacity"]
        top = mk.big_s if mk.big_s is not None else mk.s + (mk.q or 0.0)
        if cap is not None and top > cap + EPS:
            pfl.append("EXCEEDS_STORAGE_CAPACITY")
            infeasible.append(
                {
                    "code": "PROPOSED_EXCEEDS_STORAGE",
                    "message": f"The proposed policy can hold up to {top:g}, above the storage capacity {cap:g}.",
                }
            )
        implied = (mk.s if kind != "R_S" else (mk.big_s or 0.0)) - mean_prot
        cands = [{"id": "PROPOSED", "ss_multiplier": None, "q_multiplier": None, "ss": r6(implied), "flags": pfl, "policy": _policy_dict(mk), "_p": mk}, *[dict(c, id=f"REF{i + 1}") for i, c in enumerate(cands[:1])]]  # fmt: skip
    if not cands:
        result["result_status"] = "infeasible"
        result["infeasibility"] = infeasible + [{"code": "NO_FEASIBLE_POLICY", "message": "No candidate policy satisfies the order and storage constraints.", "details": invalid}]  # fmt: skip
        result["explanation"] = [{"step": "constraints", "text": result["infeasibility"][-1]["message"]}]
        _attach_projection(result, snap, dm, ip, avail, None, None, d, L, lead_sd, req)
        return result
    # ---- evaluate
    evaluation: dict[str, Any] = {"mode": "NONE", "synthetic": False, "statement": STATEMENTS["simulation"], "assumptions": [], "candidates": [], "selected": None, "analytic_id": cands[0]["id"], "current": None, "recommended": None, "comparison": None, "backtest": None, "budget": {"replications": reps if want_mc else 0, "days": days, "candidates": len(cands), "max_sim_steps": limits.max_sim_steps, "note": budget_note}, "seed": seed}  # fmt: skip
    chosen = cands[0]
    pipe = _pipeline(snap)
    on_hand0 = max(0.0, avail)
    backlog0 = backlog or 0.0
    if want_mc:
        evaluation.update(mode="MONTE_CARLO", synthetic=True)
        evaluation["assumptions"] = ["SYNTHETIC demand paths sampled from the estimated demand model (" + ("empirical resampling of observed days" if dm["intermittent"] else "normal, truncated at zero, independent days") + ")", "lead time resampled from " + ("measured deliveries" if len(samples) >= 8 else "a normal model" if lead_sd else "the single lead-time value"), "order arrives L days after the order day; stock is unusable before then", "unmet demand is " + ("backordered" if req.backorders else "lost"), "no shelf-life, expiry, minimum shelf life or multi-echelon effects are modelled"]  # fmt: skip
        outs: list[tuple[dict, dict]] = []
        for c in cands:
            agg = sim.monte_carlo(c["_p"], days=days, reps=reps, seed=seed, demand_sampler=dem_s, lead_sampler=lead_s, on_hand0=on_hand0, pipeline0=pipe, backlog0=backlog0, backorders=req.backorders, costs=costs, stop=stopper)  # fmt: skip
            outs.append((c, agg))
            evaluation["candidates"].append({"id": c["id"], "ss_multiplier": c["ss_multiplier"], "q_multiplier": c["q_multiplier"], "safety_stock": c["ss"], "policy": c["policy"], "flags": c["flags"], "meets_service_target": _meets(agg, req.service_type, req.service_target), "metrics": _brief(agg)})  # fmt: skip
        ok = [(c, a) for c, a in outs if _meets(a, req.service_type, req.service_target)]
        target_met = bool(ok)
        if req.proposed is not None:
            pick = outs[0]
            target_met = _meets(pick[1], req.service_type, req.service_target)
        elif req.objective == "MIN_TOTAL_COST":
            pick = min(outs, key=lambda ca: _score(ca[1], True))
            target_met = _meets(pick[1], req.service_type, req.service_target)
        elif ok:
            pick = min(ok, key=lambda ca: (_score(ca[1], has_cost), ca[0]["ss_multiplier"]))
        else:
            sv = "cycle_service_level" if req.service_type == "CYCLE" else "fill_rate"
            pick = max(outs, key=lambda ca: _mean(ca[1][sv]) or 0.0)
            warnings.append(_flag("SERVICE_TARGET_NOT_MET", "WARNING", "No listed candidate reaches the service target on the simulated paths (the order, storage or budget limits may prevent it); the highest-service candidate is shown."))  # fmt: skip
        chosen, chosen_agg = pick
        evaluation["selected"] = chosen["id"]
        evaluation["selection_rule"] = ("lowest simulated mean known cost (" + ", ".join(k for k in ("holding", "ordering", "shortage") if getattr(costs, k) is not None) + ") among the candidates meeting the target" if has_cost else "lowest average on-hand among the candidates meeting the target (no cost inputs were supplied, so inventory is the proxy)") if req.objective == "SERVICE_TARGET" else "lowest simulated mean total of the supplied holding, ordering and shortage costs"  # fmt: skip
        evaluation["recommended"] = {"candidate": chosen["id"], "metrics": _brief(chosen_agg), "sample_path": chosen_agg["sample_path"], "sample_orders": chosen_agg["sample_orders"], "meets_service_target": target_met}  # fmt: skip
        if cp_sim is not None:
            cagg = sim.monte_carlo(cp_sim, days=days, reps=reps, seed=seed, demand_sampler=dem_s, lead_sampler=lead_s, on_hand0=on_hand0, pipeline0=pipe, backlog0=backlog0, backorders=req.backorders, costs=costs, stop=stopper)  # fmt: skip
            evaluation["current"] = {
                **cp_info,
                "policy": _policy_dict(cp_sim),
                "metrics": _brief(cagg),
                "sample_path": cagg["sample_path"],
            }
            evaluation["comparison"] = _compare(cagg, chosen_agg)
        else:
            evaluation["current"] = cp_info
    elif cp_sim is not None:
        evaluation["current"] = cp_info
    if want_bt:
        evaluation["backtest"] = _backtest(snap, req, limits, dm, cons, b, q, costs, cp_sim, seed, daily or [])
        if evaluation["mode"] == "NONE":
            evaluation["mode"] = "BACKTEST"
    # ---- recommendation now
    p: sim.SimPolicy = chosen["_p"]
    rec = _recommend_now(kind, p, ip, d, L, b, snap, cons, infeasible, req, pos)
    rec["policy"] = _policy_dict(p)
    rec["safety_stock"] = r6(chosen["ss"])
    rec["service_target"] = {"type": req.service_type, "target": req.service_target}
    result.update(recommendation=rec, evaluation=evaluation)
    cs = _cost_summary(snap, evaluation, rec, costs)
    result["costs"] = cs
    _attach_projection(result, snap, dm, ip, avail, p, rec, d, L, lead_sd, req)
    # ---- scenarios and mitigations (on copies)
    if scen_list and not infeasible:
        result["scenario_results"], result["mitigations"] = _scenario_eval(snap, req, limits, dm, cons, b, q, costs, samples, lead_sd, L, p, cp_sim, pipe, on_hand0, backlog0, scen_list, reps, days, seed, design, kind, review, stopper, cands[0]["ss"])  # fmt: skip
    elif scen_list:
        warnings.append(
            _flag(
                "SCENARIO_SKIPPED",
                "INFO",
                "Disruption scenarios were not evaluated because a hard constraint makes the baseline infeasible.",
            )
        )
    result["infeasibility"] = infeasible
    status = "infeasible" if infeasible else ("simulated" if req.proposed is not None else ("no_order_needed" if rec["status"] == "NO_ORDER" else "recommended"))  # fmt: skip
    result["result_status"] = status
    result["explanation"] = _explain(snap, req, result, dm, design, q_info, chosen, cons)
    result["sensitivity"] = _sensitivity(d, dm["sd"], L, lead_sd, review, kind, req, q)
    return result


# ---------------------------------------------------------------------------------------------------------- pieces


def _compare(cur: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    def dv(a: dict | None, b: dict | None) -> float | None:
        return (
            None
            if a is None or b is None or a.get("mean") is None or b.get("mean") is None
            else r6(b["mean"] - a["mean"])
        )

    same_omitted = cur["costs"]["omitted_components"] == rec["costs"]["omitted_components"]
    kc, kr = cur["costs"].get("known_subtotal"), rec["costs"].get("known_subtotal")
    return {"basis": "paired simulation: both policies saw the same demand and lead-time draws", "fill_rate_change": dv(cur["fill_rate"], rec["fill_rate"]), "cycle_service_level_change": dv(cur["cycle_service_level"], rec["cycle_service_level"]), "units_short_change": dv(cur["units_short"], rec["units_short"]), "avg_on_hand_change": dv(cur["avg_on_hand"], rec["avg_on_hand"]), "orders_change": dv(cur["orders"], rec["orders"]), "known_cost_change": dv(kc, kr) if same_omitted else None, "known_cost_note": None if same_omitted else "cost totals are not compared because the components differ", "omitted_cost_components": rec["costs"]["omitted_components"]}  # fmt: skip


def _recommend_now(kind: str, p: sim.SimPolicy, ip: float, d: float, L: float, b: dict[str, Any], snap: dict[str, Any], cons: dict[str, Any], infeasible: list, req: ReplenishRequest, pos: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    s = p.s if kind != "R_S" else None
    tgt = pol.order_target(kind, ip, s if s is not None else 0.0, p.q, p.big_s)
    out: dict[str, Any] = {"status": "NO_ORDER", "order_now": False, "quantity": 0.0, "flags": [], "inventory_position": r6(ip), "trigger": tgt["trigger"], "rule": tgt.get("rule"), "arrival_after_days": None, "arrival_date": None, "unit": snap["unit"], "purchase_spend": None, "currency": snap["costs"]["currency"], "supplier_id": snap["lead_time"].get("supplier_id"), "decision_support_only": True, "statement": STATEMENTS["decision_support"]}  # fmt: skip
    if kind == "R_S":
        out["rule"] = "at each review: order up to S (this recommendation assumes today is a review day)"
    if infeasible:
        out["status"] = "INFEASIBLE"
        out["flags"] = [i["code"] for i in infeasible]
        return out
    if not tgt["trigger"] or not tgt["target"]:
        return out
    fit = pol.fit_quantity(tgt["target"], b)
    out.update(
        status=fit["status"],
        quantity=fit["quantity"],
        flags=fit["flags"],
        shortfall_vs_target=fit.get("shortfall_vs_target"),
    )
    if fit["status"] in ("ORDER", "ORDER_CAPPED") and fit["quantity"]:
        out["order_now"] = True
        out["arrival_after_days"] = r6(L)
        from datetime import date, timedelta

        out["arrival_date"] = (date.fromisoformat(snap["as_of_date"]) + timedelta(days=math.ceil(L))).isoformat()
        uc = snap["costs"]["values"]["unit_cost"]
        out["purchase_spend"] = None if uc is None else r6(uc * fit["quantity"])
        if cons["required_within_days"] is not None and L > cons["required_within_days"]:
            out["flags"].append("ARRIVES_AFTER_REQUIRED_DATE")
    if fit["status"] == "INFEASIBLE":
        out["flags"] += ["NO_FEASIBLE_QUANTITY"]
    return out


def _attach_projection(result: dict[str, Any], snap: dict[str, Any], dm: dict[str, Any], ip: float, avail: float, p: sim.SimPolicy | None, rec: dict[str, Any] | None, d: float, L: float, lead_sd: float | None, req: ReplenishRequest) -> None:  # fmt: skip
    H = req.horizon_days
    series = dm.get("series")
    dem = [(series[i] if series and i < len(series) else d) for i in range(H)]
    thr = {"reorder_point": (p.s if p and p.kind != "R_S" else None), "safety_stock": None}
    proj = calc.project_series(max(0.0, avail), dem, snap["receipts"]["counted"], thr)
    sd = dm["sd"]
    stock_in_lt = max(0.0, avail) + sum(
        r["quantity"] for r in snap["receipts"]["counted"] if r["day"] <= max(1, math.ceil(L))
    )
    risk: dict[str, Any] = {"value": None, "basis": None}
    if not dm["intermittent"] and sd is not None and d > 0:
        mu, sig = d * L, math.sqrt(L * sd * sd + (d * (lead_sd or 0.0)) ** 2)
        k = (stock_in_lt - mu) / sig if sig > EPS else (math.inf if stock_in_lt >= mu else -math.inf)
        risk = {"value": r6(1 - safety.ND.cdf(k)) if math.isfinite(k) else (0.0 if k > 0 else 1.0), "basis": f"model-based probability that demand over the {L:g}-day lead time exceeds the stock available before any new order can arrive ({stock_in_lt:g}); a normal approximation, not a calibrated probability, and it ignores orders not yet placed", "stock_available_before_new_order": r6(stock_in_lt)}  # fmt: skip
        if not lead_sd:
            risk["basis"] += "; lead-time variability is unknown and excluded"
    days_to_reorder = None
    if p is not None and p.kind != "R_S" and d > 0:
        days_to_reorder = r6(max(0.0, (ip - p.s) / d))
    result["projection"] = {"horizon_days": H, "demand_basis": "forecast series" if series else "constant observed mean", "series": proj["series"], "stockout_within_day": proj["stockout_within_day"], "min_closing": proj["min_closing"], "unfilled_demand": proj["unfilled_demand"], "receipts_counted": proj["receipts_counted"], "days_until_reorder_point": days_to_reorder, "days_until_reorder_note": "(inventory position - s) / demand per day; 0 means a reorder is due now", "stockout_risk_before_next_arrival": risk, "note": "Projection without any new order: opening available stock, dated receipts, and the stated demand. It is conditional, not a forecast of outcomes."}  # fmt: skip


def _cost_summary(snap: dict[str, Any], ev: dict[str, Any], rec: dict[str, Any], costs: sim.SimCosts) -> dict[str, Any]:
    cs = snap["costs"]
    out: dict[str, Any] = {"currency": cs["currency"], "inputs": cs["values"], "provenance": cs["provenance"], "missing_inputs": cs["missing"], "statement": STATEMENTS["cost"], "recommended_policy": None, "current_policy": None, "order_purchase_spend": rec.get("purchase_spend")}  # fmt: skip
    for key, src in (("recommended_policy", ev.get("recommended")), ("current_policy", ev.get("current"))):
        m = src.get("metrics") if src else None
        if m:
            c = m["costs"]
            out[key] = {"holding": c["holding"], "ordering": c["ordering"], "shortage": c["shortage"], "known_subtotal": c["known_subtotal"], "omitted_components": c["omitted_components"], "purchase_spend": c["purchase_spend"], "horizon": "per simulated horizon (synthetic), mean and p05/p95 over replications"}  # fmt: skip
    return out


def _backtest(snap: dict[str, Any], req: ReplenishRequest, limits: Limits, dm: dict[str, Any], cons: dict[str, Any], b: dict[str, Any], q: float | None, costs: sim.SimCosts, cp_sim: sim.SimPolicy | None, seed: int, daily: list[float]) -> dict[str, Any]:  # fmt: skip
    """Parameters are computed from the training days only and then run, unchanged, on the later test days."""
    n = len(daily)
    test_n = max(14, int(n * 0.4))
    train, test = daily[: n - test_n], daily[n - test_n :]
    out: dict[str, Any] = {"status": "skipped", "train_days": len(train), "test_days": len(test), "leakage_guard": "parameters use the training days only; the test days are never read when designing the policy", "limitations": ["lead-time statistics are those known at run time (not restricted to the training window)", "the test path is one realised history, so service results are noisy", "observed days are used in order; gaps in the record are not filled"]}  # fmt: skip
    if len(train) < limits.min_demand_days or len(test) < 14:
        out["reason"] = "Not enough history for a train/test split."
        return out
    des = backtest_design(train, snap, req, limits, q, seed)
    if not des["ok"]:
        out["reason"] = des["reason"]
        return out
    p, _ = _policy_from(
        req.policy_type,
        des["level"],
        des["mean_protection"],
        des["ss"],
        q if req.policy_type != "R_S" else None,
        req.review_period_days,
        cons,
    )
    if p is None:
        out["reason"] = "The train-only design cannot be placed within the storage constraint."
        return out
    lead = snap["lead_time"]["days"]
    samples = snap["lead_time"].get("samples") or []
    rng = random.Random(seed)
    lts = [
        max(1, int(round(samples[rng.randrange(len(samples))] if len(samples) >= 8 else lead)))
        for _ in range(len(test) + 1)
    ]
    start = p.big_s if p.big_s is not None else p.s + (p.q or 0.0)
    res = {}
    for name, pp in (("recommended", p), ("current", cp_sim)):
        if pp is None:
            continue
        r = sim.simulate(
            test,
            pp,
            lambda i: lts[i % len(lts)],
            on_hand0=start,
            backorders=req.backorders,
            costs=costs,
            keep_path=False,
        )
        res[name] = {
            k: r[k]
            for k in (
                "fill_rate",
                "cycle_service_level",
                "stockout_days",
                "units_short",
                "avg_on_hand",
                "orders",
                "turnover",
                "costs",
                "completed_cycles",
            )
        }
    out.update(status="completed", parameters=_policy_dict(p), design=des["method"], safety_stock=r6(des["ss"]), results=res, note="Both policies start the test window with the same stock level and see the same demand and lead times.")  # fmt: skip
    return out


def backtest_design(train: list[float], snap: dict[str, Any], req: ReplenishRequest, limits: Limits, q: float | None, seed: int) -> dict[str, Any]:  # fmt: skip
    """Design a policy from training demand alone (exposed so tests can prove the test window cannot influence it)."""
    stats = safety.demand_stats([(1, x) for x in train])
    lt = snap["lead_time"]
    return _design(kind=req.policy_type, d=stats["mean_per_day"], sd=stats["sd_per_day"], lead=lt["days"], lead_sd=lt.get("std"), review=req.review_period_days, daily=train, resolution=1, intermittent=bool(stats["intermittent"]), lead_samples=lt.get("samples") or [], service_type=req.service_type, target=req.service_target, q=q, method=req.safety_stock_method, seed=seed, min_boot=limits.min_bootstrap_days, stats=stats)  # fmt: skip


# ------------------------------------------------------------------------------------------------------ scenarios


def _scenarios(snap: dict[str, Any], req: ReplenishRequest) -> list[dict[str, Any]]:
    out = []
    sc = snap.get("scenario")
    if sc:
        for w in sc.get("what_ifs", []):
            out.append(
                {
                    "name": w["name"],
                    "what_if": WhatIf(**w["what_if"]),
                    "source": "SCENARIO",
                    "events": w.get("events", []),
                }
            )
    if not req.what_if.is_neutral():
        out.append({"name": "Analyst what-if", "what_if": req.what_if, "source": "REQUEST", "events": []})
    return out[:6]


def _scenario_eval(
    snap,
    req,
    limits,
    dm,
    cons,
    b,
    q,
    costs,
    samples,
    lead_sd,
    L,
    p,
    cp_sim,
    pipe,
    on_hand0,
    backlog0,
    scen_list,
    reps,
    days,
    seed,
    design,
    kind,
    review,
    stopper,
    ss0,
) -> tuple[list, list]:  # noqa: ANN001, C901
    results, mitigations = [], []
    if reps < 5:
        return [
            {"name": s["name"], "status": "not_run", "reason": "simulation budget exhausted"} for s in scen_list
        ], []
    base_dem, base_lead = _samplers(dm, L, lead_sd, samples)
    base = sim.monte_carlo(p, days=days, reps=reps, seed=seed, demand_sampler=base_dem, lead_sampler=base_lead, on_hand0=on_hand0, pipeline0=pipe, backlog0=backlog0, backorders=req.backorders, costs=costs, stop=stopper)  # fmt: skip
    for sc in scen_list:
        w: WhatIf = sc["what_if"]
        dem_s, lead_s = _samplers(
            dm, L, lead_sd, samples, w.demand_multiplier, w.extra_lead_time_days, w.lead_time_multiplier
        )
        start, span = w.supplier_blocked_start_day, w.supplier_blocked_days
        blocked = (lambda t, a=start, n=span: a < t <= a + n) if span else None  # noqa: E731
        pipe_w = [(d_ + (span if (start == 0 and span) else 0), qty) for d_, qty in pipe]
        kw = dict(days=days, reps=reps, seed=seed, demand_sampler=dem_s, lead_sampler=lead_s, on_hand0=on_hand0, backlog0=backlog0, backorders=req.backorders, costs=costs, blocked=blocked, stop=stopper)  # fmt: skip
        as_is = sim.monte_carlo(p, pipeline0=pipe_w, **kw)
        cur = sim.monte_carlo(cp_sim, pipeline0=pipe_w, **kw) if cp_sim is not None else None
        # re-planned: parameters recomputed under the disrupted inputs
        d2 = dm["mean"] * w.demand_multiplier
        sd2 = (dm["sd"] or 0.0) * w.demand_multiplier
        L2 = L * w.lead_time_multiplier + w.extra_lead_time_days
        lsd2 = None if lead_sd is None else lead_sd * w.lead_time_multiplier
        des2 = _design(kind=kind, d=d2, sd=sd2, lead=L2, lead_sd=lsd2, review=review, daily=[x * w.demand_multiplier for x in (dm["daily"] or [])] or None, resolution=dm["resolution_days"], intermittent=dm["intermittent"], lead_samples=[x * w.lead_time_multiplier + w.extra_lead_time_days for x in samples], service_type=req.service_type, target=req.service_target, q=q, method=req.safety_stock_method, seed=seed, min_boot=limits.min_bootstrap_days, stats={})  # fmt: skip
        row: dict[str, Any] = {"name": sc["name"], "source": sc["source"], "events": sc["events"], "what_if": w.model_dump(), "effective": {"demand_per_day": r6(d2), "lead_time_days": r6(L2), "orders_blocked": [start + 1, start + span] if span else None}, "undisrupted_recommended": _brief(base), "recommended_as_is": _brief(as_is), "current_policy": _brief(cur) if cur else None, "degradation": _compare(base, as_is)}  # fmt: skip
        if des2["ok"]:
            p2, fl2 = _policy_from(
                kind, des2["level"], des2["mean_protection"], des2["ss"], q if kind != "R_S" else None, review, cons
            )
            if p2 is not None:
                rp = sim.monte_carlo(p2, pipeline0=pipe_w, **kw)
                row["replanned"] = {
                    "policy": _policy_dict(p2),
                    "safety_stock": r6(des2["ss"]),
                    "metrics": _brief(rp),
                    "flags": fl2,
                }
                mitigations.append({"scenario": sc["name"], "option": "RAISE_SAFETY_STOCK", "description": "Hold the safety stock recomputed under the disrupted demand and lead time.", "policy": _policy_dict(p2), "extra_safety_stock": r6(des2["ss"] - ss0), "metrics": _brief(rp), "improvement_vs_as_is": _compare(as_is, rp)})  # fmt: skip
        else:
            row["replanned"] = {"status": "not_available", "reason": des2["reason"]}
        if span:
            need = d2 * (span + L2) - (on_hand0 + sum(qty for _, qty in pipe_w))
            if need > 0:
                qf = pol.fit_quantity(need, b)
                if qf["status"] in ("ORDER", "ORDER_CAPPED") and qf["quantity"]:
                    ex = sim.monte_carlo(p, pipeline0=[*pipe_w, (max(1, int(round(L2))), qf["quantity"])], **kw)
                    mitigations.append({"scenario": sc["name"], "option": "PLACE_BRIDGING_ORDER_NOW", "description": "Place one order now sized to the demand expected before new supply can arrive; only possible if the supplier is not yet blocked.", "quantity": qf["quantity"], "flags": qf["flags"], "feasible_now": start >= 1, "metrics": _brief(ex), "improvement_vs_as_is": _compare(as_is, ex), "note": "Not modelled: expediting, alternative suppliers (see Sourcing), or inventory transfers." if start >= 1 else "The supplier is blocked from day 1, so this order could not be placed; shown for sizing only."})  # fmt: skip
        results.append(row)
    return results, mitigations


def _sensitivity(d: float, sd: float | None, L: float, lead_sd: float | None, review: int, kind: str, req: ReplenishRequest, q: float | None) -> list[dict[str, Any]]:  # fmt: skip
    out = []
    eff = review if kind == "R_S" else 1
    for name, d2, L2 in (
        ("demand +10%", d * 1.1, L),
        ("demand -10%", d * 0.9, L),
        ("lead time +1 sd" if lead_sd else "lead time +10%", d, L + (lead_sd or 0.1 * L)),
    ):
        r = safety.normal_safety_stock(
            d=d2,
            sd=(sd or 0.0) * (d2 / d),
            lead=L2,
            lead_sd=lead_sd,
            review=eff,
            service_type=req.service_type,
            target=req.service_target,
            q=q,
        )
        if r.get("value") is not None:
            out.append(
                {
                    "change": name,
                    "safety_stock": r["value"],
                    "reorder_level": r6(d2 * (L2 + eff) + r["value"]),
                    "basis": "analytic normal approximation",
                }
            )
    return out


def _explain(snap, req, result, dm, design, q_info, chosen, cons) -> list[dict[str, str]]:  # noqa: ANN001
    lt, rec, pos = snap["lead_time"], result["recommendation"], result["position"]
    u = snap["unit"] or "units"
    ex = [
        {
            "step": "position",
            "text": f"Available stock {pos['available']:g} {u}, backorders {'unknown' if pos['backordered'] is None else format(pos['backordered'], 'g')}, dated open receipts {pos['on_order_counted']:g}: inventory position {pos['inventory_position']:g}.",
        },  # fmt: skip
        {
            "step": "demand",
            "text": f"Demand {dm['mean']:.4g} {u}/day from {dm['source'].replace('_', ' ').lower()}"
            + (
                f" (forecast run {dm['forecast']['forecast_run_id']}, model {dm['forecast'].get('model')})"
                if dm.get("forecast") and dm["forecast"].get("forecast_run_id")
                else ""
            )
            + (
                f", sd {dm['sd']:.4g}/day ({dm['sd_source'].replace('_', ' ').lower()})."
                if dm["sd"] is not None
                else ", variability unknown."
            ),
        },  # fmt: skip
        {
            "step": "lead_time",
            "text": f"Lead time {lt['days']:.4g} days from {lt['source'].replace('_', ' ').lower()} ({lt['provenance'].lower()}"
            + (f", n={lt['n']}" if lt.get("n") else "")
            + ")"
            + (f", sd {lt['std']:.4g}." if lt.get("std") is not None else "; its variability is unknown."),
        },  # fmt: skip
        {
            "step": "safety_stock",
            "text": f"Safety stock {design['ss']:.4g} {u} by the {design['method'].replace('_', ' ').lower()} method for a {req.service_target:.1%} {'cycle service level' if req.service_type == 'CYCLE' else 'fill rate'}; protection interval {design['protection_days']:g} days"
            + (
                " (lead time plus one day: continuous review is evaluated daily)."
                if req.policy_type != "R_S"
                else " (lead time plus the review period)."
            ),
        },  # fmt: skip
    ]
    p = rec["policy"]
    if p["type"] == "R_S":
        ex.append(
            {"step": "policy", "text": f"Every {p['review_period_days']} days order up to {p['order_up_to']:.4g} {u}."}
        )
    else:
        ex.append({"step": "policy", "text": f"Reorder when the inventory position is at or below {p['reorder_point']:.4g} {u}" + (f"; order {p['order_quantity']:.4g} {u} ({q_info['method'].replace('_', ' ').lower()})." if p["type"] == "S_Q" else f", up to {p['order_up_to']:.4g} {u}.")})  # fmt: skip
    if rec["order_now"]:
        ex.append({"step": "action", "text": f"Order {rec['quantity']:.4g} {u} now (arrives in about {rec['arrival_after_days']:g} days)" + (f"; adjustments: {', '.join(rec['flags']).lower().replace('_', ' ')}." if rec["flags"] else ".")})  # fmt: skip
    else:
        ex.append({"step": "action", "text": "No order is triggered now: the inventory position is above the reorder level." if rec["status"] == "NO_ORDER" else "No order can be recommended: " + ", ".join(rec["flags"])})  # fmt: skip
    ev = result["evaluation"]
    if ev["recommended"]:
        m = ev["recommended"]["metrics"]
        ex.append({"step": "evaluation", "text": f"SYNTHETIC Monte Carlo ({ev['budget']['replications']} paths x {ev['budget']['days']} days): simulated cycle service level {_pct(_mean(m['cycle_service_level']))}, fill rate {_pct(_mean(m['fill_rate']))}. Best of {len(ev['candidates'])} listed candidates, not a proof of optimality."})  # fmt: skip
    if cons["notes"] or result["costs"]["missing_inputs"]:
        ex.append({"step": "limits", "text": "Cost inputs not supplied: " + (", ".join(result["costs"]["missing_inputs"]) or "none") + "; the cost totals include only the supplied components."})  # fmt: skip
    return ex


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"
