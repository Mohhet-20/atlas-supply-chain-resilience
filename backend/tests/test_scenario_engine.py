"""Pure-engine tests: direct and indirect impact, quantification, cycles, paths, determinism and bounds."""

from __future__ import annotations

from app.scenarios.engine import EngineLimits
from tests.graph_helpers import node, ok
from tests.scenario_helpers import by_name, chain, ev, simulate, sup_out


def test_supplier_outage_direct_and_downstream(client):
    w = chain(client)
    r = simulate(client, [sup_out(w["a"])])
    a = by_name(r, "Alder Parts")
    assert a["impact_kind"] == "DIRECT" and a["loss_pct"] == 100.0 and a["exposure_level"] == "DIRECT"
    f1 = by_name(r, "Cedar Plant")
    assert f1["impact_kind"] == "DOWNSTREAM" and f1["loss_pct"] == 60.0 and f1["quantification"] == "QUANTIFIED"
    assert f1["exposure_level"] == "HIGH" and f1["depth"] == 1
    f2 = by_name(r, "Dogwood Hub")
    assert f2["loss_pct"] == 60.0 and f2["depth"] == 2  # 100% of inflow from F1, F1 lost 60%
    p1 = by_name(r, "Elm Widget")
    assert p1["loss_pct"] == 60.0 and p1["depth"] == 3
    assert [s["ref"] for s in p1["primary_path"]] == ["SUPPLY:1", "SUPPLY:3", "PRODUCT:1"]
    assert "Birch Parts" not in {i["entity_name"] for i in r.impacts}  # a sibling supplier is not affected
    assert r.summary["direct"] == 1 and r.summary["indirect"] == 3


def test_partial_capacity_reduction(client):
    w = chain(client)
    r = simulate(
        client, [ev("SUPPLIER_CAPACITY_REDUCTION", target_type="SUPPLIER", target_id=w["a"]["id"], magnitude_pct=50)]
    )
    assert by_name(r, "Alder Parts")["loss_pct"] == 50.0
    assert by_name(r, "Cedar Plant")["loss_pct"] == 30.0  # 60% share x 50%
    assert by_name(r, "Elm Widget")["loss_pct"] == 30.0
    assert r.summary["capacity_at_risk_by_unit"] == {"units/week": 50.0}  # edge capacity 100 x 50%


def test_facility_shutdown_with_upstream_exploration(client):
    w = chain(client)
    r = simulate(client, [ev("FACILITY_SHUTDOWN", target_type="FACILITY", target_id=w["f1"]["id"])])
    f1 = by_name(r, "Cedar Plant")
    assert (
        f1["impact_kind"] == "DIRECT" and f1["capacity_lost_value"] is None
    )  # facility capacity unknown stays unknown
    assert by_name(r, "Dogwood Hub")["loss_pct"] == 100.0
    up = {i["entity_name"]: i for i in r.impacts if i["impact_kind"] == "UPSTREAM"}
    assert set(up) == {"Alder Parts", "Birch Parts"}
    assert all(u["quantification"] == "UNQUANTIFIED" and u["exposure_level"] == "QUALITATIVE" for u in up.values())
    off = simulate(
        client, [ev("FACILITY_SHUTDOWN", target_type="FACILITY", target_id=w["f1"]["id"])], include_upstream=False
    )
    assert not [i for i in off.impacts if i["impact_kind"] == "UPSTREAM"]


def test_unknown_share_is_never_zero_or_hundred(client):
    w = chain(client, shares=(None, 100.0))
    r = simulate(client, [sup_out(w["a"])])
    f1 = by_name(r, "Cedar Plant")
    assert f1["loss_pct"] is None and f1["quantification"] == "UNQUANTIFIED" and f1["loss_pct_lower_bound"] is None
    assert f1["uncertainty"] and f1["exposure_level"] == "UNQUANTIFIED"
    assert by_name(r, "Dogwood Hub")["loss_pct"] is None
    assert any(m["code"] == "SUPPLY_SHARE_UNKNOWN" for m in r.missing_data)


def test_lower_bound_when_only_some_shares_known(client):
    a = node(client, "suppliers", "Alder Parts")
    b = node(client, "suppliers", "Birch Parts")
    f = node(client, "facilities", "Cedar Plant")
    ok(client, "SUPPLIES", a, f, supply_share_pct=40.0)
    ok(client, "SUPPLIES", b, f)  # unknown share
    r = simulate(client, [sup_out(a), sup_out(b, "EV-2")])
    c = by_name(r, "Cedar Plant")
    assert c["quantification"] == "LOWER_BOUND" and c["loss_pct"] is None and c["loss_pct_lower_bound"] == 40.0
    assert c["path_count"] == 2 and sorted(c["event_uids"]) == ["EV-1", "EV-2"]


