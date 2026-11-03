from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.graph_helpers import G, node, ok, q, refs, world

PAST = "2020-01-01"


def ids(graph, kind):
    return {n["id"] for n in graph["nodes"] if n["type"] == kind}


def test_direct_neighbors_split_upstream_and_downstream(client):
    w = world(client)
    g = client.get(f"{G}/nodes/FACILITY/{w['f1']['id']}/neighbors").json()
    assert ids(g, "SUPPLIER") == {w["s1"]["id"], w["s2"]["id"]}
    assert ids(g, "FACILITY") == {w["f1"]["id"], w["f2"]["id"]}
    assert ids(g, "PRODUCT") == {w["p1"]["id"]}
    assert g["limits"]["upstream_count"] == 2 and g["limits"]["downstream_count"] == 2 and g["limits"]["counts_exact"]
    assert all(n["depth"] in (0, 1) for n in g["nodes"]) and g["truncated"] is False
    down = client.get(f"{G}/nodes/FACILITY/{w['f1']['id']}/neighbors", params={"direction": "downstream"}).json()
    assert {e["type"] for e in down["edges"]} == {"TRANSFERS_TO", "PRODUCES"}
    up = client.get(f"{G}/nodes/FACILITY/{w['f1']['id']}/neighbors", params={"direction": "upstream"}).json()
    assert {e["type"] for e in up["edges"]} == {"SUPPLIES"}


def test_downstream_and_upstream_follow_flow_direction_by_depth(client):
    w = world(client)
    d1 = client.get(f"{G}/nodes/SUPPLIER/{w['s1']['id']}/downstream", params={"depth": 1}).json()
    assert refs(d1) == {("SUPPLIER", w["s1"]["id"]), ("FACILITY", w["f1"]["id"]), ("PRODUCT", w["p2"]["id"])}
    d3 = client.get(f"{G}/nodes/SUPPLIER/{w['s1']['id']}/downstream", params={"depth": 3}).json()
    assert ids(d3, "FACILITY") == {w[k]["id"] for k in ("f1", "f2", "f3")} and ids(d3, "PRODUCT") == {
        w["p1"]["id"],
        w["p2"]["id"],
    }
    assert ids(d3, "SUPPLIER") == {w["s1"]["id"]}  # never walks back up to s2
    up = client.get(f"{G}/nodes/PRODUCT/{w['p1']['id']}/upstream", params={"depth": 2}).json()
    assert ids(up, "FACILITY") == {w[k]["id"] for k in ("f1", "f3", "f2")}
    assert ids(up, "SUPPLIER") == {w["s1"]["id"], w["s2"]["id"]}
    assert w["p2"]["id"] not in ids(up, "PRODUCT") - {w["p1"]["id"]}
    assert up["max_depth_reached"] == 3 or up["max_depth_reached"] == 2


def test_expand_depth_metadata_and_depth_cap(client):
    w = world(client)
    g = client.get(f"{G}/expand", params=q(w["s1"]) | {"direction": "downstream", "depth": 1}).json()
    assert g["requested_depth"] == 1 and g["max_depth_reached"] == 1 and g["nodes_at_depth_limit"] == 2
    assert client.get(f"{G}/expand", params=q(w["s1"]) | {"depth": 7}).status_code == 422  # hard cap is 6
    assert client.get(f"{G}/expand", params=q(w["s1"]) | {"depth": 0}).status_code == 422
    assert client.get(f"{G}/expand", params=q(w["s1"]) | {"max_nodes": 501}).status_code == 422
    assert client.get(f"{G}/expand", params=q(w["s1"]) | {"max_edges": 1001}).status_code == 422


