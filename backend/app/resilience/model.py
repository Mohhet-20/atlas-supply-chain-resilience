"""Plan evaluation model (pure functions of the problem dict; no database, no randomness).

A plan is {offer_uid: quantity}. For one scenario each used supplier delivers min(quantity, capacity) if it is available and its lead time fits
inside the horizon. Opening stock covers any shortfall first. In ADAPTIVE mode the spare capacity of other available suppliers (whose lead time
plus the response time fits the horizon) then covers what is still short. Nothing here changes stored data.
"""

from __future__ import annotations

from typing import Any

EPS = 1e-9


def build_problem(snap: dict[str, Any], req: Any) -> dict[str, Any]:
    """Problem dict from the stored snapshot and the request."""
    eligible = [s for s in snap["suppliers"] if s["status"] == "ELIGIBLE"]
    eligible.sort(key=lambda s: (s["price"] if s["price"] is not None else float("inf"), s["uid"]))
    freight_all = bool(eligible) and all(s.get("freight") is not None for s in eligible)
    stock = snap["opening_stock"]["value"]
    return {
        "D": float(snap["demand"]), "H": float(req.horizon_days), "unit": snap["unit"], "currency": snap["currency"],
        "sup": {s["uid"]: s for s in eligible}, "order": [s["uid"] for s in eligible], "eligible": eligible,
        "scenarios": snap["scenarios"], "stock": stock, "stock_known": stock is not None, "freight_basis": freight_all,
        "shortage_cost": req.shortage_cost_per_unit, "mode": req.scenario_mode, "response": req.response_days,
        "overage": req.constraints.overage_pct, "allow_partial": req.allow_partial, "has_stress": bool(snap["scenarios"]),
    }  # fmt: skip


def _eff(
    P: dict[str, Any], uid: str, sc: dict[str, Any] | None, outage: str | None
) -> tuple[bool, float, float | None]:
    base = P["sup"][uid]
    avail, cap, lead = True, float(base["capacity"]), base["lead_days"]
    e = (sc or {}).get("effects", {}).get(uid)
    if e:
        avail = bool(e.get("available", True))
        if e.get("capacity") is not None:
            cap = float(e["capacity"])
        if e.get("lead_days") is not None:
            lead = float(e["lead_days"])
    if outage == uid:
        avail = False
    return avail, cap, lead


def eval_scenario(
    P: dict[str, Any], plan: dict[str, float], sc: dict[str, Any] | None = None, outage: str | None = None
) -> dict[str, Any]:
    dm = float((sc or {}).get("demand_multiplier") or 1.0)
    Ds = P["D"] * dm
    H = P["H"]
    delivered = 0.0
    lost = 0.0
    for uid, q in plan.items():
        if q <= EPS:
            continue
        avail, cap, lead = _eff(P, uid, sc, outage)
        got = min(q, cap) if (avail and (lead is None or lead <= H + EPS)) else 0.0
        delivered += got
        lost += q - got
    short = max(0.0, Ds - delivered)
    stock = P["stock"] or 0.0
    from_stock = min(stock, short)
    short -= from_stock
    recovered = 0.0
    if P["mode"] == "ADAPTIVE" and P["response"] is not None and short > EPS:
        head = 0.0
        for uid in P["order"]:
            if outage == uid:
                continue
            avail, cap, lead = _eff(P, uid, sc, outage)
            if avail and (lead is None or lead + P["response"] <= H + EPS):
                head += max(0.0, cap - plan.get(uid, 0.0))
        recovered = min(short, head)
        short -= recovered
    return {
        "demand": Ds, "delivered": delivered, "supply_lost": lost, "from_stock": from_stock, "recovered": recovered,
        "unmet": max(0.0, short), "fulfilment": 1.0 if Ds <= EPS else max(0.0, 1.0 - max(0.0, short) / Ds),
    }  # fmt: skip


def _largest(plan: dict[str, float]) -> str | None:
    used = [(-q, u) for u, q in plan.items() if q > EPS]
    return min(used)[1] if used else None


def scenario_results(P: dict[str, Any], plan: dict[str, float]) -> list[dict[str, Any]]:
    out = []
    for sc in P["scenarios"]:
        outage = None
        if sc.get("outage") == "LARGEST":
            outage = _largest(plan)
        elif sc.get("outage"):
            outage = None  # a named supplier is expressed through effects
        r = eval_scenario(P, plan, sc, outage)
        out.append(
            {
                "name": sc["name"],
                "kind": sc["kind"],
                "probability": sc.get("probability"),
                "mode": P["mode"],
                "failed_supplier": outage,
                **r,
            }
        )
    return out


def metrics(P: dict[str, Any], plan: dict[str, float]) -> dict[str, Any]:
    used = {u: q for u, q in plan.items() if q > EPS}
    tot = sum(used.values())
    base = eval_scenario(P, used)
    purchase = sum(q * P["sup"][u]["price"] for u, q in used.items() if P["sup"][u]["price"] is not None)
    all_priced = all(P["sup"][u]["price"] is not None for u in used)
    freight = sum(q * (P["sup"][u]["freight"] or 0.0) for u, q in used.items()) if P["freight_basis"] else None
    omitted = []
    if not P["freight_basis"]:
        omitted.append("freight (not stated by every eligible offer)")
    shortage = None
    if P["shortage_cost"] is not None:
        shortage = base["unmet"] * P["shortage_cost"]
    elif base["unmet"] > EPS:
        omitted.append("shortage cost (not supplied)")
    cost = None
    if all_priced:
        cost = purchase + (freight or 0.0) + (shortage or 0.0)
    shares = {u: q / tot for u, q in used.items()} if tot > EPS else {}
    hhi = sum(s * s for s in shares.values()) if shares else None
    leads = [P["sup"][u]["lead_days"] for u in used]
    delay = (
        sum(q * P["sup"][u]["lead_days"] for u, q in used.items()) / tot
        if tot > EPS and all(x is not None for x in leads)
        else None
    )
    exposure = max((eval_scenario(P, used, None, outage=u)["unmet"] for u in used), default=0.0)
    sres = scenario_results(P, used) if P["scenarios"] else []
    worst = min((r["fulfilment"] for r in sres), default=None)
    probs = [r["probability"] for r in sres]
    expected_unmet = (
        sum(r["probability"] * r["unmet"] for r in sres) if sres and all(p is not None for p in probs) else None
    )
    return {
        "total_quantity": tot, "suppliers_used": len(used), "shares": shares, "baseline": base,
        "cost": {"total_known": cost, "purchase": purchase if all_priced else None, "freight": freight, "shortage_penalty": shortage, "omitted_components": omitted, "basis": "PURCHASE_AND_FREIGHT" if P["freight_basis"] else "PURCHASE_ONLY", "currency": P["currency"]},  # fmt: skip
        "values": {"COST": cost, "UNMET_DEMAND": base["unmet"], "DELIVERY_DELAY": delay, "CONCENTRATION": hhi, "DISRUPTION_EXPOSURE": exposure, "WORST_CASE_SERVICE": worst},  # fmt: skip
        "baseline_fulfilment": base["fulfilment"], "scenarios": sres, "expected_unmet": expected_unmet,
        "countries": _country_hhi(P, shares),
    }  # fmt: skip


def _country_hhi(P: dict[str, Any], shares: dict[str, float]) -> float | None:
    by: dict[str, float] = {}
    for u, s in shares.items():
        c = P["sup"][u].get("country")
        if not c:
            return None
        by[c] = by.get(c, 0.0) + s
    return sum(v * v for v in by.values()) if by else None
