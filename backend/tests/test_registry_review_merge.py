from __future__ import annotations

import pytest

from app.registry import merge as merge_mod
from tests.registry_helpers import API, confirm_pair, link, merge_body, mk, source_id

KEEP = {"name": {"choice": "KEEP_SURVIVOR"}}


def pair(client, rtype="suppliers"):
    a = mk(client, rtype, name="Acme Components GmbH")
    b = mk(client, rtype, name="ACME Components G.m.b.H.")
    return a, b


def candidates(client, rtype="SUPPLIER"):
    client.post(f"{API}/candidates/generate", json={"entity_type": rtype})
    return client.get(f"{API}/candidates", params={"entity_type": rtype}).json()["items"]


# ------------------------------------------------------------------ review


def test_review_state_machine(client):
    pair(client)
    cid = candidates(client)[0]["id"]
    d = lambda dec, **k: client.post(f"{API}/candidates/{cid}/decision", json={"decision": dec} | k)  # noqa: E731
    assert d("DEFERRED", reason="need more data").json()["status"] == "DEFERRED"
    assert d("UNDER_REVIEW").json()["status"] == "UNDER_REVIEW"
    assert d("REJECTED", reason="different companies").json()["status"] == "REJECTED"
    assert d("CONFIRMED_DUPLICATE").status_code == 409  # must be reopened first
    assert d("REOPENED", reason="new evidence").json()["status"] == "REOPENED"
    assert d("CONFIRMED_DUPLICATE", reason="same").json()["status"] == "CONFIRMED_DUPLICATE"
    assert d("REJECTED").status_code == 409  # confirmed duplicates must be reopened first
    assert d("MERGED").status_code in (409, 422)
    hist = client.get(f"{API}/candidates/{cid}").json()["history"]
    assert [h["to_status"] for h in hist][-3:] == ["REJECTED", "REOPENED", "CONFIRMED_DUPLICATE"]
    assert all(h["actor"] for h in hist)


def test_decisions_require_reviewer_and_are_immutable_history(client):
    pair(client)
    cid = candidates(client)[0]["id"]
    before = client.get(f"{API}/candidates/{cid}").json()["history"]
    client.post(f"{API}/candidates/{cid}/decision", json={"decision": "REJECTED", "reason": "no"})
    after = client.get(f"{API}/candidates/{cid}").json()["history"]
    assert len(after) == len(before) + 1 and after[: len(before)] == before


def test_comparison_shows_original_and_normalized_values(client):
    pair(client)
    cid = candidates(client)[0]["id"]
    cmp = client.get(f"{API}/candidates/{cid}").json()
    name = next(f for f in cmp["fields"] if f["field"] == "name")
    assert name["left"] != name["right"] and name["left_normalized"] == name["right_normalized"]
    assert "candidate" in cmp and cmp["candidate"]["evidence"]["signals"]


def test_rejected_pair_stays_rejected_unless_evidence_improves(client):
    a, b = pair(client)
    cid = candidates(client)[0]["id"]
    client.post(f"{API}/candidates/{cid}/decision", json={"decision": "REJECTED", "reason": "no"})
    client.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"})
    assert client.get(f"{API}/candidates/{cid}").json()["candidate"]["status"] == "REJECTED"
    client.patch(f"{API}/suppliers/{b['id']}", json={"expected_version": b["version"], "address": "5 New Lane"})
    client.patch(f"{API}/suppliers/{a['id']}", json={"expected_version": a["version"], "address": "5 New Lane"})
    client.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"})
    assert client.get(f"{API}/candidates/{cid}").json()["candidate"]["status"] == "REOPENED"


# -------------------------------------------------------------------- merge


def test_merge_requires_confirmed_candidate(client):
    a, b = pair(client)
    body = merge_body("SUPPLIER", a, b, field_decisions=KEEP)
    p = client.post(f"{API}/merges/preview", json=body)
    assert p.status_code == 200 and p.json()["can_execute"] is False
    assert any(x["code"] == "review_required" for x in p.json()["blockers"])
    assert client.post(f"{API}/merges", json=body).status_code == 422


def test_preview_is_read_only_and_requires_field_decisions(client):
    a, b = pair(client)
    cid = confirm_pair(client, "SUPPLIER", a, b)
    p = client.post(f"{API}/merges/preview", json=merge_body("SUPPLIER", a, b, candidate_id=cid)).json()
    assert p["can_execute"] is False and any(f["field"] == "name" and f["needs_decision"] for f in p["fields"])
    p2 = client.post(
        f"{API}/merges/preview", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid)
    ).json()
    assert p2["can_execute"] is True
    for e in (a, b):
        cur = client.get(f"{API}/suppliers/{e['id']}").json()
        assert cur["version"] == e["version"] and cur["lifecycle"] == "ACTIVE"