def test_node_limit_truncates_and_returns_consistent_subgraph(client):
    w = world(client)
    g = client.get(f"{G}/expand", params=q(w["s1"]) | {"direction": "downstream", "depth": 4, "max_nodes": 3}).json()
    assert g["truncated"] is True and "node_limit" in g["truncation_reasons"]
    assert g["nodes_returned"] == 3 == len(g["nodes"])
    known = refs(g)
    assert all(
        (e["source_type"], e["source_id"]) in known and (e["destination_type"], e["destination_id"]) in known
        for e in g["edges"]
    )


def test_edge_limit_truncates(client):
    w = world(client)
    g = client.get(f"{G}/expand", params=q(w["f1"]) | {"depth": 3, "max_edges": 2}).json()
    assert g["truncated"] is True and "edge_limit" in g["truncation_reasons"] and g["edges_returned"] <= 2


def test_high_degree_hub_is_bounded(client):
    hub = node(client, "suppliers", "Mega Hub")
    for i in range(30):
        ok(client, "SUPPLIES", hub, node(client, "facilities", f"Spoke {i:02d}"))
    g = client.get(f"{G}/expand", params=q(hub) | {"direction": "downstream", "max_edges": 10, "max_nodes": 200}).json()
    assert g["truncated"] is True and g["edges_returned"] == 10 and g["nodes_returned"] == 11
    full = client.get(f"{G}/expand", params=q(hub) | {"direction": "downstream"}).json()
    assert full["edges_returned"] == 30 and full["truncated"] is False


def test_cycles_terminate_and_are_not_repeated(client):
    w = world(client)
    ok(client, "TRANSFERS_TO", w["f3"], w["f1"])  # f1 -> f2 -> f3 -> f1
    g = client.get(f"{G}/expand", params=q(w["f1"]) | {"direction": "downstream", "depth": 6}).json()
    node_ids = [(n["type"], n["id"]) for n in g["nodes"]]
    assert len(node_ids) == len(set(node_ids))
    assert {e["ref"] for e in g["edges"]} >= {e["ref"] for e in g["edges"] if e["type"] == "TRANSFERS_TO"}
    sum_cycle = [e for e in g["edges"] if e["type"] == "TRANSFERS_TO"]
    assert len(sum_cycle) == 3 and g["truncated"] is False
    p = client.get(f"{G}/paths", params={"from_type": "FACILITY", "from_id": w["f1"]["id"], "to_type": "FACILITY",
                                          "to_id": w["f1"]["id"]}).json()  # fmt: skip
    assert p["found"] is True and p["shortest_length"] == 0
    p2 = client.get(f"{G}/paths", params={"from_type": "FACILITY", "from_id": w["f2"]["id"], "to_type": "PRODUCT",
                                           "to_id": w["p1"]["id"], "max_paths": 10, "max_length": 8}).json()  # fmt: skip
    for path in p2["paths"]:
        seq = [(n["type"], n["id"]) for n in path["nodes"]]
        assert len(seq) == len(set(seq))  # simple paths only


def test_path_search_returns_ordered_nodes_edges_and_provenance(client):
    w = world(client)
    r = client.get(f"{G}/paths", params={"from_type": "SUPPLIER", "from_id": w["s1"]["id"], "to_type": "PRODUCT",
                                          "to_id": w["p1"]["id"], "max_paths": 5}).json()  # fmt: skip
    assert r["found"] and r["shortest_length"] == 2 and r["mode"] == "directed"
    lens = [p["length"] for p in r["paths"]]
    assert lens == sorted(lens) and lens[:2] == [2, 4]
    best = r["paths"][0]
    assert [n["id"] for n in best["nodes"]] == [w["s1"]["id"], w["f1"]["id"], w["p1"]["id"]]
    assert best["relationship_types"] == ["SUPPLIES", "PRODUCES"] and best["provenance"] == ["USER_PROVIDED"]
    assert [e["hop"] for e in best["edges"]] == [1, 2] and best["statuses"] == ["CONFIRMED"]
    assert "no path is inferred" in r["note"]