def test_multiple_paths_counted_once(client):
    a = node(client, "suppliers", "Alder Parts")
    b = node(client, "suppliers", "Birch Parts")
    f = node(client, "facilities", "Cedar Plant")
    ok(client, "SUPPLIES", a, f, supply_share_pct=30.0)
    ok(client, "SUPPLIES", b, f, supply_share_pct=50.0)
    r = simulate(client, [sup_out(a), sup_out(b, "EV-2")])
    assert [i["entity_name"] for i in r.impacts].count("Cedar Plant") == 1
    assert by_name(r, "Cedar Plant")["loss_pct"] == 80.0
    assert r.summary["entities_affected"] == 3


def test_shares_over_100_are_capped_and_noted(client):
    a = node(client, "suppliers", "Alder Parts")
    b = node(client, "suppliers", "Birch Parts")
    f = node(client, "facilities", "Cedar Plant")
    ok(client, "SUPPLIES", a, f, supply_share_pct=70.0)
    ok(client, "SUPPLIES", b, f, supply_share_pct=70.0)
    c = by_name(simulate(client, [sup_out(a), sup_out(b, "EV-2")]), "Cedar Plant")
    assert c["loss_pct"] == 100.0 and any("more than 100" in u for u in c["uncertainty"])


def test_overlapping_events_on_one_target_do_not_stack(client):
    w = chain(client)
    e1 = ev("SUPPLIER_CAPACITY_REDUCTION", "EV-1", target_type="SUPPLIER", target_id=w["a"]["id"], magnitude_pct=30)
    e2 = ev("SUPPLIER_CAPACITY_REDUCTION", "EV-2", target_type="SUPPLIER", target_id=w["a"]["id"], magnitude_pct=40)
    r = simulate(client, [e1, e2])
    assert by_name(r, "Alder Parts")["loss_pct"] == 40.0  # the larger, not 70


def test_cycle_terminates_and_counts_each_entity_once(client):
    a = node(client, "suppliers", "Alder Parts")
    f1 = node(client, "facilities", "Cedar Plant")
    f2 = node(client, "facilities", "Dogwood Hub")
    ok(client, "SUPPLIES", a, f1, supply_share_pct=100.0)
    ok(client, "TRANSFERS_TO", f1, f2, supply_share_pct=100.0)
    ok(client, "TRANSFERS_TO", f2, f1, supply_share_pct=50.0)
    r = simulate(client, [sup_out(a)], downstream_depth=6)
    names = [i["entity_name"] for i in r.impacts]
    assert sorted(names) == ["Alder Parts", "Cedar Plant", "Dogwood Hub"]
    assert by_name(r, "Cedar Plant")["loss_pct"] == 100.0 and by_name(r, "Dogwood Hub")["loss_pct"] == 100.0


def test_disconnected_component_unaffected(client):
    w = chain(client)
    lone = node(client, "suppliers", "Lonely Supplier")
    lf = node(client, "facilities", "Lonely Site")
    ok(client, "SUPPLIES", lone, lf, supply_share_pct=100.0)
    r = simulate(client, [sup_out(w["a"])])
    assert {"Lonely Supplier", "Lonely Site"}.isdisjoint({i["entity_name"] for i in r.impacts})


def test_isolated_target_reports_only_direct_impact(client):
    lone = node(client, "suppliers", "Lonely Supplier")
    r = simulate(client, [sup_out(lone)])
    assert [i["impact_kind"] for i in r.impacts] == ["DIRECT"]
    assert any(w["code"] == "NO_RELATIONSHIPS_FOUND" for w in r.warnings)


def test_relationship_unavailable_event(client):
    w = chain(client)
    edge = ok(client, "SUPPLIES", node(client, "suppliers", "Fir Parts"), w["f2"], supply_share_pct=25.0)
    ref = edge["ref"]
    r = simulate(client, [ev("RELATIONSHIP_UNAVAILABLE", relationship_ref=ref)])
    f2 = by_name(r, "Dogwood Hub")
    assert f2["impact_kind"] == "DOWNSTREAM" and f2["loss_pct"] == 25.0 and f2["depth"] == 1
    assert by_name(r, "Elm Widget")["loss_pct"] == 25.0
    assert by_name(r, "Fir Parts")["impact_kind"] == "UPSTREAM"
    assert [e for e in r.edge_impacts if e["edge_ref"] == ref][0]["effect"] == "UNAVAILABLE"


def test_lead_time_increase_passes_downstream_additively(client):
    w = chain(client)
    r = simulate(
        client, [ev("LEAD_TIME_INCREASE", target_type="SUPPLIER", target_id=w["a"]["id"], added_lead_time_days=10)]
    )
    f1 = by_name(r, "Cedar Plant")
    assert f1["added_lead_time_days"] == 10 and f1["exposure_level"] == "DELAY" and f1["loss_pct"] is None
    assert by_name(r, "Elm Widget")["added_lead_time_days"] == 10
    assert all(i["impact_kind"] != "DIRECT" for i in r.impacts)


