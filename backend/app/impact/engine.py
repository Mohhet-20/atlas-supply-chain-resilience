"""Cascading impact analysis engine (``ci-1``).

Input: one finished simulation run (its frozen scenario definition). The scenario engine is reused, not copied:
it is run once for the whole scenario (the combined view) and once per initiating event (each event's own contribution).
This module adds what a single pass cannot show: the initiating / direct / propagated distinction, bounded multi-path
enumeration with per-event attribution, explicit rules for combining overlapping events, impact dimensions with explicit
"unavailable" states, separate confidence / coverage / uncertainty, structural criticality and alternate paths, and an
immutable snapshot of the relationship records that were read.

Nothing is invented: unknown shares, capacities, lead times and durations stay unknown; reachability is never reported as
a shortage. All output is deterministic for identical inputs and configuration.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.graph.rules import EdgeKind, GraphRelType
from app.graph.store import EdgeFilter, EdgeRec, NodeKey, NodeRec, fetch_incident, get_rec, load_nodes, parse_ref
from app.impact import config as cfg
from app.impact.structure import PathIndex, enumerate_paths, reachable, roots_reaching
from app.models.enums import EdgeStatus, EntityKind, EventType, RegistryType
from app.models.scenario import SimulationRun
from app.quality import engine as quality_engine
from app.scenarios.engine import FLOW_TYPES, QUALITATIVE_EDGE_TYPES, EngineLimits, EngineResult, run_engine
from app.scenarios.validation import event_targets

S, F, P = RegistryType.SUPPLIER, RegistryType.FACILITY, RegistryType.PRODUCT
UNAVAILABLE, QUALITATIVE, QUANTIFIED, NOT_APPLICABLE, LOWER_BOUND = (
    "UNAVAILABLE", "QUALITATIVE", "QUANTIFIED", "NOT_APPLICABLE", "LOWER_BOUND",
)  # fmt: skip


@dataclass(frozen=True)
class ImpactLimits:
    max_depth: int = 4
    max_nodes: int = 500
    max_edges: int = 1_000
    time_budget_seconds: float = 30.0
    max_path_steps: int = 50_000
    max_findings: int = 2_000


@dataclass
class AnalysisResult:
    findings: list[dict[str, Any]]
    criticality: list[dict[str, Any]]
    alternatives: list[dict[str, Any]]
    edge_snapshots: list[dict[str, Any]]
    summary: dict[str, Any]
    coverage: dict[str, Any]
    uncertainty: list[dict[str, Any]]
    events: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    excluded: dict[str, Any]
    truncated: bool
    truncation_reasons: list[str]
    graph_hash: str
    graph_basis: str
    source_engine_version: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _k(row: dict[str, Any]) -> NodeKey:
    return RegistryType(row["entity_type"]), int(row["entity_id"])


def _label(k: NodeKey) -> str:
    return f"{k[0].value}:{k[1]}"


def _edge_record(e: EdgeRec, role: str) -> dict[str, Any]:
    return {
        "edge_ref": e.ref, "edge_type": e.type.value, "source_type": e.source[0], "source_id": e.source[1],
        "destination_type": e.destination[0], "destination_id": e.destination[1], "status": e.status.value, "role": role,
        "version": e.version,
        "record": {
            "code": e.code, "provenance": e.provenance, "source_namespace": e.source_namespace, "supply_share_pct": e.supply_share_pct,
            "capacity_value": e.capacity_value, "capacity_unit": e.capacity_unit, "lead_time_days": e.lead_time_days,
            "confidence": e.confidence, "criticality": e.criticality,
            "effective_from": e.effective_from.isoformat() if e.effective_from else None,
            "effective_to": e.effective_to.isoformat() if e.effective_to else None,
            "last_updated": e.updated_at.isoformat() if e.updated_at else None,
        },
    }  # fmt: skip


def _stale(e: EdgeRec, ref_date: date, days: int) -> bool:
    ts = e.updated_at or e.created_at
    return ts is not None and (ref_date - ts.astimezone(timezone.utc).date()).days > days


def _is_synthetic(e: EdgeRec) -> bool:
    return "SYNTHETIC" in (e.provenance or "").upper() or "SIMULATED" in (e.provenance or "").upper()


def run_analysis(
    session: Session,
    run: SimulationRun,
    conf: cfg.ImpactConfig,
    lim: ImpactLimits,
    *,
    checkpoint: Callable[[], None] = lambda: None,
) -> AnalysisResult:
    t0 = time.monotonic()
    ref_date = conf.reference_date or datetime.now(timezone.utc).date()
    defn = run.scenario_snapshot
    events: list[dict[str, Any]] = list(defn["events"])
    reasons: list[str] = []
    warnings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    depth = min(lim.max_depth, conf.max_path_length)

    def budget() -> float:
        return max(0.001, lim.time_budget_seconds - (time.monotonic() - t0))

    def over_budget() -> bool:
        return time.monotonic() - t0 > lim.time_budget_seconds

    base_assump = dict(defn["scenario"].get("assumptions") or {})
    assump = {**base_assump, "include_upstream": False, "include_alternatives": False}
    if conf.as_of_date is not None:
        assump["as_of_date"] = conf.as_of_date.isoformat()
    scenario = {**defn["scenario"], "assumptions": assump}

    def eng(evs: list[dict[str, Any]]) -> EngineResult:
        return run_engine(
            session, {"scenario": scenario, "events": evs},
            EngineLimits(lim.max_nodes, lim.max_edges, depth, budget()), checkpoint=checkpoint,
        )  # fmt: skip

    # 0. is the network still what the simulation run saw? (same engine, original assumptions, compare input hashes) -----
    basis = "UNKNOWN"
    if run.graph_hash:
        orig = run_engine(
            session, defn, EngineLimits(lim.max_nodes, lim.max_edges, lim.max_depth, budget()), checkpoint=checkpoint
        )
        basis = "SAME_AS_RUN" if orig.input_hash == run.graph_hash else "CHANGED_SINCE_RUN"
    if basis == "CHANGED_SINCE_RUN":
        warnings.append({"code": "GRAPH_CHANGED_SINCE_RUN", "text": "The network data changed after the source simulation run. This analysis uses the network as read now; counts may differ from the run's results."})  # fmt: skip

    # 1. combined view and each event's own contribution ---------------------------------------------------------------
    combined = eng(events)
    reasons += [f"engine_{r}" for r in combined.truncation_reasons]
    failures += combined.failures
    per_event: dict[str, dict[NodeKey, dict[str, Any]]] = {}
    if len(events) == 1:
        per_event[events[0]["event_uid"]] = {_k(r): r for r in combined.impacts}
    else:
        for ev in events:
            if over_budget():
                reasons.append("time_budget")
                break
            r = eng([ev])
            per_event[ev["event_uid"]] = {_k(i): i for i in r.impacts}
            checkpoint()
    rows = {_k(r): r for r in combined.impacts if r["impact_kind"] in ("DIRECT", "DOWNSTREAM")}
    if len(rows) > lim.max_findings:
        keep = sorted(rows, key=lambda k: (rows[k]["depth"], k[0].value, k[1]))[: lim.max_findings]
        rows = {k: rows[k] for k in keep}
        reasons.append("findings_cap")
    checkpoint()

    # 2. seeds, attributed edges and the relationship records that were actually read ----------------------------------
    types = set(FLOW_TYPES) | ({GraphRelType.OWNS} if assump.get("include_ownership") else set())
    flt = EdgeFilter(types=frozenset(types), statuses=frozenset({EdgeStatus.CONFIRMED}), as_of=conf.as_of_date)
    node_seeds: dict[str, list[NodeKey]] = {}
    edge_seed_refs: dict[str, list[str]] = defaultdict(list)
    for ev in events:
        uid = ev["event_uid"]
        if ev.get("relationship_ref") and EventType(ev["event_type"]) in (
            EventType.RELATIONSHIP_UNAVAILABLE,
            EventType.LEAD_TIME_INCREASE,
        ):
            edge_seed_refs[uid].append(ev["relationship_ref"])
        else:
            node_seeds[uid] = event_targets(ev)
    initiating = {k for ks in node_seeds.values() for k in ks}
    edge_rows = {e["edge_ref"]: e for e in combined.edge_impacts}
    keys_for_edges = sorted(set(rows) | initiating, key=lambda k: (k[0].value, k[1]))
    out_edges, more = fetch_incident(session, keys_for_edges, "out", flt, lim.max_edges * 2)
    if more:
        reasons.append("edge_limit")
    recs: dict[str, EdgeRec] = {e.ref: e for e in out_edges if e.ref in edge_rows}
    for ref in sorted(set(edge_rows) | {r for rs in edge_seed_refs.values() for r in rs}):
        if ref not in recs:
            kind, rid = parse_ref(ref)
            rec = get_rec(session, kind, rid)
            if rec is not None:
                recs[ref] = rec
    event_edges: dict[str, list[EdgeRec]] = defaultdict(list)
    for ref, er in edge_rows.items():
        if ref in recs:
            for uid in er["event_uids"]:
                event_edges[uid].append(recs[ref])
    edge_seeds = {uid: [recs[r] for r in refs if r in recs] for uid, refs in edge_seed_refs.items()}
    roots: set[NodeKey] = set(initiating) | {e.destination for es in edge_seeds.values() for e in es}
    checkpoint()

    # 3. bounded multi-path enumeration ----------------------------------------------------------------------------------
    idx = enumerate_paths(
        event_edges, node_seeds, edge_seeds, max_length=depth,
        max_store_per_event=conf.max_paths_per_entity, max_steps=lim.max_path_steps,
    )  # fmt: skip
    if not idx.complete:
        reasons.append("path_enumeration_cap")
        warnings.append({"code": "PATHS_TRUNCATED", "text": "Path enumeration reached its step cap; path counts are lower bounds and event-overlap could not be fully established."})  # fmt: skip
    checkpoint()

    # 4. inbound context: concentration, alternatives, conflicting shares ---------------------------------------------
    affected = set(rows)
    targets_fp = sorted((k for k in affected if k[0] in (F, P)), key=lambda k: (k[0].value, k[1]))
    in_types = frozenset(FLOW_TYPES - {GraphRelType.STORES})
    in_flt = EdgeFilter(
        types=in_types,
        statuses=frozenset({EdgeStatus.CONFIRMED} | ({EdgeStatus.PROPOSED} if conf.include_proposed_alternatives else set())),
        as_of=conf.as_of_date,
    )  # fmt: skip
    inbound: dict[NodeKey, list[EdgeRec]] = defaultdict(list)
    if targets_fp and not over_budget():
        in_edges, more_in = fetch_incident(session, targets_fp, "in", in_flt, lim.max_edges * 3)
        if more_in:
            reasons.append("inbound_edge_limit")
        for e in in_edges:
            inbound[e.destination].append(e)
    checkpoint()

    node_ids = set(rows) | initiating | {e.source for es in inbound.values() for e in es}
    nodes: dict[NodeKey, NodeRec] = load_nodes(session, sorted(node_ids, key=lambda k: (k[0].value, k[1])))

    # 5. structural subgraph, criticality ----------------------------------------------------------------------------------
    adj: dict[NodeKey, set[NodeKey]] = defaultdict(set)
    for e in recs.values():
        adj[e.source].add(e.destination)
    for es in edge_seeds.values():
        for e in es:
            adj[e.source].add(e.destination)
    base_reach = reachable(adj, roots)
    rr = roots_reaching(adj, roots)

    # 6. alternatives ------------------------------------------------------------------------------------------------------
    alt_rows, alt_edges, alt_count = _alternatives(
        session, conf, lim, ref_date, targets_fp, inbound, recs, affected, nodes, reasons, over_budget
    )
    nodes.update(
        load_nodes(
            session, [k for r in alt_rows for k in [(RegistryType(r["source_type"]), r["source_id"])] if k not in nodes]
        )
    )
    for r in alt_rows:
        n = nodes.get((RegistryType(r["source_type"]), r["source_id"]))
        r["source_name"] = n.name if n else r["source_name"]
    checkpoint()

    # 7. quality scores of the relationships involved (data quality, read-only) -----------------------------------------------
    qscores = _quality(session, set(recs) | set(alt_edges))

    # 8. findings ----------------------------------------------------------------------------------------------------------
    ev_by_uid = {ev["event_uid"]: ev for ev in events}
    findings: list[dict[str, Any]] = []
    for k in sorted(rows, key=lambda k: (rows[k]["depth"], k[0].value, k[1])):
        findings.append(
            _finding(
                k,
                rows[k],
                per_event,
                idx,
                recs,
                nodes,
                ev_by_uid,
                inbound,
                conf,
                ref_date,
                qscores,
                alt_count.get(k, 0),
                initiating,
                combined_assumptions=assump,
            )  # fmt: skip
        )
    crit = _criticality(adj, roots, base_reach, rr, idx, nodes, rows, initiating, inbound, alt_count, session)
    snapshots = _snapshots(recs, alt_edges, inbound)

    # 9. exclusions explained -----------------------------------------------------------------------------------------------
    excluded = _excluded(session, conf, keys_for_edges, recs, types)

    # 10. aggregates -------------------------------------------------------------------------------------------------------
    summary, coverage, unc = _aggregate(findings, crit, alt_rows, idx, rows, per_event, events, run, basis)
    ev_out = _event_explanations(events, per_event, rows, nodes, node_seeds, edge_seeds)
    if basis == "CHANGED_SINCE_RUN":
        unc.append({"code": "GRAPH_CHANGED_SINCE_RUN", "count": 1, "text": "The network changed after the source run."})
    return AnalysisResult(
        findings=findings, criticality=crit, alternatives=alt_rows, edge_snapshots=snapshots, summary=summary, coverage=coverage,
        uncertainty=unc, events=ev_out, warnings=warnings + [{"code": w["code"], "text": w["text"]} for w in combined.warnings if w["code"] != "NO_BUFFER_MODEL"] + [{"code": "NO_BUFFER_MODEL", "text": cfg.REACHABILITY_NOTE}],
        failures=failures, excluded=excluded, truncated=bool(reasons), truncation_reasons=sorted(set(reasons)),
        graph_hash=combined.input_hash, graph_basis=basis, source_engine_version=run.engine_version,
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------------------- findings


def _combine(
    k: NodeKey, row: dict[str, Any], contribs: list[dict[str, Any]], idx: PathIndex, initiating: set[NodeKey]
) -> tuple[str, dict[str, Any]]:
    """Decide how several initiating events combine at one entity. Returns (interaction, combined-info)."""
    uids = [c["event_uid"] for c in contribs]
    known = [c["loss_pct"] for c in contribs if c["loss_pct"] is not None]
    info: dict[str, Any] = {
        "events": uids, "per_event_loss_pct": {c["event_uid"]: c["loss_pct"] for c in contribs},
        "sum_of_event_values_pct": round(sum(known), 4) if len(known) == len(contribs) else None,
    }  # fmt: skip
    if len(uids) <= 1:
        info.update(status="SINGLE_EVENT", rule="One initiating event reaches this entity; nothing to combine.")
        return "SINGLE_EVENT", info
    if k in initiating:
        info.update(
            status="ASSUMPTION_DEPENDENT",
            rule="This entity is itself a target of the scenario; its own assumed loss applies and other events' inflow effects are not added to it.",
        )  # fmt: skip
        return "OVERLAPPING", info
    if not idx.complete:
        interaction = "UNDETERMINED"
    else:
        overlap = False
        for i, a in enumerate(uids):
            for b in uids[i + 1 :]:
                ua, ub = idx.used.get((k, a)), idx.used.get((k, b))
                if ua is None or ub is None or (ua[0] & ub[0]) or (ua[1] & ub[1]):
                    overlap = True
        interaction = "OVERLAPPING" if overlap else "INDEPENDENT"
    quantified = row["loss_pct"] is not None
    if not quantified:
        info.update(status="INDETERMINATE", rule="The events' combined effect cannot be computed because at least one needed supply share is unknown; contributions are reported separately.")  # fmt: skip
    elif interaction == "INDEPENDENT" and info["sum_of_event_values_pct"] is not None:
        total = min(100.0, info["sum_of_event_values_pct"])
        if abs(total - row["loss_pct"]) <= 1e-6:
            info.update(status="ADDITIVE_INDEPENDENT", rule="The events reach this entity through disjoint paths, so their lost shares of inflow add (capped at 100%).")  # fmt: skip
        else:
            info.update(status="INDETERMINATE", rule="Per-event values do not reproduce the combined value; the combination is not reported as exact.")  # fmt: skip
    else:
        info.update(status="ASSUMPTION_DEPENDENT", rule="Events share paths or sources. Overlapping reductions at one source are not stacked (the larger fraction applies), so the combined value depends on that assumption.")  # fmt: skip
    if known:
        info["bounds_pct"] = {"min": round(max(known), 4), "max": round(min(100.0, sum(known)), 4)}
    info["not_additive"] = interaction != "INDEPENDENT"
    return interaction, info


def _finding(
    k,
    row,
    per_event,
    idx,
    recs,
    nodes,
    ev_by_uid,
    inbound,
    conf,
    ref_date,
    qscores,
    alt_n,
    initiating,
    combined_assumptions,
):  # noqa: ANN001, C901
    n = nodes[k]
    depth = row["depth"]
    is_init = k in initiating
    classification = "INITIATING" if is_init else ("DIRECT" if depth <= 1 else "PROPAGATED")
    uids = list(row["event_uids"])
    contribs = []
    for uid in uids:
        r = per_event.get(uid, {}).get(k)
        contribs.append({
            "event_uid": uid, "depth": r["depth"] if r else None, "loss_pct": r["loss_pct"] if r else None,
            "loss_pct_lower_bound": r["loss_pct_lower_bound"] if r else None, "quantification": r["quantification"] if r else "UNQUANTIFIED",
            "added_lead_time_days": r["added_lead_time_days"] if r else None, "paths_found": idx.per_event.get((k, uid), 0),
        })  # fmt: skip
    interaction, comb = _combine(k, row, contribs, idx, initiating)
    status = comb["status"]
    loss = row["loss_pct"] if status != "INDETERMINATE" else None
    lower = row["loss_pct_lower_bound"]

    # paths: first stored path of every event, then fill by length --------------------------------------------------------
    stored = sorted(idx.stored.get(k, []), key=lambda x: (len(x[1]), x[0], x[1]))
    chosen: list[tuple[str, tuple[str, ...]]] = []
    seen_ev: set[str] = set()
    for uid, refs in stored:
        if uid not in seen_ev:
            chosen.append((uid, refs))
            seen_ev.add(uid)
    for item in stored:
        if len(chosen) >= max(conf.max_paths_per_entity, len(seen_ev)):
            break
        if item not in chosen:
            chosen.append(item)
    chosen.sort(key=lambda x: (len(x[1]), x[0], x[1]))
    paths = []
    for uid, refs in chosen:
        steps = []
        for ref in refs:
            e = recs.get(ref)
            if e is None:
                continue
            fn, tn = nodes.get(e.source), nodes.get(e.destination)
            steps.append({
                "ref": ref, "type": e.type.value, "from": {"type": e.source[0].value, "id": e.source[1], "name": fn.name if fn else None},
                "to": {"type": e.destination[0].value, "id": e.destination[1], "name": tn.name if tn else None},
                "supply_share_pct": e.supply_share_pct, "status": e.status.value,
            })  # fmt: skip
        paths.append({"event_uid": uid, "length": len(refs), "steps": steps})
    path_count = idx.count.get(k, 0)

    # edges that matter: parents + path edges --------------------------------------------------------------------------------
    parent_refs = list(row.get("parent_edges") or [])
    path_refs = [s["ref"] for p in paths for s in p["steps"]]
    ev_refs = list(dict.fromkeys(parent_refs + path_refs))
    evid_edges = [recs[r] for r in ev_refs if r in recs][: cfg.MAX_EVIDENCE_PER_FINDING]

    # coverage: known / needed inputs over the affected inbound relationships ---------------------------------------------
    has_delay = row["added_lead_time_days"] is not None
    needed = known = 0
    missing: dict[str, int] = defaultdict(int)
    for r in parent_refs:
        e = recs.get(r)
        if e is None:
            continue
        fields = [("lead_time_days", e.lead_time_days)] if has_delay else []
        fields.append(("capacity_value", e.capacity_value))
        if e.type not in QUALITATIVE_EDGE_TYPES:
            fields.append(("supply_share_pct", e.supply_share_pct))
        for name, val in fields:
            needed += 1
            if val is None:
                missing[name] += 1
            else:
                known += 1
    coverage = None if needed == 0 else round(known / needed, 4)
    cov_detail = {"known_inputs": known, "needed_inputs": needed, "missing_by_field": dict(missing), "basis": "inputs on the affected inbound relationships (share, capacity, lead time where a delay applies)"}  # fmt: skip

    # confidence: weakest recorded confidence along the primary path (a descriptive grade, not a probability) -----------------
    unc: list[dict[str, str]] = []
    primary = paths[0]["steps"] if paths else []
    confs = [(s["ref"], recs[s["ref"]].confidence) for s in primary if s["ref"] in recs]
    if is_init and not confs:
        conf_val, strength = None, "N/A"
        cbasis = {"note": "Initiating entity: no inbound relationship is needed for this finding."}
    elif not confs or any(c is None for _, c in confs):
        conf_val, strength = None, "UNKNOWN"
        cbasis = {"weakest_edge": None, "per_edge": [{"ref": r, "confidence": c} for r, c in confs], "note": "At least one relationship on the primary path has no recorded confidence."}  # fmt: skip
        unc.append(
            {
                "code": "CONFIDENCE_UNKNOWN",
                "text": "At least one relationship on the primary path has no recorded confidence.",
            }
        )
    else:
        weakest = min(confs, key=lambda x: (x[1], x[0]))
        conf_val = weakest[1]
        strength = (
            "STRONGER"
            if conf_val >= cfg.STRONGER_CONFIDENCE
            else ("MODERATE" if conf_val >= cfg.MODERATE_CONFIDENCE else "LIMITED")
        )
        cbasis = {"weakest_edge": weakest[0], "per_edge": [{"ref": r, "confidence": c} for r, c in confs], "note": "Weakest recorded relationship confidence on the primary path. A descriptive grade, not a calibrated probability."}  # fmt: skip
        if strength == "LIMITED":
            unc.append(
                {
                    "code": "LOW_CONFIDENCE",
                    "text": f"Relationship {weakest[0]} has a recorded confidence of {conf_val:g}.",
                }
            )

    # uncertainty -----------------------------------------------------------------------------------------------------------------
    for u in row["uncertainty"]:
        unc.append({"code": "ENGINE", "text": u})
    for e in evid_edges:
        if _stale(e, ref_date, conf.stale_after_days):
            unc.append(
                {
                    "code": "STALE_RELATIONSHIP",
                    "text": f"Relationship {e.ref} was last updated more than {conf.stale_after_days} days before {ref_date.isoformat()}.",
                }
            )
        if _is_synthetic(e):
            unc.append(
                {
                    "code": "SYNTHETIC_EVIDENCE",
                    "text": f"Relationship {e.ref} carries synthetic or simulated provenance.",
                }
            )
        if conf.as_of_date and e.effective_from is None and e.effective_to is None:
            unc.append(
                {
                    "code": "DATES_UNSPECIFIED",
                    "text": f"Relationship {e.ref} has no effective dates, so it was treated as effective on {conf.as_of_date.isoformat()}.",
                }
            )
    inb = [e for e in inbound.get(k, []) if e.status is EdgeStatus.CONFIRMED]
    shares = [e.supply_share_pct for e in inb]
    if all(s is not None for s in shares) and inb and sum(shares) > 100 + cfg.SUM_TOLERANCE_PCT:
        unc.append({"code": "CONFLICTING_SHARES", "text": f"Recorded inbound supply shares sum to {sum(shares):g}% (above 100%); the data conflict, so shares are not relied on for exactness."})  # fmt: skip
    if interaction in ("OVERLAPPING", "UNDETERMINED"):
        unc.append({"code": "EVENT_INTERACTION", "text": comb["rule"] if interaction == "OVERLAPPING" else "Event overlap could not be established because path enumeration was capped."})  # fmt: skip
    if status == "INDETERMINATE":
        unc.append({"code": "COMBINED_INDETERMINATE", "text": comb["rule"]})
    if idx.per_event and path_count > sum(1 for _ in paths) and not idx.complete:
        unc.append({"code": "PATHS_TRUNCATED", "text": "Path enumeration was capped; the path count is a lower bound."})
    unc = list({(u["code"], u["text"]): u for u in unc}.values())

    # dimensions -----------------------------------------------------------------------------------------------------------------
    ev_defs = [ev_by_uid[u] for u in uids if u in ev_by_uid]
    durs = [e.get("duration_days") for e in ev_defs]
    dims: dict[str, Any] = {
        "supplier_exposure": _dim(QUALITATIVE, True)
        if k[0] is S
        else _dim(NOT_APPLICABLE, None, reason="Entity is not a supplier."),
        "facility_exposure": _dim(QUALITATIVE, True)
        if k[0] is F
        else _dim(NOT_APPLICABLE, None, reason="Entity is not a facility."),
        "product_exposure": _dim(QUALITATIVE, True)
        if k[0] is P
        else _dim(NOT_APPLICABLE, None, reason="Entity is not a product."),
        "relationship_exposure": _dim(QUALITATIVE, len(parent_refs), unit="affected inbound relationships")
        if parent_refs
        else _dim(NOT_APPLICABLE, 0, reason="No inbound relationship is involved (initiating entity)."),  # fmt: skip
    }
    if is_init and k[0] is F and row["capacity_lost_value"] is not None:
        dims["capacity_shortfall"] = _dim(QUANTIFIED, row["capacity_lost_value"], unit=row["capacity_unit"], assumption="Scenario-assumed lost fraction x the facility's recorded capacity.")  # fmt: skip
    else:
        why = "Suppliers have no capacity field in the registry." if k[0] is S else (
            "A facility's output cannot be derived from lost inflow: input quantity per unit of output, throughput and bill-of-materials data are not recorded." if k[0] is F else "Products have no capacity or throughput of their own in the registry.")  # fmt: skip
        dims["capacity_shortfall"] = _dim(UNAVAILABLE, None, reason=why, missing=["input quantity per unit output", "production capacity", "throughput"])  # fmt: skip
    caps: dict[str, float] = defaultdict(float)
    unknown_caps = 0
    for r in parent_refs:
        e = recs.get(r)
        if e is not None:
            if e.capacity_value is None:
                unknown_caps += 1
            else:
                caps[e.capacity_unit or "unspecified"] += e.capacity_value
    dims["relationship_capacity_at_risk"] = (
        _dim(
            QUANTIFIED,
            {u: round(v, 6) for u, v in sorted(caps.items())},
            unit="per recorded unit (not converted)",
            note="Recorded capacity of the affected inbound relationships. Not a shortfall.",
            excluded_unknown=unknown_caps,
        )  # fmt: skip
        if caps
        else _dim(
            UNAVAILABLE if parent_refs else NOT_APPLICABLE,
            None,
            reason="No affected inbound relationship has a recorded capacity."
            if parent_refs
            else "No inbound relationship is involved.",
        )  # fmt: skip
    )
    if is_init:
        dims["supply_shortfall"] = _dim(QUANTIFIED, loss, unit="% of the entity's output (scenario assumption)", assumption="Taken directly from the scenario; not observed.")  # fmt: skip
    elif loss is not None:
        dims["supply_shortfall"] = _dim(QUANTIFIED, loss, unit="% of recorded inbound supply share", assumption=comb.get("rule"), lower_bound=lower)  # fmt: skip
    elif lower:
        dims["supply_shortfall"] = _dim(LOWER_BOUND, lower, unit="% of recorded inbound supply share", reason="Some affected inflow has an unknown supply share.", missing=["supply_share_pct on affected inbound relationships"])  # fmt: skip
    else:
        dims["supply_shortfall"] = _dim(UNAVAILABLE, None, reason="Supply shares needed to size the loss are not recorded." if parent_refs else "No quantity can be derived.", missing=["supply_share_pct"])  # fmt: skip
    dims["delivery_delay"] = (
        _dim(
            QUANTIFIED,
            row["added_lead_time_days"],
            unit="days added along the path (no buffer assumed)",
            assumption="Added days accumulate along the path; inventory buffers are not modelled.",
        )  # fmt: skip
        if has_delay
        else _dim(NOT_APPLICABLE, None, reason="No lead-time event reaches this entity.")
    )
    if durs and all(d is not None for d in durs):
        dims["disruption_duration"] = _dim(QUANTIFIED, max(durs), unit="days (longest initiating-event duration)", note="Duration of the initiating event(s) as stated by the scenario. Downstream recovery time is not inferred.")  # fmt: skip
    else:
        dims["disruption_duration"] = _dim(UNAVAILABLE, None, reason="At least one contributing event has no stated duration; recovery times are never inferred.", missing=["duration_days"])  # fmt: skip
    if k[0] in (F, P) and inb:
        src_keys = {e.source for e in inb}
        if all(s is not None for s in shares):
            hhi = round(sum((s / 100.0) ** 2 for s in shares) * 10000, 1)
            dims["dependency_concentration"] = _dim(QUANTIFIED, {"known_confirmed_sources": len(src_keys), "largest_share_pct": max(shares), "hhi": hhi}, unit="shares are percentages of recorded supply")  # fmt: skip
        else:
            dims["dependency_concentration"] = _dim(QUALITATIVE, {"known_confirmed_sources": len(src_keys), "sole_known_source": len(src_keys) == 1}, reason="Not every inbound share is recorded, so no concentration index is computed.")  # fmt: skip
    else:
        dims["dependency_concentration"] = _dim(NOT_APPLICABLE if k[0] is S or is_init else UNAVAILABLE, None, reason="Not applicable to suppliers and initiating entities." if k[0] is S or is_init else "No confirmed inbound relationships are recorded.")  # fmt: skip
    dims["shared_upstream"] = _dim(QUALITATIVE, {"initiating_events": len(uids), "affected_inbound_relationships": len(parent_refs), "distinct_paths": path_count, "convergence": path_count > 1 or len(parent_refs) > 1})  # fmt: skip
    dims["evidence_completeness"] = _dim(QUANTIFIED if coverage is not None else NOT_APPLICABLE, coverage, unit="fraction of needed inputs known", confidence=conf_val, evidence_strength=strength)  # fmt: skip

    if is_init:
        state = "SCENARIO_ASSUMED"
    elif status == "INDETERMINATE":
        state = "INDETERMINATE"
    elif loss is not None:
        state = "ASSUMPTION_DEPENDENT" if status == "ASSUMPTION_DEPENDENT" else "QUANTIFIED"
    elif has_delay and row["quantification"] == "QUANTIFIED" and not row["loss_pct_lower_bound"]:
        state = "ASSUMPTION_DEPENDENT"
    else:
        state = "POTENTIAL_EXPOSURE"

    assumptions = [
        "Only CONFIRMED relationships propagate impact; PROPOSED, REJECTED and INACTIVE do not.",
        "Overlapping events at one source do not stack: the larger lost fraction applies.",
        "No inventory, safety-stock, demand or substitution is modelled.",
        "The scenario's magnitudes are hypothetical assumptions, not observations.",
    ]
    if conf.as_of_date:
        assumptions.append(
            f"Relationships are taken as effective on {conf.as_of_date.isoformat()}; undated relationships are treated as always effective."
        )

    where = {"INITIATING": "is a named target of the scenario", "DIRECT": "is directly dependent on a disrupted entity or relationship", "PROPAGATED": f"is reached through {max(0, depth - 1)} intermediate tier(s)"}[classification]  # fmt: skip
    expl = (
        f"{n.name} {where} (minimum {depth} hop{'s' if depth != 1 else ''} from the disruption). {row['explanation']}"
    )
    if path_count > 1:
        expl += f" {path_count} distinct paths were found within the limits; the entity is counted once."
    if len(uids) > 1:
        expl += f" Combination of {len(uids)} initiating events: {comb['rule']}"
    return {
        "entity_type": k[0], "entity_id": k[1], "entity_code": n.code, "entity_name": n.name, "classification": classification,
        "min_depth": depth, "quantification": row["quantification"], "exposure_level": row["exposure_level"], "impact_state": state,
        "loss_pct": loss, "loss_pct_lower_bound": lower, "added_lead_time_days": row["added_lead_time_days"], "path_count": path_count,
        "paths_complete": idx.complete, "paths": paths, "event_uids": uids, "per_event": contribs, "interaction": interaction, "combined": comb,
        "dimensions": dims, "confidence": conf_val, "evidence_strength": strength, "confidence_basis": cbasis, "coverage": coverage,
        "coverage_detail": cov_detail, "data_quality": _finding_quality(evid_edges, qscores), "uncertainty": unc,
        "evidence": [_evidence(e, ref_date, conf.stale_after_days) for e in evid_edges], "assumptions": assumptions, "rules": row["rules"],
        "explanation": expl + " " + cfg.REACHABILITY_NOTE, "alternatives_count": alt_n,
    }  # fmt: skip


def _dim(status: str, value: Any, **extra: Any) -> dict[str, Any]:
    d = {"status": status, "value": value}
    d.update({k: v for k, v in extra.items() if v is not None})
    return d


def _evidence(e: EdgeRec, ref_date: date, stale_days: int) -> dict[str, Any]:
    return {
        "ref": e.ref, "type": e.type.value, "status": e.status.value, "provenance": e.provenance, "source_namespace": e.source_namespace,
        "confidence": e.confidence, "supply_share_pct": e.supply_share_pct, "capacity_value": e.capacity_value, "capacity_unit": e.capacity_unit,
        "lead_time_days": e.lead_time_days, "effective_from": e.effective_from.isoformat() if e.effective_from else None,
        "effective_to": e.effective_to.isoformat() if e.effective_to else None,
        "last_updated": e.updated_at.isoformat() if e.updated_at else None, "stale": _stale(e, ref_date, stale_days),
        "synthetic": _is_synthetic(e), "version": e.version,
    }  # fmt: skip


def _quality(session: Session, refs: set[str]) -> dict[str, Any]:
    by_kind: dict[EntityKind, list[int]] = defaultdict(list)
    for r in refs:
        kind, rid = parse_ref(r)
        if kind is EdgeKind.SUPPLY:
            by_kind[EntityKind.RELATIONSHIP].append(rid)
        elif kind is EdgeKind.PRODUCT:
            by_kind[EntityKind.PRODUCT_LINK].append(rid)
    out: dict[str, Any] = {}
    for kind, ids in by_kind.items():
        prefix = "SUPPLY" if kind is EntityKind.RELATIONSHIP else "PRODUCT"
        for rid, sc in quality_engine.latest_scores(session, kind, sorted(ids)).items():
            out[f"{prefix}:{rid}"] = {"state": sc.state.value, "score": sc.score, "formula": sc.formula_version}
    return out


def _finding_quality(edges: list[EdgeRec], q: dict[str, Any]) -> dict[str, Any]:
    scored = [q[e.ref] for e in edges if e.ref in q and q[e.ref]["state"] == "SCORED" and q[e.ref]["score"] is not None]
    return {
        "relationships_considered": len(edges), "scored": len(scored), "not_evaluated": sum(1 for e in edges if e.ref not in q),
        "lowest_score": min((s["score"] for s in scored), default=None),
        "note": "Data-quality scores of the relationships behind this finding; a missing score is unknown, not good.",
    }  # fmt: skip


# -------------------------------------------------------------------------------------------------------- alternatives


def _alternatives(session, conf, lim, ref_date, targets, inbound, recs, affected, nodes, reasons, over_budget):  # noqa: ANN001
    rows: list[dict[str, Any]] = []
    alt_edges: dict[str, EdgeRec] = {}
    counts: dict[NodeKey, int] = defaultdict(int)
    if conf.alternatives_per_entity == 0 or not targets or over_budget():
        return rows, alt_edges, counts
    cands: dict[NodeKey, list[EdgeRec]] = {}
    for t in targets:
        cs = [e for e in inbound.get(t, []) if e.ref not in recs and e.source not in affected]
        cs.sort(
            key=lambda e: (
                e.status is not EdgeStatus.CONFIRMED,
                e.supply_share_pct is None,
                -(e.supply_share_pct or 0.0),
                e.ref,
            )
        )
        cands[t] = cs[: conf.alternatives_per_entity]
    srcs = sorted({e.source for cs in cands.values() for e in cs}, key=lambda k: (k[0].value, k[1]))
    up: dict[NodeKey, EdgeRec] = {}
    if srcs:
        up_flt = EdgeFilter(
            types=frozenset(FLOW_TYPES - {GraphRelType.STORES}),
            statuses=frozenset({EdgeStatus.CONFIRMED}),
            as_of=conf.as_of_date,
        )
        ins, _more = fetch_incident(session, srcs, "in", up_flt, lim.max_edges)
        for e in sorted(ins, key=lambda e: (e.supply_share_pct is None, -(e.supply_share_pct or 0.0), e.ref)):
            if e.source not in affected and e.destination not in up:
                up[e.destination] = e
    nodes.update(load_nodes(session, [e.source for e in up.values() if e.source not in nodes]))
    nodes_needed = [e.source for cs in cands.values() for e in cs if e.source not in nodes]
    nodes.update(load_nodes(session, nodes_needed))
    for t in targets:
        for e in cands[t]:
            if len(rows) >= cfg.MAX_ALTERNATIVES_TOTAL:
                reasons.append("alternatives_cap")
                return rows, alt_edges, counts
            chain = [e]
            if e.source in up:
                chain.insert(0, up[e.source])
            for c in chain:
                alt_edges[c.ref] = c
            states = []
            for c in chain:
                if c.status is not EdgeStatus.CONFIRMED:
                    states.append("PROPOSED")
                elif _stale(c, ref_date, conf.stale_after_days):
                    states.append("STALE")
                elif c.confidence is None or c.confidence < cfg.MODERATE_CONFIDENCE:
                    states.append("UNCERTAIN")
                else:
                    states.append("ACTIVE")
            path_state = next((s for s in ("PROPOSED", "STALE", "UNCERTAIN") if s in states), "ACTIVE")
            miss = ["Spare capacity at the alternate source is not recorded and is never inferred.", "Product compatibility, certification and qualification are not modelled.", "Transport access and logistics are not modelled."]  # fmt: skip
            if e.capacity_value is None:
                miss.append("Capacity of the alternate relationship is unknown.")
            if e.supply_share_pct is None:
                miss.append("Current supply share is unknown.")
            if e.lead_time_days is None:
                miss.append("Lead time is unknown.")
            if e.confidence is None:
                miss.append("No confidence is recorded for the alternate relationship.")
            if path_state == "PROPOSED":
                miss.append("A relationship on this path is PROPOSED, not confirmed.")
            sname = nodes.get(e.source).name if nodes.get(e.source) else ""
            steps = [{"ref": c.ref, "type": c.type.value, "status": c.status.value, "from": {"type": c.source[0].value, "id": c.source[1]}, "to": {"type": c.destination[0].value, "id": c.destination[1]}, "supply_share_pct": c.supply_share_pct, "confidence": c.confidence} for c in chain]  # fmt: skip
            rows.append({
                "for_type": t[0], "for_id": t[1], "for_name": nodes[t].name if t in nodes else "", "source_type": e.source[0],
                "source_id": e.source[1], "source_name": sname, "path": steps, "path_length": len(chain), "path_state": path_state,
                "feasibility": "STRUCTURAL_ONLY", "capacity_value": e.capacity_value, "capacity_unit": e.capacity_unit,
                "supply_share_pct": e.supply_share_pct, "lead_time_days": e.lead_time_days, "missing": miss, "caveat": cfg.ALTERNATIVE_CAVEAT[:400],
            })  # fmt: skip
            counts[t] += 1
    return rows, alt_edges, counts


# -------------------------------------------------------------------------------------------------------- criticality


def _criticality(adj, roots, base_reach, rr, idx, nodes, rows, initiating, inbound, alt_count, session):  # noqa: ANN001
    cand = sorted((n for n in set(adj) if n in base_reach or n in roots), key=lambda k: (k[0].value, k[1]))[
        : cfg.MAX_CRITICALITY_NODES
    ]
    sole: dict[NodeKey, list[str]] = defaultdict(list)
    for t, es in inbound.items():
        srcs = {e.source for e in es if e.status is EdgeStatus.CONFIRMED}
        if len(srcs) == 1:
            sole[next(iter(srcs))].append(_label(t))
    out = []
    for u in cand:
        reach_u = reachable(adj, {u}) - {u}
        without = reachable(adj, roots - {u}, banned=u)
        cut = sorted(
            (v for v in base_reach if v != u and v not in without and v not in roots), key=lambda k: (k[0].value, k[1])
        )
        role = "INITIATING" if u in initiating else "INTERMEDIATE"
        n = nodes.get(u)
        if n is None:
            continue
        paths_through = idx.through.get(u, 0)
        shared = []
        if rr is not None:
            shared = [v for v in sorted(reach_u, key=lambda k: (k[0].value, k[1])) if len(rr.get(v, ())) >= 2][
                : cfg.MAX_DISCONNECT_SAMPLE
            ]
        reasons = []
        cand_flag = False
        if role == "INTERMEDIATE":
            if len(cut) >= 1 and len(reach_u) >= cfg.BOTTLENECK_MIN_DOWNSTREAM:
                reasons.append(
                    f"Removing it would cut off {len(cut)} downstream entit{'y' if len(cut) == 1 else 'ies'} from every initiating entity within the analysed subgraph (directed)."
                )
            if paths_through >= cfg.BOTTLENECK_MIN_PATHS and len(reach_u) >= cfg.BOTTLENECK_MIN_DOWNSTREAM:
                reasons.append(f"{paths_through} enumerated propagation paths pass through it.")
            cand_flag = bool(reasons)
        out.append({
            "entity_type": u[0], "entity_id": u[1], "entity_code": n.code, "entity_name": n.name, "role": role,
            "downstream_reachable": len(reach_u), "products_reachable": sum(1 for v in reach_u if v[0] is P),
            "facilities_reachable": sum(1 for v in reach_u if v[0] is F), "paths_through": paths_through, "would_disconnect": len(cut),
            "would_disconnect_sample": [_label(v) for v in cut[: cfg.MAX_DISCONNECT_SAMPLE]], "sole_known_source_of": sorted(sole.get(u, []))[: cfg.MAX_DISCONNECT_SAMPLE],
            "alternatives_for_downstream": sum(1 for v in reach_u if alt_count.get(v, 0) > 0), "bottleneck_candidate": cand_flag, "reasons": reasons,
            "shared_upstream_of": [_label(v) for v in shared], "caveat": cfg.STRUCTURAL_CAVEAT[:400],
        })  # fmt: skip
    return out


def _snapshots(
    recs: dict[str, EdgeRec], alt_edges: dict[str, EdgeRec], inbound: dict[NodeKey, list[EdgeRec]]
) -> list[dict[str, Any]]:
    out = {r: _edge_record(e, "PROPAGATION") for r, e in recs.items()}
    for r, e in alt_edges.items():
        out.setdefault(r, _edge_record(e, "ALTERNATIVE"))
    n_ctx = 0
    for es in inbound.values():
        for e in es:
            if e.ref not in out and n_ctx < cfg.MAX_CONTEXT_EDGES:
                out[e.ref] = _edge_record(e, "CONTEXT")
                n_ctx += 1
    return [out[r] for r in sorted(out)]


def _excluded(session, conf, keys, recs, types):  # noqa: ANN001
    info: dict[str, Any] = {"by_dates": [], "proposed_not_propagated": 0, "by_dates_total": 0}
    if not keys:
        return info
    if conf.as_of_date is not None:
        all_conf, _m = fetch_incident(
            session,
            keys,
            "out",
            EdgeFilter(types=frozenset(types), statuses=frozenset({EdgeStatus.CONFIRMED}), as_of=None),
            5_000,
        )
        gone = [e for e in all_conf if e.ref not in recs and not e.effective_on(conf.as_of_date)]
        info["by_dates_total"] = len(gone)
        for e in gone[:50]:
            info["by_dates"].append({"ref": e.ref, "from": _label(e.source), "to": _label(e.destination), "effective_from": e.effective_from.isoformat() if e.effective_from else None, "effective_to": e.effective_to.isoformat() if e.effective_to else None, "reason": "NOT_YET_EFFECTIVE" if e.effective_from and e.effective_from > conf.as_of_date else "EXPIRED"})  # fmt: skip
    prop, _m = fetch_incident(
        session,
        keys,
        "out",
        EdgeFilter(types=frozenset(types), statuses=frozenset({EdgeStatus.PROPOSED}), as_of=conf.as_of_date),
        5_000,
    )
    info["proposed_not_propagated"] = len(prop)
    info["note"] = "Relationships excluded from propagation: outside the as-of date, or PROPOSED (not confirmed)."
    return info


# ------------------------------------------------------------------------------------------------------------ aggregates


def _aggregate(findings, crit, alts, idx, rows, per_event, events, run, basis):  # noqa: ANN001
    by_class: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    by_state: dict[str, int] = defaultdict(int)
    for f in findings:
        by_class[f["classification"]] += 1
        by_type[f["entity_type"].value] += 1
        by_state[f["impact_state"]] += 1
    cov = [f["coverage"] for f in findings if f["coverage"] is not None]
    missing: dict[str, int] = defaultdict(int)
    codes: dict[str, dict[str, Any]] = {}
    for f in findings:
        for fld, c in f["coverage_detail"]["missing_by_field"].items():
            missing[fld] += c
        for u in f["uncertainty"]:
            d = codes.setdefault(u["code"], {"code": u["code"], "count": 0, "text": u["text"]})
            d["count"] += 1
    n_events = len(events)
    summary = {
        "scope": cfg.SCOPE_STATEMENT, "reachability_note": cfg.REACHABILITY_NOTE, "initiating_events": n_events,
        "unique_entities": len(findings), "initiating": by_class.get("INITIATING", 0), "direct": by_class.get("DIRECT", 0),
        "propagated": by_class.get("PROPAGATED", 0), "by_type": dict(by_type), "by_state": dict(by_state),
        "suppliers_exposed": by_type.get("SUPPLIER", 0), "facilities_exposed": by_type.get("FACILITY", 0), "products_exposed": by_type.get("PRODUCT", 0),
        "quantified": sum(1 for f in findings if f["impact_state"] in ("QUANTIFIED", "ASSUMPTION_DEPENDENT", "SCENARIO_ASSUMED")),
        "unquantified": sum(1 for f in findings if f["impact_state"] in ("POTENTIAL_EXPOSURE", "INDETERMINATE")),
        "indeterminate": by_state.get("INDETERMINATE", 0),
        "distinct_paths_found": sum(idx.count.values()), "distinct_paths_exact": idx.complete,
        "multi_path_entities": sum(1 for f in findings if f["path_count"] > 1),
        "multi_event_entities": sum(1 for f in findings if len(f["event_uids"]) > 1),
        "overlapping_entities": sum(1 for f in findings if f["interaction"] == "OVERLAPPING"),
        "independent_entities": sum(1 for f in findings if f["interaction"] == "INDEPENDENT"),
        "bottleneck_candidates": sum(1 for c in crit if c["bottleneck_candidate"]),
        "alternatives_total": len(alts), "entities_with_alternatives": len({(a["for_type"], a["for_id"]) for a in alts}),
        "source_run": {"run_id": run.id, "run_uid": run.run_uid, "status": run.status.value, "engine_version": run.engine_version},
        "graph_basis": basis,
        "counting_rule": "Headline totals count each entity once. Paths and contributions are preserved separately and are never summed into totals.",
    }  # fmt: skip
    coverage = {
        "findings_with_inputs_applicable": len(cov), "mean_input_coverage": round(sum(cov) / len(cov), 4) if cov else None,
        "full_coverage": sum(1 for c in cov if c >= 1.0), "missing_inputs_by_field": dict(missing),
        "evidence_strength": {s: sum(1 for f in findings if f["evidence_strength"] == s) for s in ("STRONGER", "MODERATE", "LIMITED", "UNKNOWN", "N/A")},
        "note": "Coverage = known / needed inputs on the affected inbound relationships. It is separate from confidence and from uncertainty.",
    }  # fmt: skip
    return summary, coverage, sorted(codes.values(), key=lambda d: (-d["count"], d["code"]))


def _event_explanations(events, per_event, rows, nodes, node_seeds, edge_seeds):  # noqa: ANN001
    out = []
    for ev in events:
        uid = ev["event_uid"]
        reached = per_event.get(uid, {})
        targets = [
            {"type": k[0].value, "id": k[1], "name": nodes[k].name if k in nodes else None}
            for k in node_seeds.get(uid, [])
        ]
        et = EventType(ev["event_type"])
        out.append({
            "event_uid": uid, "event_type": et.value, "label": ev.get("label"), "targets": targets,
            "relationship_refs": [e.ref for e in edge_seeds.get(uid, [])] or ([ev["relationship_ref"]] if ev.get("relationship_ref") else []),
            "magnitude_pct": ev.get("magnitude_pct"), "added_lead_time_days": ev.get("added_lead_time_days"), "duration_days": ev.get("duration_days"),
            "recovery_days": ev.get("recovery_days"), "region_label": ev.get("region_label"),
            "entities_reached": len(reached), "reached_direct": sum(1 for r in reached.values() if r["depth"] == 1),
            "timing": "UNSPECIFIED: propagation timing is not inferred from graph depth." if ev.get("duration_days") is None else f"Stated duration {ev['duration_days']} days; propagation timing is not inferred from graph depth.",
            "explanation": f"Initiating event {et.value}: {len(targets) or len(edge_seeds.get(uid, []))} initiating target(s) reach {len(reached)} entities on their own.",
        })  # fmt: skip
    return out