def test_directed_path_respects_flow_but_undirected_does_not(client):
    w = world(client)
    args = {"from_type": "PRODUCT", "from_id": w["p1"]["id"], "to_type": "SUPPLIER", "to_id": w["s1"]["id"]}
    back = client.get(f"{G}/paths", params=args).json()
    assert back["found"] is False and "does not prove" in back["note"]
    both = client.get(f"{G}/paths", params=args | {"mode": "undirected"}).json()
    assert both["found"] and both["shortest_length"] == 2


def test_path_length_and_path_count_limits(client):
    w = world(client)
    args = {"from_type": "SUPPLIER", "from_id": w["s1"]["id"], "to_type": "PRODUCT", "to_id": w["p1"]["id"]}
    short = client.get(f"{G}/paths", params=args | {"max_length": 3, "max_paths": 5}).json()
    assert [p["length"] for p in short["paths"]] == [2]
    one = client.get(f"{G}/paths", params=args | {"max_paths": 1}).json()
    assert len(one["paths"]) == 1
    assert client.get(f"{G}/paths", params=args | {"max_length": 9}).status_code == 422
    assert client.get(f"{G}/paths", params=args | {"max_paths": 11}).status_code == 422


def test_disconnected_and_empty_networks(client):
    empty = client.get(f"{G}/summary").json()
    assert empty["relationships"]["total"] == 0 and any("No relationships" in n for n in empty["notes"])
    w = world(client)
    iso = client.get(f"{G}/nodes/SUPPLIER/{w['s3']['id']}/neighbors").json()
    assert iso["nodes_returned"] == 1 and iso["edges_returned"] == 0 and iso["truncated"] is False
    p = client.get(f"{G}/paths", params={"from_type": "SUPPLIER", "from_id": w["s3"]["id"], "to_type": "PRODUCT",
                                          "to_id": w["p1"]["id"]}).json()  # fmt: skip
    assert p["found"] is False and p["paths"] == []


def test_status_filter_excludes_proposed_by_default(client):
    w = world(client)
    ok(client, "SUPPLIES", w["s3"], w["f4"], status="PROPOSED")
    default = client.get(f"{G}/expand", params=q(w["s3"])).json()
    assert default["edges_returned"] == 0
    both = client.get(f"{G}/expand", params=q(w["s3"]) | {"statuses": ["PROPOSED", "CONFIRMED"]}).json()
    assert both["edges_returned"] == 1 and both["edges"][0]["status"] == "PROPOSED"
    only = client.get(f"{G}/relationships", params={"statuses": "PROPOSED"}).json()
    assert only["total"] == 1


def test_inactive_and_rejected_relationships_leave_the_active_network(client):
    w = world(client)
    e = client.get(f"{G}/relationships", params={"node_type": "SUPPLIER", "node_id": w["s2"]["id"]}).json()["items"][0]
    r = client.post(
        f"{G}/relationships/{e['ref']}/status",
        json={"status": "INACTIVE", "reason": "ended", "expected_version": e["version"]},
    )
    assert r.status_code == 200
    up = client.get(f"{G}/nodes/FACILITY/{w['f1']['id']}/upstream").json()
    assert ids(up, "SUPPLIER") == {w["s1"]["id"]}
    shown = client.get(
        f"{G}/nodes/FACILITY/{w['f1']['id']}/upstream", params={"statuses": ["CONFIRMED", "INACTIVE"]}
    ).json()
    assert ids(shown, "SUPPLIER") == {w["s1"]["id"], w["s2"]["id"]}


def test_effective_dates_filter_edges(client):
    w = world(client)
    ok(client, "SUPPLIES", w["s3"], w["f4"], effective_from="2019-01-01", effective_to=PAST)
    ok(client, "SUPPLIES", w["s3"], w["f3"], effective_from="2999-01-01")
    one = q(w["s3"]) | {"depth": 1}
    today = client.get(f"{G}/expand", params=one).json()
    assert today["edges_returned"] == 0
    old = client.get(f"{G}/expand", params=one | {"as_of": "2019-06-01"}).json()
    assert ids(old, "FACILITY") == {w["f4"]["id"]}
    future = client.get(f"{G}/expand", params=one | {"as_of": "3000-01-01"}).json()
    assert ids(future, "FACILITY") == {w["f3"]["id"]}
    everything = client.get(f"{G}/expand", params=one | {"ignore_dates": True}).json()
    assert everything["edges_returned"] == 2