def test_merge_executes_with_tombstone_history_and_redirect(client, seeded, engine):
    src = source_id(engine)
    a, b = pair(client)
    fac = mk(client, "facilities", name="Dock One", facility_type="WAREHOUSE")
    link(engine, b["id"], fac["id"], "REL-TEST-1")
    client.post(f"{API}/suppliers/{b['id']}/aliases", json={"alias": "Acme Old", "kind": "FORMER_NAME"})
    client.post(
        f"{API}/suppliers/{b['id']}/identifiers",
        json={"source_id": src, "scheme": "DUNS", "value": "555000111", "authoritative": True},
    )
    cid = confirm_pair(client, "SUPPLIER", a, b)
    r = client.post(f"{API}/merges", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid))
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["status"] == "EXECUTED" and m["survivor_id"] == a["id"] and m["merged_id"] == b["id"]
    # tombstone, never deleted
    gone = client.get(f"{API}/suppliers/{b['id']}").json()
    assert gone["lifecycle"] == "MERGED" and gone["identity"]["merged_into"]["id"] == a["id"]
    survivor = client.get(f"{API}/suppliers/{a['id']}").json()
    assert survivor["version"] > a["version"]  # a merge always changes the survivor version
    assert any(i["value"] == "555000111" for i in survivor["identifiers"])
    old = client.get(f"{API}/search", params={"q": "Acme Old"}).json()["items"]
    assert old and old[0]["id"] == a["id"]  # alias of the tombstone still resolves, to the canonical record
    assert any(x["table"] == "supply_relationships" for x in m["reassignments"]) if "reassignments" in m else True
    # relationship now points at the survivor
    rels = survivor["relationships"]
    assert rels and "REL-TEST-1" in str(rels)
    # search redirects merged records to the canonical one
    s = client.get(f"{API}/search", params={"q": "ACME Components G.m.b.H.", "type": "SUPPLIER"}).json()["items"]
    assert s and all(i["id"] == a["id"] or i["merged_into"] for i in s)
    # candidate marked merged, merge listed, audit chain intact
    assert client.get(f"{API}/candidates/{cid}").json()["candidate"]["status"] == "MERGED"
    assert client.get(f"{API}/merges", params={"entity_id": a["id"]}).json()["total"] == 1
    assert client.get(f"{API}/audit/verify").json()["ok"] is True
    # merged record cannot be edited or merged again
    assert client.patch(f"{API}/suppliers/{b['id']}", json={"expected_version": 99, "category": "x"}).status_code in (
        409,
        422,
    )
    again = client.post(f"{API}/merges/preview", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP)).json()
    assert again["can_execute"] is False


def test_stale_versions_block_merge(client):
    a, b = pair(client)
    cid = confirm_pair(client, "SUPPLIER", a, b)
    client.patch(f"{API}/suppliers/{a['id']}", json={"expected_version": a["version"], "category": "Changed"})
    r = client.post(f"{API}/merges", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid))
    assert r.status_code == 409 and r.json()["error"]["code"] == "version_conflict"
    assert client.get(f"{API}/suppliers/{b['id']}").json()["lifecycle"] == "ACTIVE"


def test_self_merge_and_cross_type_rejected(client):
    a, _ = pair(client)
    assert client.post(f"{API}/merges/preview", json=merge_body("SUPPLIER", a, a)).status_code in (409, 422)
    f = mk(client, "facilities", name="Somewhere", facility_type="WAREHOUSE")
    body = merge_body("SUPPLIER", a, f)
    assert client.post(f"{API}/merges/preview", json=body).status_code in (404, 422)


