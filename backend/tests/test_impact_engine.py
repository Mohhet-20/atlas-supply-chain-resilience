"""Cascading impact engine: classification, paths, overlap, quantification, criticality, alternatives, determinism."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.impact import config as cfg
from app.impact import runs as iruns
from app.impact.engine import ImpactLimits
from app.impact.structure import enumerate_paths
from app.models.enums import RegistryType, SimStatus
from app.models.impact import ImpactAlternative, ImpactCriticality, ImpactEdgeSnapshot, ImpactFinding
from tests.graph_helpers import node, ok
from tests.impact_helpers import analyze, findings, linear, make_run, realistic, rows_of, sup_event


def test_single_supplier_classification_and_scope(client):
    w = linear(client)
    a = analyze(client, make_run(client, [sup_event(w["a"])]))
    assert a.status is SimStatus.COMPLETED and a.algorithm_version == "ci-1" and len(a.graph_hash) == 64
    f = findings(client, a.id)
    assert set(f) == {"Alder Parts", "Cedar Plant", "Dogwood Hub", "Elm Widget"}
    assert f["Alder Parts"].classification == "INITIATING" and f["Alder Parts"].impact_state == "SCENARIO_ASSUMED"
    assert f["Cedar Plant"].classification == "DIRECT" and f["Cedar Plant"].min_depth == 1
    assert f["Dogwood Hub"].classification == "PROPAGATED" and f["Dogwood Hub"].min_depth == 2
    assert (
        f["Elm Widget"].min_depth == 3
        and f["Elm Widget"].impact_state == "QUANTIFIED"
        and f["Elm Widget"].loss_pct == 100.0
    )
    s = a.summary
    assert s["unique_entities"] == 4 and s["initiating"] == 1 and s["direct"] == 1 and s["propagated"] == 2
    assert "not listed" in s["scope"] and "does not mean it is safe" in s["scope"]
    assert a.graph_basis == "SAME_AS_RUN"


def test_missing_supply_share_stays_unknown(client):
    w = linear(client, shares=(None, 100.0))
    f = findings(client, analyze(client, make_run(client, [sup_event(w["a"])])).id)
    c = f["Cedar Plant"]
    assert c.loss_pct is None and c.impact_state == "POTENTIAL_EXPOSURE" and c.quantification != "QUANTIFIED"
    d = c.dimensions["supply_shortfall"]
    assert d["status"] == "UNAVAILABLE" and "supply_share_pct" in d["missing"] and d["value"] is None
    assert (
        c.coverage is not None and c.coverage < 1.0 and c.coverage_detail["missing_by_field"]["supply_share_pct"] == 1
    )
    assert any(u["code"] == "ENGINE" for u in c.uncertainty)


def test_dimensions_never_invent_values(client):
    w = linear(client)
    f = findings(client, analyze(client, make_run(client, [sup_event(w["a"])])).id)
    p = f["Elm Widget"].dimensions
    assert p["capacity_shortfall"]["status"] == "UNAVAILABLE" and p["capacity_shortfall"]["value"] is None
    assert p["delivery_delay"]["status"] == "NOT_APPLICABLE"
    assert p["product_exposure"]["status"] == "QUALITATIVE" and p["supplier_exposure"]["status"] == "NOT_APPLICABLE"
    assert p["disruption_duration"]["status"] == "QUANTIFIED" and p["disruption_duration"]["value"] == 10
    assert not any("revenue" in str(v).lower() or "financial_loss" in str(v).lower() for v in p.values())


def test_overlapping_events_no_double_counting(client):
    w = realistic(client)
    run = make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])])
    a = analyze(client, run)
    f = findings(client, a.id)
    assert a.summary["unique_entities"] == 7 == len(f)  # S1,S2,F1,F2,F3,P1,P2 each once
    assert a.summary["initiating_events"] == 2
    f1, f3, p1 = f["Facility F1"], f["Facility F3"], f["Product P1"]
    assert f1.interaction == "INDEPENDENT" and f1.combined["status"] == "ADDITIVE_INDEPENDENT" and f1.loss_pct == 100.0
    assert (
        f3.interaction == "OVERLAPPING"
        and f3.combined["status"] == "ASSUMPTION_DEPENDENT"
        and f3.impact_state == "ASSUMPTION_DEPENDENT"
    )
    assert (
        f3.combined["not_additive"] is True
        and f3.combined["bounds_pct"]["min"] <= f3.loss_pct <= f3.combined["bounds_pct"]["max"]
    )
    assert f3.path_count == 3 and len(f3.paths) == 3  # three distinct paths, one entity
    assert {p["event_uid"] for p in f3.paths} == set(f3.event_uids) and len(f3.event_uids) == 2
    per = {c["event_uid"]: c["loss_pct"] for c in f3.per_event}
    assert sorted(per.values()) == [30.0, 70.0]
    assert p1.path_count == 3 and a.summary["multi_path_entities"] >= 2
    assert a.summary["distinct_paths_found"] > a.summary["unique_entities"]  # paths are not entities
    assert a.summary["counting_rule"].startswith("Headline totals count each entity once")


def test_unquantifiable_combination_is_indeterminate(client):
    a_, b_ = node(client, "suppliers", "Alder Parts"), node(client, "suppliers", "Birch Parts")
    f = node(client, "facilities", "Cedar Plant")
    ok(client, "SUPPLIES", a_, f, supply_share_pct=50.0)
    ok(client, "SUPPLIES", b_, f)  # share unknown
    r = findings(client, analyze(client, make_run(client, [sup_event(a_), sup_event(b_)])).id)["Cedar Plant"]
    assert (
        r.interaction == "INDEPENDENT" and r.combined["status"] == "INDETERMINATE" and r.impact_state == "INDETERMINATE"
    )
    assert (
        r.loss_pct is None
        and r.combined["per_event_loss_pct"]
        and any(u["code"] == "COMBINED_INDETERMINATE" for u in r.uncertainty)
    )


def test_cycle_terminates_and_counts_once(client):
    a = node(client, "suppliers", "Alder Parts")
    f1, f2 = node(client, "facilities", "Cedar Plant"), node(client, "facilities", "Dogwood Hub")
    ok(client, "SUPPLIES", a, f1, supply_share_pct=100.0)
    ok(client, "TRANSFERS_TO", f1, f2, supply_share_pct=100.0)
    ok(client, "TRANSFERS_TO", f2, f1, supply_share_pct=10.0)
    an = analyze(client, make_run(client, [sup_event(a)]))
    f = findings(client, an.id)
    assert set(f) == {"Alder Parts", "Cedar Plant", "Dogwood Hub"} and an.status is SimStatus.COMPLETED
    assert f["Dogwood Hub"].path_count == 1  # the back edge does not create a longer path to F1


def test_disconnected_entity_is_not_reached(client):
    w = linear(client)
    node(client, "facilities", "Lonely Site")
    an = analyze(client, make_run(client, [sup_event(w["a"])]))
    assert "Lonely Site" not in findings(client, an.id)


def test_depth_limit_enforced_and_flagged(client):
    w = linear(client)
    run = make_run(client, [sup_event(w["a"])], downstream_depth=2)
    an = analyze(client, run)
    f = findings(client, an.id)
    assert "Elm Widget" not in f and max(x.min_depth for x in f.values()) == 2
    assert an.status is SimStatus.PARTIAL and any("depth" in r for r in an.truncation_reasons)


def test_path_enumeration_cap_marks_partial(client):
    w = realistic(client)
    run = make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])])
    lim = iruns.AnalysisLimits(engine=ImpactLimits(max_path_steps=1000))
    ok_run = analyze(client, run, limits=lim)
    assert ok_run.status is SimStatus.COMPLETED
    tiny = iruns.AnalysisLimits(engine=ImpactLimits(max_path_steps=2))
    a = analyze(client, run, limits=tiny)
    assert a.status is SimStatus.PARTIAL and "path_enumeration_cap" in a.truncation_reasons
    f3 = findings(client, a.id)["Facility F3"]
    assert f3.paths_complete is False and f3.interaction == "UNDETERMINED"


def test_findings_cap_marks_partial(client):
    w = linear(client)
    a = analyze(
        client, make_run(client, [sup_event(w["a"])]), limits=iruns.AnalysisLimits(engine=ImpactLimits(max_findings=2))
    )
    assert a.status is SimStatus.PARTIAL and "findings_cap" in a.truncation_reasons and len(findings(client, a.id)) == 2


def test_lead_time_event_is_quantified_only_as_assumption_dependent(client):
    w = linear(client)
    ev = {
        "event_type": "LEAD_TIME_INCREASE",
        "relationship_ref": None,
        "target_type": "SUPPLIER",
        "target_id": w["a"]["id"],
        "added_lead_time_days": 5,
        "duration_days": 20,
    }
    f = findings(client, analyze(client, make_run(client, [ev])).id)
    c = f["Cedar Plant"]
    assert c.added_lead_time_days == 5 and c.impact_state == "ASSUMPTION_DEPENDENT" and c.loss_pct is None
    assert (
        c.dimensions["delivery_delay"]["status"] == "QUANTIFIED" and "buffer" in c.dimensions["delivery_delay"]["unit"]
    )
    assert c.dimensions["supply_shortfall"]["status"] in ("UNAVAILABLE", "LOWER_BOUND")


def test_relationship_event_destination_is_direct(client):
    w = linear(client)
    rel = ok(client, "SUPPLIES", w["a"], node(client, "facilities", "Spur"), supply_share_pct=100.0)
    ev = {"event_type": "RELATIONSHIP_UNAVAILABLE", "relationship_ref": rel["ref"], "duration_days": 5}
    f = findings(client, analyze(client, make_run(client, [ev])).id)
    assert f["Spur"].classification == "DIRECT" and f["Spur"].paths[0]["steps"][0]["ref"] == rel["ref"]
    assert "Alder Parts" not in f and "Cedar Plant" not in f


def test_confidence_coverage_uncertainty_are_separate(client):
    w = realistic(client)
    f = findings(client, analyze(client, make_run(client, [sup_event(w["S2"])])).id)
    f1 = f["Facility F1"]  # primary path S2->F1 has confidence 0.6
    assert f1.confidence == 0.6 and f1.evidence_strength == "MODERATE"
    assert f1.coverage is not None and f1.coverage < 1.0  # capacity unknown on some inbound relationships
    assert f1.confidence_basis["weakest_edge"].startswith("SUPPLY:")
    assert "not a calibrated probability" in f1.confidence_basis["note"]
    assert f["Supplier S2"].evidence_strength == "N/A" and f["Supplier S2"].coverage is None


def test_conflicting_inbound_shares_are_flagged(client):
    a, b = node(client, "suppliers", "Alder Parts"), node(client, "suppliers", "Birch Parts")
    f = node(client, "facilities", "Cedar Plant")
    ok(client, "SUPPLIES", a, f, supply_share_pct=70.0)
    ok(client, "SUPPLIES", b, f, supply_share_pct=60.0)
    c = findings(client, analyze(client, make_run(client, [sup_event(a)])).id)["Cedar Plant"]
    assert any(u["code"] == "CONFLICTING_SHARES" for u in c.uncertainty)


def test_stale_synthetic_and_proposed_handling(client):
    w = linear(client)
    far = cfg.ImpactConfig(reference_date=date(2040, 1, 1), stale_after_days=30)
    an = analyze(client, make_run(client, [sup_event(w["a"])]), far)
    c = findings(client, an.id)["Cedar Plant"]
    assert any(u["code"] == "STALE_RELATIONSHIP" for u in c.uncertainty)
    assert all(e["stale"] for e in c.evidence)
    # proposed relationships never propagate
    p = node(client, "facilities", "Proposed Only")
    ok(client, "TRANSFERS_TO", w["f1"], p, status="PROPOSED", supply_share_pct=100.0)
    an2 = analyze(client, make_run(client, [sup_event(w["a"])]))
    assert "Proposed Only" not in findings(client, an2.id) and an2.excluded["proposed_not_propagated"] == 1


def test_effective_dates_are_applied_and_explained(client):
    a = node(client, "suppliers", "Alder Parts")
    cur, old, fut = (node(client, "facilities", n) for n in ("Current Plant", "Expired Plant", "Future Plant"))
    ok(client, "SUPPLIES", a, cur, supply_share_pct=100.0)
    ok(client, "SUPPLIES", a, old, supply_share_pct=100.0, effective_to="2020-12-31")
    ok(client, "SUPPLIES", a, fut, supply_share_pct=100.0, effective_from="2099-01-01")
    run = make_run(client, [sup_event(a)])
    an = analyze(client, run, cfg.ImpactConfig(as_of_date=date(2026, 6, 1)))
    f = findings(client, an.id)
    assert "Current Plant" in f and "Expired Plant" not in f and "Future Plant" not in f
    reasons = {e["reason"] for e in an.excluded["by_dates"]}
    assert reasons == {"EXPIRED", "NOT_YET_EFFECTIVE"} and an.excluded["by_dates_total"] == 2
    assert any(u["code"] == "DATES_UNSPECIFIED" for u in f["Current Plant"].uncertainty)
    an2 = analyze(client, run)  # dates not applied -> all three
    assert {"Expired Plant", "Future Plant"} <= set(findings(client, an2.id))


def test_criticality_bottleneck_candidates_and_directed_cut(client):
    w = realistic(client)
    an = analyze(client, make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])]))
    crit = {c.entity_name: c for c in rows_of(client, ImpactCriticality, an.id)}
    s2, f1, f2, f3 = crit["Supplier S2"], crit["Facility F1"], crit["Facility F2"], crit["Facility F3"]
    assert s2.role == "INITIATING" and s2.downstream_reachable == 5 and s2.products_reachable == 2
    # F1 is the only route to P2 and carries six enumerated paths: a structural candidate, with its reasons stated
    assert (
        f1.role == "INTERMEDIATE"
        and f1.paths_through == 6
        and f1.would_disconnect == 1
        and f1.would_disconnect_sample == ["PRODUCT:2"]
    )
    assert f1.bottleneck_candidate is True and len(f1.reasons) == 2
    # F3 feeds only one product: removing it disconnects it, but that is below the candidate threshold
    assert f3.would_disconnect == 1 and f3.bottleneck_candidate is False
    assert f2.would_disconnect == 0  # F3 and P1 stay reachable through F1
    # sole known confirmed source relationships are reported, not inferred
    assert s2.sole_known_source_of == ["FACILITY:2"] and f1.sole_known_source_of == ["PRODUCT:2"]
    # convergence: entities reached by more than one initiating entity
    assert "FACILITY:3" in f1.shared_upstream_of and "PRODUCT:1" in f1.shared_upstream_of
    # structural labels are not operational claims
    assert all("not an operational" in c.caveat for c in crit.values())


def test_alternatives_are_structural_only_and_labelled(client):
    w = realistic(client)
    an = analyze(
        client,
        make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])]),
        cfg.ImpactConfig(include_proposed_alternatives=True),
    )
    alts = rows_of(client, ImpactAlternative, an.id)
    by_src = {a.source_name: a for a in alts}
    assert (
        "Supplier S3" in by_src
        and by_src["Supplier S3"].for_name == "Facility F3"
        and by_src["Supplier S3"].path_state in ("ACTIVE", "UNCERTAIN", "STALE")
    )
    assert "Supplier S4" in by_src and by_src["Supplier S4"].path_state == "PROPOSED"
    assert all(a.feasibility == "STRUCTURAL_ONLY" for a in alts)
    assert any("Spare capacity" in m for m in by_src["Supplier S3"].missing) and any(
        "PROPOSED" in m for m in by_src["Supplier S4"].missing
    )
    no_prop = rows_of(
        client, ImpactAlternative, analyze(client, make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])])).id
    )
    assert "Supplier S4" not in {a.source_name for a in no_prop}


def test_edge_snapshot_survives_live_changes(client):
    w = linear(client)
    an = analyze(client, make_run(client, [sup_event(w["a"])]))
    snaps = {s.edge_ref: s for s in rows_of(client, ImpactEdgeSnapshot, an.id)}
    assert len(snaps) >= 3 and all(s.role == "PROPAGATION" for s in snaps.values() if s.record)
    first = next(iter(sorted(snaps)))
    assert snaps[first].record["supply_share_pct"] is not None
    # mutate the live graph; stored findings and snapshot are unchanged
    with Session(client.app.state.engine) as s, s.begin():
        from app.models.relationship import SupplyRelationship

        for r in s.scalars(select(SupplyRelationship)):
            r.supply_share_pct = 1.0
    again = findings(client, an.id)
    assert again["Elm Widget"].loss_pct == 100.0
    assert {s.edge_ref: s.record["supply_share_pct"] for s in rows_of(client, ImpactEdgeSnapshot, an.id)}[
        first
    ] == snaps[first].record["supply_share_pct"]


def test_changed_network_is_flagged(client):
    w = linear(client)
    run = make_run(client, [sup_event(w["a"])])
    ok(client, "TRANSFERS_TO", w["f1"], node(client, "facilities", "New Site"), supply_share_pct=100.0)
    an = analyze(client, run)
    assert an.graph_basis == "CHANGED_SINCE_RUN" and any(w_["code"] == "GRAPH_CHANGED_SINCE_RUN" for w_ in an.warnings)


def test_deterministic_for_identical_inputs(client):
    w = realistic(client)
    run = make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])])
    conf = cfg.ImpactConfig(reference_date=date(2026, 10, 27))

    def fingerprint(aid):
        import json

        fs = findings(client, aid)
        rows = [(n, f.classification, f.min_depth, f.loss_pct, f.interaction, f.path_count, json.dumps(f.paths, sort_keys=True), json.dumps(f.dimensions, sort_keys=True, default=str), f.confidence, f.coverage) for n, f in sorted(fs.items())]  # fmt: skip
        crit = [
            (c.entity_name, c.downstream_reachable, c.would_disconnect, c.bottleneck_candidate)
            for c in sorted(rows_of(client, ImpactCriticality, aid), key=lambda c: c.entity_name)
        ]
        alts = [
            (a.for_name, a.source_name, a.path_state)
            for a in sorted(rows_of(client, ImpactAlternative, aid), key=lambda a: (a.for_name, a.source_name))
        ]
        return rows, crit, alts

    a1, a2 = analyze(client, run, conf), analyze(client, run, conf)
    assert a1.id != a2.id and a1.graph_hash == a2.graph_hash and a1.config_hash == a2.config_hash
    assert fingerprint(a1.id) == fingerprint(a2.id)
    assert a1.summary == a2.summary


def test_pure_path_enumeration_on_hand_built_graph():
    from app.graph.rules import EdgeKind, GraphRelType
    from app.graph.store import EdgeRec
    from app.models.enums import EdgeStatus

    S, F = RegistryType.SUPPLIER, RegistryType.FACILITY

    def e(i, s, d):
        return EdgeRec(ref=f"SUPPLY:{i}", kind=EdgeKind.SUPPLY, row_id=i, type=GraphRelType.TRANSFERS_TO, source=s, destination=d, status=EdgeStatus.CONFIRMED, code=f"E{i}", provenance="SYNTHETIC_DEMO")  # fmt: skip

    a, b, c, d = (S, 1), (F, 2), (F, 3), (F, 4)
    edges = [e(1, a, b), e(2, a, c), e(3, b, d), e(4, c, d), e(5, d, b)]  # diamond plus a back edge
    idx = enumerate_paths({"EV": edges}, {"EV": [a]}, {}, max_length=4, max_store_per_event=5, max_steps=1000)
    assert (
        idx.count[d] == 2
        and idx.complete
        and idx.count[b] == 2
        and idx.count[c] == 1
        and idx.through[b] == 1
        and idx.through[c] == 2
    )
    capped = enumerate_paths({"EV": edges}, {"EV": [a]}, {}, max_length=4, max_store_per_event=5, max_steps=1)
    assert capped.complete is False
    short = enumerate_paths({"EV": edges}, {"EV": [a]}, {}, max_length=1, max_store_per_event=5, max_steps=1000)
    assert short.count[d] == 0 and short.count[b] == 1


def test_no_canonical_data_is_changed(client):
    from app.models.facility import Facility
    from app.models.relationship import SupplyRelationship
    from app.models.supplier import Supplier

    w = realistic(client)
    run = make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])])

    def snap():
        with Session(client.app.state.engine) as s:
            out = {}
            for m in (Supplier, Facility, SupplyRelationship):
                out[m.__name__] = [
                    (r.id, r.version if hasattr(r, "version") else None, r.updated_at)
                    for r in s.scalars(select(m).order_by(m.id))
                ]
            return out

    before = snap()
    analyze(client, run)
    assert snap() == before


def test_unsupported_semantics_rows_exist_for_every_finding(client):
    w = realistic(client)
    an = analyze(client, make_run(client, [sup_event(w["S1"])]))
    for f in findings(client, an.id).values():
        assert f.explanation and f.assumptions and set(f.dimensions) >= {"supply_shortfall", "capacity_shortfall", "delivery_delay", "disruption_duration", "dependency_concentration", "shared_upstream", "evidence_completeness"}  # fmt: skip
        assert f.data_quality["note"]
    with Session(client.app.state.engine) as s:
        assert s.scalar(
            select(func.count()).select_from(ImpactFinding).where(ImpactFinding.analysis_id == an.id)
        ) == len(findings(client, an.id))
