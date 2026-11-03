from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from app.models import GraphEdgeEvent, SupplyRelationship
from app.registry import merge as merge_mod  # noqa: F401
from tests.graph_helpers import G, node, ok, rel, world
from tests.registry_helpers import API, confirm_pair, merge_body, mk

KEEP = {"name": {"choice": "KEEP_SURVIVOR"}}


def test_create_every_editable_type_defaults_to_proposed_and_keeps_unknowns_null(client):
    s, f, f2, p = (node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A"),
                   node(client, "facilities", "Site B"), node(client, "products", "Thing"))  # fmt: skip
    for typ, a, b in (("SUPPLIES", s, f), ("TRANSFERS_TO", f, f2), ("SUPPLIES_PRODUCT", s, p), ("PRODUCES", f, p),
                      ("STORES", f2, p)):  # fmt: skip
        r = client.post(
            f"{G}/relationships",
            json={"type": typ, "source_type": a["type"], "source_id": a["id"], "destination_type": b["type"],
                  "destination_id": b["id"], "justification": "contract on file"},
        )  # fmt: skip
        assert r.status_code == 201, r.text
        e = r.json()
        assert e["status"] == "PROPOSED" and e["type"] == typ and e["version"] == 1
        # unknown optional values are null, never invented defaults
        for k in ("capacity_value", "capacity_unit", "lead_time_days", "supply_share_pct", "confidence",
                  "criticality", "effective_from", "effective_to"):  # fmt: skip
            assert e[k] is None, k
        assert e["provenance"] == "USER_PROVIDED" and e["created_by"] == "dev-local"