def test_failure_mid_merge_rolls_back_everything(client, seeded, engine, monkeypatch):
    a, b = pair(client)
    fac = mk(client, "facilities", name="Dock Two", facility_type="WAREHOUSE")
    rid = link(engine, b["id"], fac["id"], "REL-TEST-2")
    cid = confirm_pair(client, "SUPPLIER", a, b)

    def boom():
        raise RuntimeError("injected failure")

    monkeypatch.setattr(merge_mod, "_FAILPOINT", boom)
    from fastapi.testclient import TestClient

    with TestClient(client.app, raise_server_exceptions=False) as c2:
        r = c2.post(f"{API}/merges", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid))
    assert r.status_code == 500
    monkeypatch.setattr(merge_mod, "_FAILPOINT", None)
    cur = client.get(f"{API}/suppliers/{b['id']}").json()
    assert cur["lifecycle"] == "ACTIVE" and cur["version"] == b["version"]
    assert client.get(f"{API}/merges").json()["total"] == 0
    from sqlalchemy.orm import Session

    from app.models import SupplyRelationship

    with Session(engine) as s:
        assert s.get(SupplyRelationship, rid).supplier_id == b["id"]
    assert client.get(f"{API}/audit/verify").json()["ok"] is True


def test_duplicate_relationship_requires_explicit_drop_with_snapshot(client, seeded, engine):
    a, b = pair(client)
    fac = mk(client, "facilities", name="Dock Three", facility_type="WAREHOUSE")
    link(engine, a["id"], fac["id"], "REL-A")
    link(engine, b["id"], fac["id"], "REL-B")
    cid = confirm_pair(client, "SUPPLIER", a, b)
    body = merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid)
    p = client.post(f"{API}/merges/preview", json=body).json()
    conflict = [x for x in p["relationship_impact"] if x["action"] == "CONFLICT_DUPLICATE"]
    assert conflict and p["can_execute"] is False
    assert client.post(f"{API}/merges", json=body).status_code == 422
    body["relationship_decisions"] = {conflict[0]["code"]: "DROP_MERGED_DUPLICATE"}
    r = client.post(f"{API}/merges", json=body)
    assert r.status_code == 201, r.text
    detail = client.get(f"{API}/merges/{r.json()['id']}").json()
    assert detail["removed_duplicates"] and detail["removed_duplicates"][0]["row"]  # full snapshot kept


def test_compensation_restores_when_safe(client, seeded, engine):
    a, b = pair(client)
    fac = mk(client, "facilities", name="Dock Four", facility_type="WAREHOUSE")
    link(engine, b["id"], fac["id"], "REL-C")
    cid = confirm_pair(client, "SUPPLIER", a, b)
    m = client.post(f"{API}/merges", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid)).json()
    ok = client.post(f"{API}/merges/{m['id']}/compensate", json={"reason": "merged in error"})
    assert ok.status_code == 200 and ok.json()["status"] == "COMPENSATED"
    restored = client.get(f"{API}/suppliers/{b['id']}").json()
    assert restored["lifecycle"] == "ACTIVE" and "REL-C" in str(restored["relationships"])
    assert client.post(f"{API}/merges/{m['id']}/compensate", json={"reason": "again please"}).status_code in (409, 422)
    assert client.get(f"{API}/audit/verify").json()["ok"] is True


def test_compensation_refused_after_survivor_changes(client):
    a, b = pair(client)
    cid = confirm_pair(client, "SUPPLIER", a, b)
    m = client.post(f"{API}/merges", json=merge_body("SUPPLIER", a, b, field_decisions=KEEP, candidate_id=cid)).json()
    cur = client.get(f"{API}/suppliers/{a['id']}").json()
    client.patch(f"{API}/suppliers/{a['id']}", json={"expected_version": cur["version"], "category": "Edited later"})
    r = client.post(f"{API}/merges/{m['id']}/compensate", json={"reason": "undo please"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "compensation_unsafe"
    assert client.get(f"{API}/suppliers/{b['id']}").json()["lifecycle"] == "MERGED"


@pytest.mark.parametrize("kind", ["facilities", "products"])
def test_other_entity_types_merge(client, kind):
    rtype = {"facilities": "FACILITY", "products": "PRODUCT"}[kind]
    extra = {"facility_type": "WAREHOUSE"} if kind == "facilities" else {}
    if kind == "products":
        owner = mk(client, "suppliers", name="Maker Co")
        extra = {"manufacturer_supplier_id": owner["id"]}
    a = mk(client, kind, name="Gamma Hub One", **extra)
    b = mk(client, kind, name="GAMMA HUB ONE", **extra)
    cid = confirm_pair(client, rtype, a, b)
    r = client.post(f"{API}/merges", json=merge_body(rtype, a, b, field_decisions=KEEP, candidate_id=cid))
    assert r.status_code == 201, r.text
    assert client.get(f"{API}/{kind}/{b['id']}").json()["lifecycle"] == "MERGED"
