"""The route optimizer: candidate generation, constraint-aware evaluation, ranking, alternatives and baseline comparison.

``solve`` is a pure function of (network, request, context). It enumerates loopless routes up to the hop limit with pruning bounds
that only remove routes a hard constraint would reject anyway, evaluates every candidate, and returns the routes ranked by the
objective together with the routes that are unverifiable and the reasons the others were rejected. Constraints are never relaxed:
when nothing is feasible the result says so and lists the closest rejected routes for information only.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Callable
from typing import Any

from app.routing import algorithms as A
from app.routing import evaluate as E
from app.routing.config import STATEMENTS, Constraints, Limits, RouteRequest
from app.routing.network import Lane, Network

EPS = 1e-9


def _filters(net: Network, c: Constraints) -> dict[str, str]:
    """Lanes that a hard, structural constraint removes before the search (uid -> reason)."""
    blocked: dict[str, str] = {}
    for lanes in net.out.values():
        for ln in lanes:
            if ln.uid in c.excluded_lanes:
                blocked[ln.uid] = "excluded_lanes"
            elif ln.origin in c.excluded_facilities or ln.dest in c.excluded_facilities:
                blocked[ln.uid] = "excluded_facilities"
            elif ln.mode in c.prohibited_modes:
                blocked[ln.uid] = "prohibited_modes"
            elif c.allowed_modes is not None and ln.mode not in c.allowed_modes:
                blocked[ln.uid] = "allowed_modes"
            elif any(f"{t}_PROHIBITED" in ln.restrictions for t in c.cargo_tags):
                blocked[ln.uid] = "cargo_tags"
    return blocked


def _min_hops(net: Network, src: int, dst: int, blocked: dict[str, str] | None = None) -> int | None:
    seen = {src: 0}
    q = deque([src])
    while q:
        n = q.popleft()
        if n == dst:
            return seen[n]
        for ln in net.out.get(n, ()):
            if ln.dest not in seen and (blocked is None or ln.uid not in blocked):
                seen[ln.dest] = seen[n] + 1
                q.append(ln.dest)
    return None


def _pruner(
    c: Constraints, ctx: E.Context, lb_dur: dict[int, float], lb_cost: dict[int, float], th: float, why: Counter
) -> Callable[[list[Lane]], bool]:
    lim = c.transit_limit_hours()

    def prune(path: list[Lane]) -> bool:
        last = path[-1]
        if lim is not None:
            known = sum(ln.duration_hours or 0.0 for ln in path) + th * (len(path) - 1)
            if known + lb_dur.get(last.dest, 0.0) > lim + EPS:
                why["max_transit_hours"] += 1
                return True
        if c.max_cost is not None:
            known = sum(ln.cost or 0.0 for ln in path if ln.currency == c.currency)
            if known + lb_cost.get(last.dest, 0.0) > c.max_cost + EPS:
                why["max_cost"] += 1
                return True
        if c.min_reliability is not None:
            prod = 1.0
            for ln in path:
                r = ctx.rel.get(ln.uid)
                if r is not None and r.value is not None:
                    prod *= r.value
            if prod < c.min_reliability - EPS:
                why["min_reliability"] += 1
                return True
        if (
            c.min_capacity is not None
            and last.capacity is not None
            and last.capacity_unit == c.capacity_unit
            and last.capacity < c.min_capacity - EPS
        ):
            why["min_capacity"] += 1
            return True
        if c.max_risk is not None:
            rk = ctx.risk.get(last.dest)
            if rk is not None and rk > c.max_risk + EPS:
                why["max_risk"] += 1
                return True
        if c.max_data_age_days is not None and ctx.now is not None and last.observed_at is not None and (ctx.now - last.observed_at).total_seconds() / 86400.0 > c.max_data_age_days + EPS:  # fmt: skip
            why["max_data_age_days"] += 1
            return True
        return False

    return prune


def _ranking_currency(items: list[dict[str, Any]], c: Constraints) -> str | None:
    if c.currency:
        return c.currency
    cnt = Counter(it["metrics"]["cost"]["currency"] for it in items if it["metrics"]["cost"]["currency"])
    return sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if cnt else None


def solve(
    net: Network, req: RouteRequest, ctx: E.Context, limits: Limits, *, stop: Callable[[], None] | None = None
) -> dict[str, Any]:
    c = req.constraints
    weights = req.effective_weights()
    ctx.transfer_hours = req.transfer_hours
    base: dict[str, Any] = {
        "objective": req.objective,
        "weights": weights,
        "statements": [STATEMENTS["decision_support"], STATEMENTS["search"]],
    }
    warnings: list[str] = []
    o, d = req.origin_facility_id, req.destination_facility_id
    for fid, label in ((o, "origin"), (d, "destination")):
        if fid in net.closed_nodes:
            return base | _empty(
                "no_feasible_route", [f"The {label} facility is closed by {net.closed_nodes[fid]}."], net, req
            )
        if fid not in net.nodes:
            return base | _empty(
                "no_feasible_route", [f"The {label} facility has no transport lane in the network."], net, req
            )
    blocked = _filters(net, c)
    if c.required_facilities:
        missing = [f for f in c.required_facilities if f not in net.nodes]
        if missing:
            warnings.append(f"Required facilities with no lane in the network: {missing}; no route can include them.")
    out = net.out
    th = req.transfer_hours
    allow = lambda ln: ln.uid not in blocked  # noqa: E731
    allowed_out = {n: [ln for ln in lanes if allow(ln)] for n, lanes in out.items()}
    lb_dur = A.reverse_distances(allowed_out, d, lambda ln: ln.duration_hours)
    lb_cost = A.reverse_distances(
        allowed_out, d, lambda ln: ln.cost if (ln.currency == c.currency and ln.cost is not None) else None
    )
    hops = min(req.max_hops, c.max_transfers + 1) if c.max_transfers is not None else req.max_hops
    pruned_by: Counter = Counter()
    paths, stats = A.enumerate_paths(
        out,
        o,
        d,
        hops,
        allow=allow,
        prune=_pruner(c, ctx, lb_dur, lb_cost, th, pruned_by),
        budget=limits.max_expansions,
        stop=stop,
    )
    fallback = False
    if stats.truncated:
        warnings.append(
            f"The search budget ({limits.max_expansions} expansions) was reached; the routes shown are the best found, not proven optimal."
        )
        wfun = {"FASTEST": lambda ln: ln.duration_hours, "LOWEST_COST": lambda ln: ln.cost if (c.currency is None or ln.currency == c.currency) else None}.get(req.objective)  # fmt: skip
        if wfun is not None:
            have = {tuple(ln.id for ln in p) for p in paths}
            for p in A.yen(allowed_out, o, d, wfun, max(req.alternatives * 3, 5)):
                if len(p) <= hops and tuple(ln.id for ln in p) not in have:
                    paths.append(p)
                    fallback = True
    capped = len(paths) > limits.max_candidates
    if capped:
        warnings.append(
            f"More than {limits.max_candidates} candidate routes were found; only the first {limits.max_candidates} (in deterministic search order) were evaluated."
        )
        paths = paths[: limits.max_candidates]
    feasible: list[dict[str, Any]] = []
    unver: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for p in paths:
        m = E.metrics(p, ctx)
        res = E.check(p, m, c, ctx)
        cls = E.classify(res)
        item = {"metrics": m, "constraints": res}
        if cls == "INFEASIBLE":
            item["violations"] = [r for r in res if r["state"] == E.VIOLATED]
            rejected.append(item)
        elif cls == "UNVERIFIABLE":
            item["reasons"] = [r["detail"] for r in res if r["state"] == E.UNVERIFIABLE]
            unver.append(item)
        else:
            feasible.append(item)
    ccy = _ranking_currency(feasible, c) if "cost" in weights else None
    scorable = []
    for it in feasible:
        gaps = E.objective_gaps(it["metrics"], weights, ccy)
        if gaps:
            it["reasons"] = ["Cannot be scored by the objective - " + g for g in gaps]
            unver.append(it)
        else:
            scorable.append(it)
    ordered = E.rank(scorable, weights)
    front = E.pareto(ordered) if ordered else set()
    best = ordered[0] if ordered else None
    routes = []
    for it in ordered[: req.alternatives]:
        mine, theirs = set(it["metrics"]["lane_uids"]), set(best["metrics"]["lane_uids"]) if best else set()
        it["pareto_optimal"] = tuple(it["metrics"]["lane_uids"]) in front
        it["overlap_with_best"] = round(len(mine & theirs) / max(len(mine | theirs), 1), 3)
        it["trade_offs"] = _deltas(it["metrics"], best["metrics"]) if best and it is not best else None
        it["explanation"] = E.explain(it, req, best, it["constraints"])
        routes.append(it)
    status = "feasible" if ordered else ("unverifiable" if unver else "no_feasible_route")
    rej_counter = Counter(v["constraint"] for r in rejected for v in r["violations"])
    unver.sort(key=lambda it: (it["metrics"]["duration"]["known_hours"], tuple(it["metrics"]["lane_uids"])))
    rejected.sort(
        key=lambda it: (
            len(it["violations"]),
            it["metrics"]["duration"]["known_hours"],
            tuple(it["metrics"]["lane_uids"]),
        )
    )
    result = base | {
        "status": status, "ranking_currency": ccy, "routes": routes,
        "route_count": {"feasible": len(ordered), "unverifiable": len(unver), "rejected": len(rejected)},
        "unverifiable": unver[:20],
        "rejections": {"count": len(rejected), "by_constraint": dict(sorted(rej_counter.items())), "examples": [_slim(r) for r in rejected[:20]], "cut_before_completion": dict(sorted(pruned_by.items())), "cut_note": "Partial routes cut early because no completion could satisfy the named constraint; each cut is counted per branch, not per finished route."},
        "pareto_front": [list(k) for k in sorted(front)][:10],
        "references": _references(ordered),
        "search": {"expansions": stats.expansions, "complete_candidates": stats.complete, "pruned_branches": stats.pruned, "budget": limits.max_expansions, "truncated": stats.truncated or capped, "yen_fallback_used": fallback, "max_hops": hops, "optimality": "BEST_FOUND" if (stats.truncated or capped) else "EXACT_WITHIN_HOP_LIMIT"},  # fmt: skip
        "filters": {"lanes_removed_before_search": dict(sorted(Counter(blocked.values()).items()))},
        "warnings": warnings,
    }  # fmt: skip
    if status != "feasible":
        result["infeasibility"] = _infeasibility(net, req, rejected, unver, blocked, stats)
    return result


def _references(ordered: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for crit in E.CRITERIA:
        vals = [it for it in ordered if E.value_of(it["metrics"], crit) is not None]
        if vals:
            sign = 1 if E.DIRECTION[crit] == "min" else -1
            pick = min(vals, key=lambda it: (sign * E.value_of(it["metrics"], crit), tuple(it["metrics"]["lane_uids"])))
            out[crit] = {
                "lane_uids": pick["metrics"]["lane_uids"],
                "value": E.value_of(pick["metrics"], crit),
                "rank": pick["rank"],
            }
    return out


def _deltas(m: dict[str, Any], best: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k in E.CRITERIA:
        a, b = E.value_of(m, k), E.value_of(best, k)
        out[k] = None if a is None or b is None else round(a - b, 6)
    return out


def _slim(it: dict[str, Any]) -> dict[str, Any]:
    m = it["metrics"]
    return {"lane_uids": m["lane_uids"], "modes": m["modes"], "total_hours": m["duration"]["total_hours"], "cost": m["cost"]["total"], "currency": m["cost"]["currency"], "violations": [{"constraint": v["constraint"], "limit": v["limit"], "observed": v["observed"], "detail": v["detail"]} for v in it["violations"]]}  # fmt: skip


def _empty(status: str, reasons: list[str], net: Network, req: RouteRequest) -> dict[str, Any]:
    return {
        "status": status, "ranking_currency": None, "routes": [], "route_count": {"feasible": 0, "unverifiable": 0, "rejected": 0}, "unverifiable": [],
        "rejections": {"count": 0, "by_constraint": {}, "examples": [], "cut_before_completion": {}, "cut_note": ""}, "pareto_front": [], "references": {},
        "search": {"expansions": 0, "complete_candidates": 0, "pruned_branches": 0, "budget": 0, "truncated": False, "yen_fallback_used": False, "max_hops": req.max_hops, "optimality": "NOT_SEARCHED"},  # fmt: skip
        "filters": {"lanes_removed_before_search": {}}, "warnings": [],
        "infeasibility": {"reasons": reasons, "network_connected": False, "near_misses": [], "relaxation": "No constraint was relaxed."},
    }  # fmt: skip


def _infeasibility(net: Network, req: RouteRequest, rejected: list[dict[str, Any]], unver: list[dict[str, Any]], blocked: dict[str, str], stats: A.SearchStats) -> dict[str, Any]:  # fmt: skip
    mh_any = _min_hops(net, req.origin_facility_id, req.destination_facility_id)
    mh = _min_hops(net, req.origin_facility_id, req.destination_facility_id, blocked)
    reasons = []
    if mh_any is None:
        reasons.append(
            "NOT_CONNECTED: no sequence of available lanes links the origin to the destination (check suspended, retired or disrupted lanes)."
        )
    elif mh is None:
        reasons.append(
            f"FILTERED: structural constraints ({', '.join(sorted(set(blocked.values())))}) remove every lane sequence that connects the origin to the destination."
        )
    elif mh > req.max_hops:
        reasons.append(f"HOP_LIMIT: the shortest connection needs {mh} lanes but max_hops is {req.max_hops}.")
    if mh is not None and not rejected and not unver:
        if stats.pruned:
            reasons.append(
                "BOUNDS: every connecting route would exceed a time, cost, reliability, capacity, risk or data-age limit."
            )
    if rejected:
        by = Counter(v["constraint"] for r in rejected for v in r["violations"])
        reasons.append(
            "CONSTRAINTS: every evaluated route violates at least one hard constraint ("
            + ", ".join(f"{k}: {n}" for k, n in sorted(by.items()))
            + ")."
        )
    if unver and not rejected:
        reasons.append(
            "UNVERIFIABLE: routes exist but missing data prevents verifying a constraint or scoring the objective; see 'unverifiable'."
        )
    return {
        "reasons": reasons, "network_connected": mh_any is not None, "min_hops": mh,
        "near_misses": [_slim(r) for r in rejected[:3]],
        "relaxation": "No constraint was relaxed. Near misses are shown for information only; they are not feasible routes.",
    }  # fmt: skip


def check_route(net: Network, uids: list[str], c: Constraints, ctx: E.Context) -> dict[str, Any]:
    """Validate one proposed lane sequence. Structure problems are reported with reasons rather than raised."""
    problems = []
    path: list[Lane] = []
    for u in uids:
        ln = net.lanes.get(u)
        if ln is None:
            why = net.closed.get(u) or next(
                (f"{e['reason']}" for e in net.excluded if e["lane_uid"] == u), "unknown lane"
            )
            problems.append(
                {"lane_uid": u, "problem": "LANE_NOT_AVAILABLE", "detail": f"Lane is not traversable: {why}."}
            )
        elif ln.origin in net.closed_nodes or ln.dest in net.closed_nodes:
            problems.append(
                {
                    "lane_uid": u,
                    "problem": "FACILITY_CLOSED",
                    "detail": f"A facility on this lane is closed by {net.closed_nodes.get(ln.origin) or net.closed_nodes.get(ln.dest)}.",
                }
            )
            path.append(ln)
        else:
            path.append(ln)
    if not problems:
        for a, b in zip(path, path[1:], strict=False):
            if a.dest != b.origin:
                problems.append(
                    {
                        "lane_uid": b.uid,
                        "problem": "NOT_CONTIGUOUS",
                        "detail": f"Lane {a.uid} ends at facility {a.dest} but {b.uid} starts at {b.origin}.",
                    }
                )
        nodes = [path[0].origin, *[ln.dest for ln in path]]
        if len(set(nodes)) != len(nodes):
            problems.append(
                {"lane_uid": None, "problem": "LOOP", "detail": "The route visits a facility more than once."}
            )
    if problems:
        return {"valid_structure": False, "problems": problems, "classification": "INFEASIBLE", "metrics": None, "constraints": [], "statements": [STATEMENTS["decision_support"]]}  # fmt: skip
    m = E.metrics(path, ctx)
    res = E.check(path, m, c, ctx)
    return {"valid_structure": True, "problems": [], "classification": E.classify(res), "metrics": m, "constraints": res, "statements": [STATEMENTS["decision_support"], STATEMENTS["cost"], STATEMENTS["duration"]]}  # fmt: skip


def compare(
    base: dict[str, Any], scen: dict[str, Any], scen_net: Network, ctx: E.Context, c: Constraints
) -> dict[str, Any]:
    """Baseline versus scenario: what changed for the best route and for each baseline route."""
    b_best = base["routes"][0] if base["routes"] else None
    s_best = scen["routes"][0] if scen["routes"] else None
    out: dict[str, Any] = {
        "status_change": None if base["status"] == scen["status"] else f"{base['status']} -> {scen['status']}",
        "baseline_status": base["status"], "scenario_status": scen["status"],
        "best_route_changed": None if (b_best is None or s_best is None) else b_best["metrics"]["lane_uids"] != s_best["metrics"]["lane_uids"],
        "baseline_best": None if b_best is None else b_best["metrics"]["lane_uids"], "scenario_best": None if s_best is None else s_best["metrics"]["lane_uids"],
        "best_route_deltas": None, "baseline_routes_in_scenario": [],
    }  # fmt: skip
    if b_best and s_best:
        same_ccy = b_best["metrics"]["cost"]["currency"] == s_best["metrics"]["cost"]["currency"]
        deltas = _deltas(s_best["metrics"], b_best["metrics"])
        if not same_ccy:
            deltas["cost"] = None
        out["best_route_deltas"] = deltas
    for r in base["routes"]:
        uids = r["metrics"]["lane_uids"]
        rows = [net_state(scen_net, u) for u in uids]
        bad = [x for x in rows if x["state"] != "AVAILABLE"]
        if bad:
            out["baseline_routes_in_scenario"].append({"lane_uids": uids, "state": "UNAVAILABLE", "causes": bad})
        else:
            path = [scen_net.lanes[u] for u in uids]
            m = E.metrics(path, ctx)
            res = E.check(path, m, c, ctx)
            dl = _deltas(m, r["metrics"])
            changed = any(v not in (None, 0, 0.0) for v in dl.values())
            out["baseline_routes_in_scenario"].append({"lane_uids": uids, "state": "CHANGED" if changed else "UNCHANGED", "deltas": dl, "classification": E.classify(res), "disruption_notes": m["disruption_notes"]})  # fmt: skip
    return out


def net_state(net: Network, uid: str) -> dict[str, Any]:
    if uid in net.closed:
        return {"lane_uid": uid, "state": "CLOSED", "cause": net.closed[uid]}
    ln = net.lanes.get(uid)
    if ln is None:
        return {"lane_uid": uid, "state": "MISSING", "cause": "not in the traversable network"}
    for n in (ln.origin, ln.dest):
        if n in net.closed_nodes:
            return {"lane_uid": uid, "state": "FACILITY_CLOSED", "cause": net.closed_nodes[n], "facility_id": n}
    return {"lane_uid": uid, "state": "AVAILABLE", "cause": None}