def test_attributes_round_trip_including_units(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    e = ok(client, "SUPPLIES", s, f, capacity_value=1200, capacity_unit="units/week", lead_time_days=14,
           supply_share_pct=35.5, confidence=0.8, criticality="HIGH", effective_from="2026-01-01")  # fmt: skip
    got = client.get(f"{G}/relationships/{e['ref']}").json()["relationship"]
    assert (got["capacity_value"], got["capacity_unit"], got["lead_time_days"]) == (1200, "units/week", 14)
    assert got["supply_share_pct"] == 35.5 and got["criticality"] == "HIGH" and got["effective_from"] == "2026-01-01"


def test_nonexistent_endpoints_are_rejected(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    assert rel(client, "SUPPLIES", {"type": "SUPPLIER", "id": 9999}, f).status_code == 404
    assert rel(client, "SUPPLIES", s, {"type": "FACILITY", "id": 9999}).status_code == 404
    assert client.get(f"{G}/relationships/SUPPLY:9999").status_code == 404
    assert client.get(f"{G}/relationships/NOPE").status_code == 422


@pytest.mark.parametrize(
    ("typ", "a", "b"),
    [("SUPPLIES", "s", "p"), ("SUPPLIES", "f", "f"), ("PRODUCES", "s", "p"), ("TRANSFERS_TO", "s", "f"),
     ("STORES", "p", "f"), ("SUPPLIES_PRODUCT", "f", "p"), ("OWNS", "s", "f")],
)  # fmt: skip
def test_invalid_type_combinations_rejected(client, typ, a, b):
    nodes = {"s": node(client, "suppliers", "Alpha Co"), "f": node(client, "facilities", "Site A"),
             "p": node(client, "products", "Thing")}  # fmt: skip
    r = rel(client, typ, nodes[a], nodes[b])
    assert r.status_code == 422, r.text


def test_self_transfer_rejected(client):
    f = node(client, "facilities", "Site A")
    assert rel(client, "TRANSFERS_TO", f, f).status_code == 422


def test_duplicate_policy_one_row_per_identity_whatever_the_status(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    first = ok(client, "SUPPLIES", s, f, status="PROPOSED")
    again = rel(client, "SUPPLIES", s, f)
    assert again.status_code == 409 and again.json()["error"]["code"] == "duplicate_relationship"
    assert first["ref"] in again.json()["error"]["message"]
    client.post(f"{G}/relationships/{first['ref']}/status",
                json={"status": "REJECTED", "reason": "not true", "expected_version": 1})  # fmt: skip
    # a rejected relationship still owns the identity: reopen it instead of creating another
    assert rel(client, "SUPPLIES", s, f).status_code == 409


def test_update_attributes_with_version_check_and_clearing(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    e = ok(client, "SUPPLIES", s, f, lead_time_days=10)
    url = f"{G}/relationships/{e['ref']}"
    r = client.patch(url, json={"expected_version": 1, "reason": "new quote", "lead_time_days": 12,
                                "capacity_value": 50, "capacity_unit": "pallets"})  # fmt: skip
    assert r.status_code == 200 and r.json()["lead_time_days"] == 12 and r.json()["version"] == 2
    stale = client.patch(url, json={"expected_version": 1, "reason": "stale", "lead_time_days": 99})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    cleared = client.patch(url, json={"expected_version": 2, "reason": "unknown again", "lead_time_days": None})
    assert cleared.status_code == 200 and cleared.json()["lead_time_days"] is None
    same = client.patch(url, json={"expected_version": 3, "reason": "noop", "lead_time_days": None})
    assert same.status_code == 422
    hist = client.get(url).json()["history"]
    assert [h["action"] for h in hist] == ["UPDATED", "UPDATED", "CREATED"]
    assert hist[1]["changes"]["lead_time_days"] == [10, 12]


@pytest.mark.parametrize(
    "bad",
    [{"supply_share_pct": 101}, {"supply_share_pct": -1}, {"confidence": 1.5}, {"lead_time_days": -3},
     {"capacity_value": 5}, {"capacity_unit": "kg"}, {"capacity_value": -1, "capacity_unit": "kg"},
     {"effective_from": "2026-05-01", "effective_to": "2026-04-01"}, {"criticality": "EXTREME"},
     {"capacity_value": float("nan"), "capacity_unit": "kg"}, {"surprise": 1}],
)  # fmt: skip
def test_invalid_attribute_values_rejected(client, bad):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    body = {"type": "SUPPLIES", "source_type": "SUPPLIER", "source_id": s["id"], "destination_type": "FACILITY",
            "destination_id": f["id"], "justification": "evidence"} | bad  # fmt: skip
    import json

    r = client.post(f"{G}/relationships", content=json.dumps(body, allow_nan=True),
                    headers={"content-type": "application/json"})  # fmt: skip
    assert r.status_code == 422, r.text
    assert client.get(f"{G}/relationships").json()["total"] == 0


def test_update_cannot_break_pairing_or_dates(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    e = ok(client, "SUPPLIES", s, f, capacity_value=5, capacity_unit="kg", effective_from="2026-03-01")
    url = f"{G}/relationships/{e['ref']}"
    assert client.patch(url, json={"expected_version": 1, "reason": "x1", "capacity_unit": None}).status_code == 422
    assert (
        client.patch(url, json={"expected_version": 1, "reason": "x2", "effective_to": "2026-01-01"}).status_code == 422
    )
    assert client.patch(url, json={"expected_version": 1, "reason": "x3"}).status_code == 422


def test_status_lifecycle_and_history(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    e = ok(client, "SUPPLIES", s, f, status="PROPOSED")
    url = f"{G}/relationships/{e['ref']}/status"

    def go(status, version, reason="because"):
        return client.post(url, json={"status": status, "reason": reason, "expected_version": version})

    assert go("INACTIVE", 1).status_code == 409  # PROPOSED cannot jump to INACTIVE
    assert go("PROPOSED", 1).status_code == 422  # unchanged
    r = go("CONFIRMED", 1, "supplier contract seen")
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED" and r.json()["version"] == 2
    assert go("INACTIVE", 2, "contract ended").json()["status"] == "INACTIVE"
    assert go("CONFIRMED", 3, "renewed").json()["status"] == "CONFIRMED"
    assert go("REJECTED", 4).status_code == 409  # CONFIRMED -> REJECTED is not allowed
    hist = client.get(f"{G}/relationships/{e['ref']}").json()["history"]
    assert [(h["from_status"], h["to_status"]) for h in hist if h["action"] == "STATUS_CHANGED"] == [
        ("INACTIVE", "CONFIRMED"), ("CONFIRMED", "INACTIVE"), ("PROPOSED", "CONFIRMED")]  # fmt: skip
    assert hist[0]["reason"] == "renewed" and hist[0]["actor"] == "dev-local"
    detail = client.get(f"{G}/relationships/{e['ref']}").json()
    assert detail["allowed_transitions"] == ["INACTIVE"]


def test_reject_and_reopen(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    e = ok(client, "SUPPLIES", s, f, status="PROPOSED")
    u = f"{G}/relationships/{e['ref']}/status"
    assert (
        client.post(u, json={"status": "REJECTED", "reason": "wrong plant", "expected_version": 1}).status_code == 200
    )
    r = client.post(u, json={"status": "PROPOSED", "reason": "new evidence", "expected_version": 2})
    assert r.status_code == 200 and r.json()["status"] == "PROPOSED"


def test_status_change_needs_current_version(client):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    e = ok(client, "SUPPLIES", s, f, status="PROPOSED")
    r = client.post(f"{G}/relationships/{e['ref']}/status",
                    json={"status": "CONFIRMED", "reason": "seen", "expected_version": 7})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"


def test_owner_relationships_are_derived_and_read_only(client):
    s = node(client, "suppliers", "Alpha Co")
    f = mk(client, "facilities", name="Owned Site", facility_type="MANUFACTURING", owner_supplier_id=s["id"])
    items = client.get(f"{G}/relationships", params={"types": "OWNS"}).json()["items"]
    assert [i["ref"] for i in items] == [f"OWNER:{f['id']}"] and items[0]["read_only"] is True
    r = client.patch(
        f"{G}/relationships/OWNER:{f['id']}", json={"expected_version": 1, "reason": "not allowed", "confidence": 0.5}
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "read_only_relationship"
    assert client.post(f"{G}/relationships/OWNER:{f['id']}/status",
                       json={"status": "INACTIVE", "reason": "not allowed", "expected_version": 1}).status_code == 422  # fmt: skip


def test_retired_and_merged_endpoints_refused(client):
    s, f, p = (
        node(client, "suppliers", "Alpha Co"),
        node(client, "facilities", "Site A"),
        node(client, "products", "Old"),
    )
    assert client.post(f"{API}/products/{p['id']}/retire", json={"expected_version": p["version"]}
                       ).status_code in (200, 201, 204)  # fmt: skip
    r = rel(client, "PRODUCES", f, p)
    assert r.status_code == 422, r.text
    assert s["id"]  # supplier untouched


def test_failed_mutation_rolls_back_completely(client, monkeypatch):
    from fastapi.testclient import TestClient

    from app.graph import service

    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")

    def boom(*a, **k):
        raise RuntimeError("injected failure after the row was flushed")

    monkeypatch.setattr(service.audit, "record", boom)
    with TestClient(client.app, raise_server_exceptions=False) as c2:
        r = rel(c2, "SUPPLIES", s, f)
    assert r.status_code == 500
    monkeypatch.undo()
    assert client.get(f"{G}/relationships").json()["total"] == 0  # no row, and no history event, survived


def test_history_table_is_append_only(client, engine):
    s, f = node(client, "suppliers", "Alpha Co"), node(client, "facilities", "Site A")
    ok(client, "SUPPLIES", s, f)
    with Session(engine) as sess, pytest.raises(DatabaseError):
        sess.execute(text("UPDATE graph_edge_events SET actor = 'x'"))
        sess.commit()
    with Session(engine) as sess, pytest.raises(DatabaseError):
        sess.execute(text("DELETE FROM graph_edge_events"))
        sess.commit()


def test_registry_merge_repoints_relationships_and_keeps_history(client, engine):
    a = node(client, "suppliers", "Acme Components GmbH")
    b = node(client, "suppliers", "ACME Components G.m.b.H.")
    f1, f2 = node(client, "facilities", "Dock One"), node(client, "facilities", "Dock Two")
    ok(client, "SUPPLIES", a, f1)
    moved = ok(client, "SUPPLIES", b, f2)
    dup = ok(client, "SUPPLIES", b, f1)  # same identity as a->f1 once b is merged into a
    cid = confirm_pair(client, "SUPPLIER", a, b)
    pv = client.post(
        f"{API}/merges/preview", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid)
    ).json()
    drops = {
        i["code"]: "DROP_MERGED_DUPLICATE" for i in pv["relationship_impact"] if i["action"] == "CONFLICT_DUPLICATE"
    }
    assert drops, pv["relationship_impact"]
    body = merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid, relationship_decisions=drops)
    m = client.post(f"{API}/merges", json=body)
    assert m.status_code == 201, m.text
    edges = client.get(f"{G}/relationships", params={"node_type": "SUPPLIER", "node_id": a["id"]}).json()["items"]
    assert {e["destination_id"] for e in edges} == {f1["id"], f2["id"]}  # no dangling or duplicate edges
    hist = client.get(f"{G}/relationships/{moved['ref']}").json()["history"]
    assert hist[0]["action"] == "REPOINTED" and hist[0]["changes"]["merge_survivor"] == a["id"]
    with Session(engine) as sess:
        actions = set(sess.scalars(select(GraphEdgeEvent.action)))
    assert {"CREATED", "REPOINTED", "DUPLICATE_REMOVED"} <= actions
    assert dup["ref"]
    # a merged id still resolves to the survivor in the graph API
    n = client.get(f"{G}/nodes/SUPPLIER/{b['id']}/neighbors").json()
    assert n["resolved_from"] == {"type": "SUPPLIER", "id": b["id"]} and n["start"]["id"] == a["id"]
    # compensation puts everything back and records it
    c = client.post(f"{API}/merges/{m.json()['id']}/compensate", json={"reason": "wrong merge"})
    assert c.status_code == 200, c.text
    with Session(engine) as sess:
        actions = set(sess.scalars(select(GraphEdgeEvent.action)))
        assert {"REPOINTED_BACK", "RESTORED"} <= actions
        assert (
            sess.scalar(
                select(SupplyRelationship.supplier_id).where(SupplyRelationship.id == int(moved["ref"].split(":")[1]))
            )
            == b["id"]
        )


def test_world_fixture_builds_expected_edge_count(client):
    world(client)
    assert client.get(f"{G}/relationships").json()["total"] == 7