def test_type_and_node_type_filters_combine(client):
    w = world(client)
    only_tx = client.get(f"{G}/expand", params=q(w["f1"]) | {"types": "TRANSFERS_TO", "depth": 3}).json()
    assert {e["type"] for e in only_tx["edges"]} == {"TRANSFERS_TO"} and ids(only_tx, "FACILITY") == {
        w[k]["id"] for k in ("f1", "f2", "f3")
    }
    no_products = client.get(f"{G}/expand", params=q(w["s1"]) | {"direction": "downstream", "depth": 3,
                                                              "node_types": ["SUPPLIER", "FACILITY"]}).json()  # fmt: skip
    assert ids(no_products, "PRODUCT") == set()
    combo = client.get(f"{G}/expand", params=q(w["f1"]) | {"types": ["SUPPLIES", "PRODUCES"], "statuses": "CONFIRMED",
                                                         "direction": "both"}).json()  # fmt: skip
    assert {e["type"] for e in combo["edges"]} == {"SUPPLIES", "PRODUCES"}
    assert client.get(f"{G}/expand", params=q(w["f1"]) | {"types": "BOGUS"}).status_code == 422


def test_relationship_list_pagination_and_node_filter(client):
    w = world(client)
    p1 = client.get(f"{G}/relationships", params={"limit": 3, "offset": 0}).json()
    p2 = client.get(f"{G}/relationships", params={"limit": 3, "offset": 3}).json()
    p3 = client.get(f"{G}/relationships", params={"limit": 3, "offset": 6}).json()
    refs_seen = [e["ref"] for e in p1["items"] + p2["items"] + p3["items"]]
    assert p1["total"] == 7 and len(refs_seen) == 7 and len(set(refs_seen)) == 7
    inc = client.get(f"{G}/relationships", params={"node_type": "FACILITY", "node_id": w["f1"]["id"]}).json()
    assert inc["total"] == 4
    assert client.get(f"{G}/relationships", params={"node_type": "FACILITY"}).status_code == 422
    assert client.get(f"{G}/relationships", params={"limit": 101}).status_code == 422


def test_unknown_start_node_is_404(client):
    assert client.get(f"{G}/expand", params={"start_type": "SUPPLIER", "start_id": 12345}).status_code == 404
    assert client.get(f"{G}/nodes/BOGUS/1/neighbors").status_code == 422


def test_queries_use_a_bounded_number_of_sql_statements(client, engine):
    """Expansion issues a batch of queries per level, not one per node (no N+1)."""
    from sqlalchemy import event

    hub = node(client, "suppliers", "Mega Hub")
    for i in range(40):
        f = node(client, "facilities", f"Site {i:02d}")
        ok(client, "SUPPLIES", hub, f)
    count = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(*a, **k):  # noqa: ARG001
        count["n"] += 1

    # the API uses its own engine from the app; count on that one
    app_engine = client.app.state.session_factory.kw["bind"]
    event.listen(app_engine, "before_cursor_execute", _count)
    try:
        r = client.get(f"{G}/expand", params=q(hub) | {"direction": "downstream", "depth": 2})
    finally:
        event.remove(app_engine, "before_cursor_execute", _count)
    assert r.status_code == 200 and r.json()["nodes_returned"] == 41
    assert count["n"] < 25, count["n"]  # 40 facilities, yet a fixed handful of statements


def test_unused_imports_guard():
    assert Session and text and pytest  # keeps these imports referenced
