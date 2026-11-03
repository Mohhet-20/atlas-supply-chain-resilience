from __future__ import annotations

import pytest

from tests.registry_helpers import API, mk, source_id


def test_create_assigns_stable_code_version_and_provenance(client):
    s = mk(client, "suppliers", name="Orion Parts Ltd")
    assert s["version"] == 1 and s["code"].startswith("USR-SUP-")
    assert s["attributes"]["provenance"] == "USER_PROVIDED"
    assert s["normalized"]["name"] == "orion parts ltd"
    again = client.get(f"{API}/suppliers/{s['id']}").json()
    assert again["code"] == s["code"]


def test_validation_rejects_unknown_fields_and_mass_assignment(client):
    r = client.post(f"{API}/suppliers", json={"name": "X", "country_or_region": "DE", "category": "c", "version": 9})
    assert r.status_code == 422
    r = client.post(
        f"{API}/suppliers", json={"name": "X", "country_or_region": "DE", "category": "c", "merged_into_id": 1}
    )
    assert r.status_code == 422
    r = client.post(f"{API}/suppliers", json={"name": "", "country_or_region": "DE", "category": "c"})
    assert r.status_code == 422
    r = client.post(f"{API}/facilities", json={"name": "F", "country_or_region": "DE", "facility_type": "PORT"})
    assert r.status_code == 422