def test_stores_edges_are_qualitative(client):
    f = node(client, "facilities", "Cedar Plant")
    p = node(client, "products", "Elm Widget")
    a = node(client, "suppliers", "Alder Parts")
    ok(client, "SUPPLIES", a, f, supply_share_pct=100.0)
    ok(client, "STORES", f, p, supply_share_pct=100.0)
    pr = by_name(simulate(client, [sup_out(a)]), "Elm Widget")
    assert pr["loss_pct"] is None and pr["exposure_level"] == "QUALITATIVE"


def test_depth_limit_flagged_and_enforced(client):
    w = chain(client)
    r = simulate(client, [sup_out(w["a"])], downstream_depth=1)
    assert {i["entity_name"] for i in r.impacts} == {"Alder Parts", "Cedar Plant"}
    assert "depth_limit" in r.truncation_reasons and r.truncated
    capped = simulate(client, [sup_out(w["a"])], EngineLimits(max_depth=2), downstream_depth=5)
    assert max(i["depth"] for i in capped.impacts) == 2 and "depth_cap" in capped.truncation_reasons


def test_node_limit_marks_truncated(client):
    a = node(client, "suppliers", "Alder Parts")
    for n in range(6):
        ok(client, "SUPPLIES", a, node(client, "facilities", f"Site {n}"), supply_share_pct=10.0)
    r = simulate(client, [sup_out(a)], EngineLimits(max_nodes=4))
    assert r.truncated and "node_limit" in r.truncation_reasons and len(r.impacts) <= 4


def test_reproducible_for_identical_inputs(client):
    w = chain(client)
    r1, r2 = simulate(client, [sup_out(w["a"])]), simulate(client, [sup_out(w["a"])])
    assert r1.impacts == r2.impacts and r1.edge_impacts == r2.edge_impacts and r1.alternatives == r2.alternatives
    assert r1.input_hash == r2.input_hash and r1.summary == r2.summary
    ok(client, "SUPPLIES", node(client, "suppliers", "Gum Parts"), w["f1"], supply_share_pct=5.0)
    assert simulate(client, [sup_out(w["a"])]).input_hash != r1.input_hash  # changed inputs change the hash


def test_alternatives_are_listed_with_gaps_and_labelled(client):
    w = chain(client)
    r = simulate(client, [sup_out(w["a"])])
    alts = [x for x in r.alternatives if x["for_id"] == w["f1"]["id"] and x["for_type"] == "FACILITY"]
    assert [x["source_name"] for x in alts] == ["Birch Parts"]
    assert alts[0]["edge_status"] == "CONFIRMED" and alts[0]["supply_share_pct"] == 40.0
    assert any("spare capacity is never inferred" in m for m in alts[0]["missing"])
    assert "analyst review" in alts[0]["caveat"]
    assert all(x["source_name"] != "Alder Parts" for x in r.alternatives)


def test_proposed_alternative_is_labelled_and_can_be_excluded(client):
    w = chain(client)
    cand = node(client, "suppliers", "Hazel Parts")
    ok(client, "SUPPLIES", cand, w["f1"], status="PROPOSED")
    r = simulate(client, [sup_out(w["a"])])
    hz = [x for x in r.alternatives if x["source_name"] == "Hazel Parts"][0]
    assert hz["edge_status"] == "PROPOSED" and any("PROPOSED" in m for m in hz["missing"])
    r2 = simulate(client, [sup_out(w["a"])], include_proposed_alternatives=False)
    assert all(x["source_name"] != "Hazel Parts" for x in r2.alternatives)
    assert all(i["entity_name"] != "Hazel Parts" for i in r.impacts)  # proposed links never propagate impact


def test_facility_capacity_loss_from_known_capacity(client):
    from tests.registry_helpers import mk

    f = mk(
        client,
        "facilities",
        name="Cap Plant",
        facility_type="MANUFACTURING",
        capacity_value=1000,
        capacity_unit="units/day",
    )
    r = simulate(
        client, [ev("FACILITY_CAPACITY_REDUCTION", target_type="FACILITY", target_id=f["id"], magnitude_pct=25)]
    )
    i = by_name(r, "Cap Plant")
    assert i["capacity_lost_value"] == 250.0 and i["capacity_unit"] == "units/day"
    assert r.summary["facility_capacity_lost_by_unit"] == {"units/day": 250.0}


def test_region_event_over_selected_entities(client):
    a = node(client, "facilities", "Site A")
    b = node(client, "facilities", "Site B")
    e = ev(
        "REGION_DISRUPTION",
        region_label="Testland",
        effect="REDUCTION",
        magnitude_pct=40,
        targets=[{"type": "FACILITY", "id": a["id"]}, {"type": "FACILITY", "id": b["id"]}],
    )
    r = simulate(client, [e])
    assert {i["entity_name"] for i in r.impacts} == {"Site A", "Site B"}
    assert all(i["loss_pct"] == 40.0 for i in r.impacts)
