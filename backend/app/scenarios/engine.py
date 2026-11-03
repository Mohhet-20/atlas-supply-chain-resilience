"""Deterministic impact engine (``sim-1``). Identical definition + identical network inputs give identical results.

This is *impact analysis of a stated hypothetical*, not forecasting: no probabilities, no demand, inventory or financial
figures are modelled or invented. Exposure is "potential exposure" along confirmed relationships.

Pipeline: resolve events into seeds -> bounded downstream expansion (traversal) -> rank-ordered propagation
(each entity once, every path recorded) -> bounded upstream exploration (qualitative) -> alternative known sources.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.graph.rules import GraphRelType
from app.graph.store import EdgeFilter, EdgeRec, NodeKey, NodeRec, fetch_incident, get_rec, load_nodes, parse_ref
from app.graph.traversal import expand_multi
from app.models.enums import EdgeStatus, EventType, RegistryType
from app.models.facility import Facility
from app.scenarios import config as cfg
from app.scenarios.validation import event_targets

S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT
QUALITATIVE_EDGE_TYPES = {GraphRelType.STORES, GraphRelType.OWNS}
FLOW_TYPES = frozenset(
    {
        GraphRelType.SUPPLIES,
        GraphRelType.TRANSFERS_TO,
        GraphRelType.SUPPLIES_PRODUCT,
        GraphRelType.PRODUCES,
        GraphRelType.STORES,
    }
)


class RunCancelled(Exception):
    """Raised at a safe checkpoint when cancellation was requested."""


@dataclass(frozen=True)
class EngineLimits:
    max_nodes: int = 500
    max_edges: int = 1_000
    max_depth: int = 4
    time_budget_seconds: float = 20.0


@dataclass
class _State:
    """Propagation state of one entity (percentages are 0-100)."""

    rank: int
    direct: bool = False
    carries_loss: bool = False
    exact: float | None = None  # None = at least one needed input is unknown
    lower: float = 0.0
    delay: float | None = None
    events: frozenset[str] = frozenset()
    parents: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    best_parent: str | None = None
    path_count: int = 0


@dataclass
class EngineResult:
    impacts: list[dict[str, Any]]
    edge_impacts: list[dict[str, Any]]
    alternatives: list[dict[str, Any]]
    summary: dict[str, Any]
    warnings: list[dict[str, Any]]
    missing_data: list[dict[str, Any]]
    truncated: bool
    truncation_reasons: list[str]
    examined: int
    input_hash: str
    failures: list[dict[str, Any]]


def _pct(x: float) -> float:
    return round(min(100.0, max(0.0, x)), 4)


def input_hash(edges: dict[str, EdgeRec], nodes: dict[NodeKey, NodeRec]) -> str:
    """Hash of exactly the network facts the engine read (not timestamps, so unrelated writes do not change it)."""
    rows = [
        [e.ref, e.type.value, e.status.value, e.source[0].value, e.source[1], e.destination[0].value, e.destination[1],
         e.capacity_value, e.capacity_unit, e.lead_time_days, e.supply_share_pct, str(e.effective_from), str(e.effective_to)]
        for e in sorted(edges.values(), key=lambda x: x.ref)
    ]  # fmt: skip
    nrows = [
        [k[0].value, k[1], n.lifecycle, n.merged_into_id]
        for k, n in sorted(nodes.items(), key=lambda kv: (kv[0][0].value, kv[0][1]))
    ]
    return hashlib.sha256(json.dumps([rows, nrows], sort_keys=True, default=str).encode()).hexdigest()


def run_engine(
    session: Session,
    definition: dict[str, Any],
    limits: EngineLimits,
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> EngineResult:
    t0 = time.monotonic()
    a = cfg.Assumptions.model_validate(definition["scenario"].get("assumptions") or {})
    down_depth = min(a.downstream_depth, limits.max_depth)
    up_depth = min(a.upstream_depth, limits.max_depth)
    warnings: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    reasons: list[str] = []
    if down_depth < a.downstream_depth or up_depth < a.upstream_depth:
        warnings.append(
            {"code": "DEPTH_CAPPED", "text": f"Traversal depth was capped at the server limit of {limits.max_depth}."}
        )
        reasons.append("depth_cap")

    types = set(FLOW_TYPES) | ({GraphRelType.OWNS} if a.include_ownership else set())
    flt = EdgeFilter(types=frozenset(types), statuses=frozenset({EdgeStatus.CONFIRMED}), as_of=a.as_of_date)

    # 1. resolve events ----------------------------------------------------------------------------------------------
    node_f: dict[NodeKey, float] = {}  # fraction of the entity's output lost (overlaps take the larger value)
    node_events: dict[NodeKey, set[str]] = defaultdict(set)
    edge_fail: dict[str, set[str]] = defaultdict(set)
    edge_delay: dict[str, tuple[float, set[str]]] = {}
    delay_entity_targets: list[tuple[NodeKey, float, str]] = []
    event_meta: list[dict[str, Any]] = []
    edge_cache: dict[str, EdgeRec] = {}

    def edge_by_ref(ref: str) -> EdgeRec | None:
        if ref not in edge_cache:
            kind, rid = parse_ref(ref)
            rec = get_rec(session, kind, rid)
            if rec is None:
                return None
            edge_cache[ref] = rec
        return edge_cache[ref]

    def add_delay(ref: str, days: float, uid: str) -> None:
        old = edge_delay.get(ref)
        edge_delay[ref] = (max(days, old[0]) if old else days, (old[1] if old else set()) | {uid})

    for ev in definition["events"]:
        uid, et = ev["event_uid"], EventType(ev["event_type"])
        tg = event_targets(ev)
        dur = ev.get("duration_days")
        window = {
            "start_offset_days": ev.get("start_offset_days"), "duration_days": dur, "recovery_days": ev.get("recovery_days"),
            "total_days": (dur + (ev.get("recovery_days") or 0)) if dur else None,
        }  # fmt: skip
        event_meta.append(
            {
                "event_uid": uid,
                "event_type": et.value,
                "label": ev.get("label"),
                "window": window,
                "targets": len(tg),
                "relationship_ref": ev.get("relationship_ref"),
            }
        )
        if et in (EventType.RELATIONSHIP_UNAVAILABLE, EventType.LEAD_TIME_INCREASE) and ev.get("relationship_ref"):
            rec = edge_by_ref(ev["relationship_ref"])
            if rec is None or rec.status is not EdgeStatus.CONFIRMED:
                failures.append({"event_uid": uid, "error": "relationship_missing_or_not_confirmed_at_run_time"})
                continue
            if et is EventType.RELATIONSHIP_UNAVAILABLE:
                edge_fail[rec.ref].add(uid)
            else:
                add_delay(rec.ref, float(ev["added_lead_time_days"]), uid)
            continue
        if et is EventType.LEAD_TIME_INCREASE:
            for k in tg:
                delay_entity_targets.append((k, float(ev["added_lead_time_days"]), uid))
            continue
        if et in (EventType.SUPPLIER_UNAVAILABLE, EventType.FACILITY_SHUTDOWN):
            frac = 1.0
        elif et in (EventType.SUPPLIER_CAPACITY_REDUCTION, EventType.FACILITY_CAPACITY_REDUCTION):
            frac = float(ev["magnitude_pct"]) / 100.0
        else:  # REGION_DISRUPTION / CUSTOM
            frac = 1.0 if ev.get("effect") == "UNAVAILABLE" else float(ev["magnitude_pct"]) / 100.0
        for k in tg:
            node_f[k] = max(node_f.get(k, 0.0), frac)
            node_events[k].add(uid)
    if delay_entity_targets:
        out_edges, more = fetch_incident(session, {k for k, _, _ in delay_entity_targets}, "out", flt, limits.max_edges)
        if more:
            reasons.append("edge_limit")
        for k, days, uid in delay_entity_targets:
            hit = [e for e in out_edges if e.source == k]
            if not hit:
                missing.append(
                    {
                        "code": "NO_OUTGOING_RELATIONSHIPS",
                        "entity": f"{k[0].value}:{k[1]}",
                        "text": "The target has no confirmed outgoing relationships, so a lead-time increase has nothing to act on.",
                    }
                )
            for e in hit:
                edge_cache[e.ref] = e
                add_delay(e.ref, days, uid)
    checkpoint()

    # 2. downstream expansion -----------------------------------------------------------------------------------------
    forced_dest: dict[NodeKey, set[str]] = defaultdict(set)  # edge-event destinations -> edge refs
    for ref in set(edge_fail) | set(edge_delay):
        forced_dest[edge_cache[ref].destination].add(ref)
    starts = sorted(set(node_f) | set(forced_dest), key=lambda k: (k[0].value, k[1]))
    exp = None
    if starts:
        remaining = max(0.001, limits.time_budget_seconds - (time.monotonic() - t0))
        exp = expand_multi(
            session, starts, "downstream", down_depth, flt, limits.max_nodes, limits.max_edges, remaining
        )
        for r in exp.truncation_reasons:
            if r not in reasons:
                reasons.append(r)
    edges: dict[str, EdgeRec] = dict(exp.edges) if exp else {}
    for ref in set(edge_fail) | set(edge_delay):
        edges.setdefault(ref, edge_cache[ref])
    out_adj: dict[NodeKey, list[EdgeRec]] = defaultdict(list)
    in_adj: dict[NodeKey, list[EdgeRec]] = defaultdict(list)
    for e in sorted(edges.values(), key=lambda x: x.ref):
        out_adj[e.source].append(e)
        in_adj[e.destination].append(e)
    checkpoint()

    # 3. ranks (hops from the disrupted entity or failed relationship) ---------------------------------------------
    rank: dict[NodeKey, int] = {k: 0 for k in node_f}
    for k in forced_dest:
        rank.setdefault(k, 1)
    for level in range(0, down_depth):
        for u in sorted((n for n, r in rank.items() if r == level), key=lambda k: (k[0].value, k[1])):
            for e in out_adj.get(u, ()):
                v = e.destination
                if v not in rank or rank[v] > level + 1:
                    rank[v] = level + 1
    at_limit = [n for n, r in rank.items() if r == down_depth]
    if at_limit:  # only flag the limit if something actually lies beyond it
        beyond, _more = fetch_incident(session, at_limit, "out", flt, 50)
        if any(e.destination not in rank for e in beyond):
            reasons.append("depth_limit")
    ordered = sorted(rank, key=lambda k: (rank[k], k[0].value, k[1]))
    nodes = load_nodes(session, ordered)
    checkpoint()

    # 4. propagation -------------------------------------------------------------------------------------------------
    state: dict[NodeKey, _State] = {}
    for k in ordered:
        r = rank[k]
        st = _State(rank=r)
        if k in node_f:
            st.direct = st.carries_loss = True
            st.exact = st.lower = _pct(node_f[k] * 100)
            st.events = frozenset(node_events[k])
            st.rules.append("R1_DIRECT_TARGET")
            state[k] = st
            continue
        parents: dict[str, EdgeRec] = {}
        for e in in_adj.get(k, ()):
            forced = e.ref in edge_fail or e.ref in edge_delay
            if forced or (rank.get(e.source) == r - 1 and e.source in state):
                parents[e.ref] = e
        if not parents:
            continue
        exact_ok, qual, carries = True, False, False
        lower_sum, exact_sum = 0.0, 0.0
        contribs: list[tuple[str, float]] = []
        delay_best: float | None = None
        evs: set[str] = set()
        for ref in sorted(parents):
            e = parents[ref]
            u_state = state.get(e.source)
            if ref in edge_fail:
                f_exact, f_lower, loss_carry = 1.0, 1.0, True
                evs |= edge_fail[ref]
            elif u_state is not None and u_state.carries_loss:
                f_exact = None if u_state.exact is None else u_state.exact / 100.0
                f_lower = u_state.lower / 100.0
                loss_carry = True
            else:
                f_exact, f_lower, loss_carry = 0.0, 0.0, False
            if u_state is not None and (loss_carry or u_state.delay):
                evs |= set(u_state.events)
            if loss_carry:
                carries = True
                if e.type in QUALITATIVE_EDGE_TYPES:
                    qual, exact_ok = True, False
                    st.rules.append("R4_QUALITATIVE_EDGE_" + e.type.value)
                elif e.supply_share_pct is None:
                    exact_ok = False
                    st.uncertainty.append(
                        f"Supply share on {e.ref} is unknown, so the lost share of inflow cannot be quantified from it."
                    )
                else:
                    share = e.supply_share_pct
                    lower_sum += share * f_lower
                    if f_exact is None:
                        exact_ok = False
                        st.uncertainty.append(
                            f"The loss at the upstream entity of {e.ref} is itself not fully quantified."
                        )
                    else:
                        exact_sum += share * f_exact
                    contribs.append((ref, share * (f_exact if f_exact is not None else f_lower)))
            d_edge = edge_delay.get(ref)
            d_up = (u_state.delay or 0.0) if u_state is not None else 0.0
            if d_edge is not None or d_up:
                total = d_up + (d_edge[0] if d_edge else 0.0)
                delay_best = total if delay_best is None else max(delay_best, total)
                if d_edge:
                    evs |= d_edge[1]
        st.events = frozenset(evs)
        st.parents = sorted(parents)
        st.path_count = len(parents)
        st.carries_loss = carries
        if carries:
            st.lower = _pct(lower_sum)
            if exact_ok and not qual:
                if exact_sum > 100.0 + 1e-9:
                    st.uncertainty.append(
                        "Known supply shares from affected sources sum to more than 100%; the figure is capped at 100%."
                    )
                st.exact = _pct(exact_sum)
                st.rules.append("R2_SHARE_PROPAGATION")
            else:
                st.exact = None
                st.rules.append("R4_QUALITATIVE_ONLY" if qual else "R3_PARTIAL_OR_UNKNOWN_SHARE")
        if delay_best is not None:
            st.delay = delay_best
            st.rules.append("R5_DELAY_PASS_THROUGH")
        st.best_parent = sorted(contribs, key=lambda c: (-c[1], c[0]))[0][0] if contribs else sorted(parents)[0]
        state[k] = st
    checkpoint()

    # 5. upstream exploration (qualitative) --------------------------------------------------------------------------
    up_state: dict[NodeKey, dict[str, Any]] = {}
    if a.include_upstream and up_depth > 0:
        up_starts = sorted({k for k in node_f if k[0] is not S}, key=lambda k: (k[0].value, k[1]))
        if up_starts:
            remaining = max(0.001, limits.time_budget_seconds - (time.monotonic() - t0))
            uexp = expand_multi(
                session, up_starts, "upstream", up_depth, flt, limits.max_nodes, limits.max_edges, remaining
            )
            for r in uexp.truncation_reasons:
                reasons.append("upstream_" + r)
            for e in uexp.edges.values():
                edges.setdefault(e.ref, e)
            for k, d in uexp.depth_of.items():
                if d == 0 or k in state:
                    continue
                par = sorted(
                    (e for e in uexp.edges.values() if e.source == k and uexp.depth_of.get(e.destination, -1) == d - 1),
                    key=lambda x: x.ref,
                )
                up_state[k] = {"depth": d, "edge": par[0] if par else None, "events": set()}
    for ref in sorted(set(edge_fail) | set(edge_delay)):
        e = edges[ref]
        if a.include_upstream and e.source not in state and e.source not in up_state:
            evs = set(edge_fail.get(ref, set())) | (edge_delay[ref][1] if ref in edge_delay else set())
            up_state[e.source] = {"depth": 1, "edge": e, "events": evs}
    for (
        info
    ) in up_state.values():  # attribute to the events that target the entity this one feeds (following the chain)
        cur = info["edge"]
        for _ in range(up_depth + 1):
            if cur is None:
                break
            if cur.destination in node_events:
                info["events"] = set(node_events[cur.destination])
                break
            nxt = up_state.get(cur.destination)
            cur = nxt["edge"] if nxt else None
    nodes.update(load_nodes(session, [k for k in up_state if k not in nodes]))
    checkpoint()

    # 6. alternatives ------------------------------------------------------------------------------------------------
    impacted_keys = {k for k, st in state.items() if st.carries_loss or st.delay}
    alt_rows: list[dict[str, Any]] = []
    alt_edges: dict[str, EdgeRec] = {}
    if a.include_alternatives and impacted_keys:
        targets = sorted((k for k in impacted_keys if k[0] in (F, P)), key=lambda k: (state[k].rank, k[0].value, k[1]))
        if targets and time.monotonic() - t0 < limits.time_budget_seconds:
            alt_flt = EdgeFilter(
                types=frozenset(FLOW_TYPES - {GraphRelType.STORES}),
                statuses=frozenset(
                    {EdgeStatus.CONFIRMED} | ({EdgeStatus.PROPOSED} if a.include_proposed_alternatives else set())
                ),
                as_of=a.as_of_date,
            )
            inbound, more = fetch_incident(session, targets, "in", alt_flt, limits.max_edges * 2)
            if more:
                reasons.append("alternatives_edge_limit")
            by_dest: dict[NodeKey, list[EdgeRec]] = defaultdict(list)
            for e in inbound:
                by_dest[e.destination].append(e)
            picks: list[tuple[NodeKey, EdgeRec]] = []
            for t in targets:
                cands = [
                    e for e in by_dest.get(t, ())
                    if e.ref not in edge_fail and e.ref not in state[t].parents and e.source not in impacted_keys
                    and not (e.source in state and state[e.source].direct)
                ]  # fmt: skip
                cands.sort(
                    key=lambda e: (
                        e.status is not EdgeStatus.CONFIRMED,
                        e.supply_share_pct is None,
                        -(e.supply_share_pct or 0.0),
                        e.ref,
                    )
                )
                for e in cands[: cfg.MAX_ALTERNATIVES_PER_ENTITY]:
                    if len(picks) >= cfg.MAX_ALTERNATIVES_TOTAL:
                        reasons.append("alternatives_cap")
                        break
                    picks.append((t, e))
            nodes.update(load_nodes(session, [e.source for _, e in picks if e.source not in nodes]))
            for t, e in picks:
                n = nodes.get(e.source)
                if n is None:
                    continue
                alt_edges[e.ref] = e
                gaps = []
                if e.status is not EdgeStatus.CONFIRMED:
                    gaps.append("The relationship is PROPOSED (not yet confirmed).")
                if e.capacity_value is None:
                    gaps.append("Capacity of this relationship is unknown; spare capacity is never inferred.")
                if e.supply_share_pct is None:
                    gaps.append("Current supply share is unknown.")
                if e.lead_time_days is None:
                    gaps.append("Lead time is unknown.")
                if e.confidence is None:
                    gaps.append("No confidence value is recorded for the relationship.")
                alt_rows.append({
                    "for_type": t[0].value, "for_id": t[1], "source_type": e.source[0].value, "source_id": e.source[1],
                    "source_code": n.code, "source_name": n.name, "edge_ref": e.ref, "edge_type": e.type.value,
                    "edge_status": e.status.value, "supply_share_pct": e.supply_share_pct, "capacity_value": e.capacity_value,
                    "capacity_unit": e.capacity_unit, "lead_time_days": e.lead_time_days, "confidence": e.confidence, "missing": gaps,
                    "caveat": "A graph connection is not proof of available capacity or qualification. For analyst review only; no action is taken.",
                })  # fmt: skip
    checkpoint()

    # 7. assemble impacts --------------------------------------------------------------------------------------------
    fac_caps: dict[int, tuple[float | None, str | None]] = {}
    fac_ids = [k[1] for k in node_f if k[0] is F]
    if fac_ids:
        for fr in session.scalars(select(Facility).where(Facility.id.in_(fac_ids))):
            fac_caps[fr.id] = (fr.capacity_value, fr.capacity_unit)
    impacts: list[dict[str, Any]] = []
    for k in ordered:
        st, n = state.get(k), nodes.get(k)
        if st is None or n is None or not (st.direct or st.carries_loss or st.delay):
            continue
        impacts.append(_impact_row(k, st, n, state, nodes, edges, fac_caps, node_f))
    for k, info in sorted(up_state.items(), key=lambda kv: (kv[1]["depth"], kv[0][0].value, kv[0][1])):
        n = nodes.get(k)
        if n is None:
            continue
        e = info["edge"]
        step = _step(e, nodes)
        impacts.append({
            "entity_type": k[0].value, "entity_id": k[1], "entity_code": n.code, "entity_name": n.name, "impact_kind": "UPSTREAM",
            "depth": info["depth"], "event_uids": sorted(info["events"]), "quantification": "UNQUANTIFIED", "exposure_level": "QUALITATIVE",
            "loss_pct": None, "loss_pct_lower_bound": None, "capacity_lost_value": None, "capacity_unit": None, "added_lead_time_days": None,
            "path_count": 1, "primary_path": [step] if step else [], "parent_edges": [e.ref] if e else [], "rules": ["R6_UPSTREAM_OUTLET_LOST"],
            "uncertainty": ["The supplier's total output and alternative customers are unknown, so the effect on it is not quantified."],
            "explanation": f"{n.name} supplies a disrupted entity through {e.ref if e else 'a relationship'}. It may lose that outlet; the size of the effect is not estimated. {cfg.EXPLAIN_NOTE}",
        })  # fmt: skip
    if len(impacts) > cfg.RESULT_CAP:
        impacts = impacts[: cfg.RESULT_CAP]
        reasons.append("result_cap")

    edge_impacts = _edge_impacts(edges, state, node_f, edge_fail, edge_delay, impacts)
    summary = _summary(impacts, edge_impacts, node_f, fac_caps, a, alt_rows)
    for em in event_meta:
        em["entities_reached"] = sum(1 for i in impacts if em["event_uid"] in i["event_uids"])
        em["direct"] = sum(1 for i in impacts if i["impact_kind"] == "DIRECT" and em["event_uid"] in i["event_uids"])
    summary["by_event"] = event_meta
    unk_share = sum(1 for e in edge_impacts if e["supply_share_pct"] is None and e["effect"] != "DELAYED")
    if unk_share:
        missing.append(
            {
                "code": "SUPPLY_SHARE_UNKNOWN",
                "count": unk_share,
                "text": f"{unk_share} affected relationship(s) have no supply share, so downstream loss could not be fully quantified.",
            }
        )
    unk_cap = sum(1 for e in edge_impacts if e["capacity_value"] is None)
    if unk_cap:
        missing.append(
            {
                "code": "CAPACITY_UNKNOWN",
                "count": unk_cap,
                "text": f"{unk_cap} affected relationship(s) have no recorded capacity.",
            }
        )
    n_sup = sum(1 for k in node_f if k[0] is S)
    if n_sup:
        missing.append(
            {
                "code": "SUPPLIER_CAPACITY_NOT_RECORDED",
                "count": n_sup,
                "text": "Suppliers have no capacity field in the registry; supplier capacity effects are expressed through relationship capacity and shares only.",
            }
        )
    n_nodur = sum(1 for em in event_meta if em["window"]["duration_days"] is None)
    if n_nodur:
        missing.append(
            {
                "code": "DURATION_UNKNOWN",
                "count": n_nodur,
                "text": "Some events have no duration; their disruption window is unknown.",
            }
        )
    warnings.append(
        {
            "code": "NO_BUFFER_MODEL",
            "text": "Inventory, safety stock, substitution and demand are not modelled; exposure is potential, not predicted.",
        }
    )
    if not edges:
        warnings.append(
            {
                "code": "NO_RELATIONSHIPS_FOUND",
                "text": "No confirmed relationships were found around the disrupted entities, so only direct impact is reported.",
            }
        )
    if exp is not None and exp.truncated:
        warnings.append(
            {
                "code": "TRAVERSAL_TRUNCATED",
                "text": "Traversal limits were reached; the affected set may be incomplete: "
                + ", ".join(exp.truncation_reasons),
            }
        )
    used_edges = dict(edges)
    used_edges.update(alt_edges)
    return EngineResult(
        impacts=impacts, edge_impacts=edge_impacts, alternatives=alt_rows, summary=summary, warnings=warnings, missing_data=missing,
        truncated=bool(reasons), truncation_reasons=sorted(set(reasons)), examined=len(rank) + len(up_state),
        input_hash=input_hash(used_edges, nodes), failures=failures,
    )  # fmt: skip


def _step(e: EdgeRec | None, nodes: dict[NodeKey, NodeRec]) -> dict[str, Any] | None:
    if e is None:
        return None
    fn, tn = nodes.get(e.source), nodes.get(e.destination)
    return {
        "ref": e.ref, "type": e.type.value, "from": {"type": e.source[0].value, "id": e.source[1], "name": fn.name if fn else None},
        "to": {"type": e.destination[0].value, "id": e.destination[1], "name": tn.name if tn else None},
        "supply_share_pct": e.supply_share_pct,
    }  # fmt: skip


def _path_to(
    k: NodeKey, state: dict[NodeKey, _State], edges: dict[str, EdgeRec], nodes: dict[NodeKey, NodeRec]
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    cur = k
    for _ in range(cfg.MAX_PATH_STEPS):
        st = state.get(cur)
        if st is None or st.direct or st.best_parent is None:
            break
        e = edges.get(st.best_parent)
        if e is None:
            break
        step = _step(e, nodes)
        if step:
            steps.append(step)
        cur = e.source
    steps.reverse()
    return steps


def _impact_row(k, st, n, state, nodes, edges, fac_caps, node_f) -> dict[str, Any]:  # noqa: ANN001
    kind = "DIRECT" if st.direct else "DOWNSTREAM"
    cap_val = cap_unit = None
    unc = list(dict.fromkeys(st.uncertainty))
    if st.direct:
        if k[0] is F:
            cv, cu = fac_caps.get(k[1], (None, None))
            if cv is not None:
                cap_val, cap_unit = round(cv * node_f[k], 6), cu
            else:
                unc.append("The facility's capacity is not recorded, so absolute capacity loss is unknown.")
        else:
            unc.append("Supplier capacity is not recorded; see affected relationships for capacity at risk.")
        unc.append("The loss percentage is the scenario's own assumption, not an observation.")
        level, quant, loss, lower = "DIRECT", "QUANTIFIED", st.exact, st.lower
        expl = f"{n.name} is a named target of the scenario; the scenario assumes {loss:g}% of its output is lost."
    else:
        if st.carries_loss and st.exact is not None:
            quant, loss, lower = "QUANTIFIED", st.exact, st.lower
            level = "HIGH" if loss >= cfg.HIGH_LOSS_PCT else "PARTIAL"
            expl = f"{n.name} receives {loss:g}% of its known inflow (sum of supply share x lost fraction) from disrupted sources."
        elif st.carries_loss and st.lower > 0:
            quant, loss, lower = "LOWER_BOUND", None, st.lower
            level = "HIGH" if lower >= cfg.HIGH_LOSS_PCT else "UNQUANTIFIED"
            expl = f"{n.name} is exposed; at least {lower:g}% of its inflow comes from disrupted sources, but other affected inflow has an unknown share."
        elif st.carries_loss:
            quant, loss, lower = "UNQUANTIFIED", None, None
            level = "QUALITATIVE" if "R4_QUALITATIVE_ONLY" in st.rules else "UNQUANTIFIED"
            expl = f"{n.name} is downstream of a disrupted entity; the data does not support a quantity."
        else:
            quant, loss, lower, level = "QUANTIFIED", None, None, "DELAY"
            expl = f"{n.name} may receive deliveries later."
        if st.delay:
            expl += f" Potential added lead time up to {st.delay:g} days along the path (no buffer assumed)."
        if st.path_count > 1:
            expl += f" {st.path_count} affected inbound relationships were found; the entity is counted once."
        if loss is None and st.carries_loss:
            unc.append("Share of inflow lost could not be fully computed from known supply shares.")
    return {
        "entity_type": k[0].value, "entity_id": k[1], "entity_code": n.code, "entity_name": n.name, "impact_kind": kind, "depth": st.rank,
        "event_uids": sorted(st.events), "quantification": quant, "exposure_level": level, "loss_pct": loss, "loss_pct_lower_bound": lower,
        "capacity_lost_value": cap_val, "capacity_unit": cap_unit, "added_lead_time_days": st.delay, "path_count": max(1, st.path_count),
        "primary_path": _path_to(k, state, edges, nodes), "parent_edges": st.parents[: cfg.MAX_PARENT_EDGES_STORED],
        "rules": list(dict.fromkeys(st.rules)), "uncertainty": unc, "explanation": expl + " " + cfg.EXPLAIN_NOTE,
    }  # fmt: skip


def _edge_impacts(edges, state, node_f, edge_fail, edge_delay, impacts) -> list[dict[str, Any]]:  # noqa: ANN001
    rows = []
    for e in sorted(edges.values(), key=lambda x: x.ref):
        failed, delayed = e.ref in edge_fail, e.ref in edge_delay
        src_state = state.get(e.source)
        src_loss = src_state is not None and src_state.carries_loss
        src_delay = src_state is not None and bool(src_state.delay)
        if not (failed or delayed or src_loss or src_delay):
            continue
        if failed:
            effect = "UNAVAILABLE"
        elif e.source in node_f:
            effect = "UNAVAILABLE" if node_f[e.source] >= 1.0 else "REDUCED"
        elif (delayed or src_delay) and not src_loss:
            effect = "DELAYED"
        else:
            effect = "EXPOSED"
        evs: set[str] = set(edge_fail.get(e.ref, set()))
        if delayed:
            evs |= edge_delay[e.ref][1]
        if src_state is not None:
            evs |= set(src_state.events)
        rows.append({
            "edge_ref": e.ref, "edge_type": e.type.value, "source_type": e.source[0].value, "source_id": e.source[1],
            "destination_type": e.destination[0].value, "destination_id": e.destination[1], "effect": effect,
            "supply_share_pct": e.supply_share_pct, "capacity_value": e.capacity_value, "capacity_unit": e.capacity_unit,
            "lead_time_days": e.lead_time_days, "added_lead_time_days": edge_delay[e.ref][0] if delayed else None, "event_uids": sorted(evs),
        })  # fmt: skip
    return rows


def _summary(impacts, edge_impacts, node_f, fac_caps, a, alts) -> dict[str, Any]:  # noqa: ANN001
    by_kind: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    by_level: dict[str, int] = defaultdict(int)
    by_quant: dict[str, int] = defaultdict(int)
    for i in impacts:
        by_kind[i["impact_kind"]] += 1
        by_type[i["entity_type"]] += 1
        by_level[i["exposure_level"]] += 1
        by_quant[i["quantification"]] += 1
    at_risk: dict[str, float] = defaultdict(float)
    unknown_cap = 0
    for e in edge_impacts:
        src = (RegistryType(e["source_type"]), e["source_id"])
        if src in node_f and e["effect"] in ("UNAVAILABLE", "REDUCED"):
            if e["capacity_value"] is None:
                unknown_cap += 1
            else:
                at_risk[e["capacity_unit"] or "unspecified"] += e["capacity_value"] * node_f[src]
    fac_loss: dict[str, float] = defaultdict(float)
    for i in impacts:
        if i["capacity_lost_value"] is not None:
            fac_loss[i["capacity_unit"] or "unspecified"] += i["capacity_lost_value"]
    return {
        "entities_affected": len(impacts), "direct": by_kind.get("DIRECT", 0),
        "indirect": by_kind.get("DOWNSTREAM", 0) + by_kind.get("UPSTREAM", 0), "downstream": by_kind.get("DOWNSTREAM", 0),
        "upstream": by_kind.get("UPSTREAM", 0), "by_type": dict(by_type), "by_exposure_level": dict(by_level),
        "by_quantification": dict(by_quant), "affected_relationships": len(edge_impacts),
        "affected_products": by_type.get("PRODUCT", 0), "affected_facilities": by_type.get("FACILITY", 0),
        "affected_suppliers": by_type.get("SUPPLIER", 0),
        "capacity_at_risk_by_unit": {k: round(v, 6) for k, v in sorted(at_risk.items())},
        "capacity_at_risk_note": "Relationship capacities leaving disrupted entities x lost fraction, per unit. Units are never converted or added across; relationships with unknown capacity are excluded (counted separately).",
        "relationships_with_unknown_capacity": unknown_cap,
        "facility_capacity_lost_by_unit": {k: round(v, 6) for k, v in sorted(fac_loss.items())},
        "alternatives_total": len(alts), "entities_with_alternatives": len({(x["for_type"], x["for_id"]) for x in alts}),
        "assumptions_used": a.model_dump(mode="json"), "interpretation": cfg.EXPLAIN_NOTE,
    }  # fmt: skip