def test_update_requires_expected_version_and_detects_conflict(client):
    s = mk(client, "suppliers", name="Orion Parts Ltd")
    ok = client.patch(f"{API}/suppliers/{s['id']}", json={"expected_version": 1, "category": "Machinery"})
    assert ok.status_code == 200 and ok.json()["version"] == 2
    stale = client.patch(f"{API}/suppliers/{s['id']}", json={"expected_version": 1, "category": "Other"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    assert client.patch(f"{API}/suppliers/{s['id']}", json={"category": "x"}).status_code == 422
    hist = client.get(f"{API}/suppliers/{s['id']}/history").json()["items"]
    assert any(h["action"] == "registry.update" for h in hist)


def test_noop_update_does_not_bump_version(client):
    s = mk(client, "suppliers", name="Orion Parts Ltd")
    r = client.patch(
        f"{API}/suppliers/{s['id']}", json={"expected_version": 1, "category": s["attributes"]["category"]}
    )
    assert r.status_code == 200 and r.json()["version"] == 1


def test_parent_cycle_and_missing_reference_rejected(client):
    a = mk(client, "suppliers", name="Parent Co")
    b = mk(client, "suppliers", name="Child Co", parent_supplier_id=a["id"])
    r = client.patch(f"{API}/suppliers/{a['id']}", json={"expected_version": 1, "parent_supplier_id": b["id"]})
    assert r.status_code in (409, 422)
    r = client.patch(f"{API}/suppliers/{a['id']}", json={"expected_version": 1, "parent_supplier_id": a["id"]})
    assert r.status_code in (409, 422)
    r = client.post(f"{API}/products", json={"name": "P", "manufacturer_supplier_id": 99999})
    assert r.status_code in (404, 422)


def test_aliases_roundtrip_and_duplicate_alias(client):
    s = mk(client, "suppliers", name="Orion Parts Ltd")
    r = client.post(f"{API}/suppliers/{s['id']}/aliases", json={"alias": "Orion Parts Limited", "kind": "TRADING_NAME"})
    assert r.status_code == 201, r.text
    dup = client.post(
        f"{API}/suppliers/{s['id']}/aliases", json={"alias": "ORION parts limited", "kind": "TRADING_NAME"}
    )
    assert dup.status_code == 409
    aliases = client.get(f"{API}/suppliers/{s['id']}/aliases").json()["items"]
    assert len(aliases) == 1
    assert client.delete(f"{API}/suppliers/{s['id']}/aliases/{aliases[0]['id']}").status_code == 204
    assert client.get(f"{API}/suppliers/{s['id']}/aliases").json()["items"] == []


def test_identifiers_scoped_by_source_and_unique(client, seeded, engine):
    src = source_id(engine)
    a = mk(client, "suppliers", name="Orion Parts Ltd")
    b = mk(client, "suppliers", name="Vega Metals")
    body = {"source_id": src, "scheme": "LEI", "value": "LEI-TEST-0001", "authoritative": True}
    assert client.post(f"{API}/suppliers/{a['id']}/identifiers", json=body).status_code == 201
    clash = client.post(f"{API}/suppliers/{b['id']}/identifiers", json=body)
    assert clash.status_code == 409  # duplicate holder is a duplicate signal, never silently reassigned
    # case-sensitive values: a different case is a different identifier
    other = body | {"value": "lei-test-0001"}
    assert client.post(f"{API}/suppliers/{b['id']}/identifiers", json=other).status_code == 201
    listed = client.get(f"{API}/suppliers/{a['id']}/identifiers").json()["items"]
    assert [i["value"] for i in listed] == ["LEI-TEST-0001"]
    assert client.post(f"{API}/suppliers/{a['id']}/identifiers", json=body | {"source_id": 99999}).status_code in (
        404,
        422,
    )


def test_product_retire_and_reactivate(client):
    p = mk(client, "products", name="Widget A")
    r = client.post(f"{API}/products/{p['id']}/retire", json={"expected_version": p["version"]})
    assert r.status_code == 200 and r.json()["lifecycle"] == "RETIRED"
    again = client.post(f"{API}/products/{p['id']}/retire", json={"expected_version": r.json()["version"]})
    assert again.status_code in (409, 422)
    re = client.post(f"{API}/products/{p['id']}/reactivate", json={"expected_version": r.json()["version"]})
    assert re.status_code == 200 and re.json()["lifecycle"] != "RETIRED"
    assert client.post(f"{API}/products/{p['id']}/retire", json={"expected_version": 1, "x": 1}).status_code == 422


def test_write_gate_blocks_writes(settings, engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    s = settings.model_copy(update={"write_api_enabled": False, "environment": "production", "api_tokens": None})
    with TestClient(create_app(s, engine)) as c:
        assert c.get(f"{API}/me").status_code in (200, 503)


@pytest.mark.parametrize("q", ["a", "%", "_", "'; DROP TABLE suppliers;--", "名前", "x" * 100])
def test_search_handles_hostile_input(client, q):
    mk(client, "suppliers", name="Orion Parts Ltd")
    r = client.get(f"{API}/search", params={"q": q})
    assert r.status_code == 200
    assert client.get(f"{API}/search", params={"q": "x" * 101}).status_code == 422


def test_search_exact_vs_approximate_and_reasons(client, seeded, engine):
    src = source_id(engine)
    s = mk(client, "suppliers", name="Orion Parts Ltd")
    client.post(f"{API}/suppliers/{s['id']}/aliases", json={"alias": "Orion Trading", "kind": "TRADING_NAME"})
    client.post(
        f"{API}/suppliers/{s['id']}/identifiers", json={"source_id": src, "scheme": "DUNS", "value": "123456789"}
    )
    by_name = client.get(f"{API}/search", params={"q": "Orion Parts Ltd", "type": "SUPPLIER"}).json()["items"][0]
    assert by_name["exact"] is True and by_name["match_reasons"][0]["kind"].startswith("EXACT")
    by_alias = client.get(f"{API}/search", params={"q": "orion trading"}).json()["items"][0]
    assert by_alias["exact"] and any(m["kind"] == "EXACT_ALIAS" for m in by_alias["match_reasons"])
    by_code = client.get(f"{API}/search", params={"q": s["code"]}).json()["items"][0]
    assert by_code["id"] == s["id"] and by_code["exact"]
    by_ident = client.get(f"{API}/search", params={"q": "123456789"}).json()["items"]
    assert by_ident and by_ident[0]["id"] == s["id"]
    fuzzy = client.get(f"{API}/search", params={"q": "Orin Parts"}).json()
    assert fuzzy["items"] and fuzzy["items"][0]["approximate"] and not fuzzy["items"][0]["exact"]
    assert "never imply" in " ".join(fuzzy["notes"])
    nofuzzy = client.get(f"{API}/search", params={"q": "Orin Parts", "fuzzy": "false"}).json()
    assert nofuzzy["items"] == []


def test_search_filters_pagination_and_determinism(client):
    for i in range(7):
        mk(client, "suppliers", name=f"Delta Works {i}", country_or_region="France")
    mk(client, "suppliers", name="Delta Works X", country_or_region="Spain")
    q = {"q": "Delta Works", "country": "France", "type": "SUPPLIER", "sort": "name"}
    p1 = client.get(f"{API}/search", params=q | {"limit": 3}).json()
    p2 = client.get(f"{API}/search", params=q | {"limit": 3, "offset": 3}).json()
    assert p1["total"] == 7 and len(p1["items"]) == 3 and len(p2["items"]) == 3
    assert not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]}
    assert client.get(f"{API}/search", params=q | {"limit": 3}).json() == p1
    assert client.get(f"{API}/search", params=q | {"limit": 101}).status_code == 422
    assert client.get(f"{API}/search", params=q | {"sort": "bogus"}).status_code == 422
