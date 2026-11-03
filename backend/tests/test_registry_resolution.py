from __future__ import annotations

from app.registry.config import ResolutionConfig
from tests.registry_helpers import API, mk, source_id


def _gen(client, rtype="SUPPLIER"):
    r = client.post(f"{API}/candidates/generate", json={"entity_type": rtype})
    assert r.status_code == 200, r.text
    return r.json()


def _cands(client, rtype="SUPPLIER", **params):
    return client.get(
        f"{API}/candidates",
        params={"entity_type": rtype, "limit": 100, "tier": ["STRONG", "POSSIBLE", "INSUFFICIENT"]} | params,
    ).json()["items"]


def _pair(cands, a, b):
    return next((c for c in cands if {c["left_id"], c["right_id"]} == {a["id"], b["id"]}), None)


def test_differing_authoritative_identifiers_are_hard_conflict(client, seeded, engine):
    src = source_id(engine)
    a = mk(client, "suppliers", name="Helios Fabrication Ltd")
    b = mk(client, "suppliers", name="Helios Fabrication Ltd")
    # shared VAT (same value cannot be held twice) is modelled via equal values under different sources is out of scope;
    # here both carry differing DUNS: a conflict, score collapses below POSSIBLE and nothing is proposed.
    for e, v in ((a, "999000111"), (b, "999000222")):
        r = client.post(
            f"{API}/suppliers/{e['id']}/identifiers",
            json={"source_id": src, "scheme": "DUNS", "value": v, "authoritative": True},
        )
        assert r.status_code == 201
    _gen(client)
    assert _pair(_cands(client, tier=["STRONG", "POSSIBLE"]), a, b) is None
    ev = client.get(
        f"{API}/candidates/compare", params={"entity_type": "SUPPLIER", "left_id": a["id"], "right_id": b["id"]}
    ).json()
    assert ev["hard_conflict"] is True and ev["tier"] != "STRONG"
    assert any(s["signal"] == "identifier_conflict" for s in ev["signals"])


def test_exact_names_without_corroboration_is_possible(client):
    a = mk(client, "suppliers", name="Zephyr Tooling GmbH")
    b = mk(client, "suppliers", name="Zephyr Tooling GmbH", country_or_region="Austria")
    _gen(client)
    c = _pair(_cands(client), a, b)
    assert c is None or c["tier"] != "STRONG"


def test_name_only_never_strong(client):
    a = mk(client, "suppliers", name="Nova Plastics")
    b = mk(client, "suppliers", name="Nova Plastics Inc")
    _gen(client)
    c = _pair(_cands(client), a, b)
    assert c is None or c["tier"] != "STRONG"  # one evidence family is not enough
    ev = client.get(
        f"{API}/candidates/compare", params={"entity_type": "SUPPLIER", "left_id": a["id"], "right_id": b["id"]}
    ).json()
    assert ev["tier"] != "STRONG" and ev["score"] < 0.8


def test_corroborated_match_can_be_strong(client):
    a = mk(client, "suppliers", name="Atlas Gears Ltd", address="12 Industrial Road", country_or_region="Poland")
    b = mk(client, "suppliers", name="ATLAS GEARS LIMITED", address="12 industrial rd", country_or_region="Poland")
    _gen(client)
    c = _pair(_cands(client), a, b)
    assert c, "expected a candidate"
    fams = set(c["evidence"]["families"])
    assert len(fams) >= 2
    assert c["tier"] in ("STRONG", "POSSIBLE")


def test_similar_but_distinct_entities_not_strong(client):
    a = mk(client, "suppliers", name="Pacific Steel Works", country_or_region="Japan", category="Metals")
    b = mk(client, "suppliers", name="Pacific Steel Works", country_or_region="Chile", category="Textiles")
    _gen(client)
    c = _pair(_cands(client), a, b)
    assert c is None or c["tier"] != "STRONG"


def test_unrelated_names_produce_no_candidate(client):
    a = mk(client, "suppliers", name="Quartz Mining Co")
    b = mk(client, "suppliers", name="Birch Furniture")
    _gen(client)
    assert _pair(_cands(client), a, b) is None


def test_missing_attributes_do_not_count_as_matches(client):
    a = mk(client, "products", name="Bolt M8", category=None)
    b = mk(client, "products", name="Bolt M8", category=None)
    _gen(client, "PRODUCT")
    ev = client.get(
        f"{API}/candidates/compare", params={"entity_type": "PRODUCT", "left_id": a["id"], "right_id": b["id"]}
    ).json()
    assert not any(s["signal"] == "category_match" for s in ev["signals"])


def test_alias_match_is_reported(client):
    a = mk(client, "suppliers", name="Meridian Logistics")
    b = mk(client, "suppliers", name="MLG Holdings")
    client.post(f"{API}/suppliers/{b['id']}/aliases", json={"alias": "Meridian Logistics", "kind": "FORMER_NAME"})
    _gen(client)
    ev = client.get(
        f"{API}/candidates/compare", params={"entity_type": "SUPPLIER", "left_id": a["id"], "right_id": b["id"]}
    ).json()
    assert any("alias" in s["signal"] for s in ev["signals"])


def test_generation_is_deterministic_and_idempotent(client):
    mk(client, "suppliers", name="Acme Components GmbH")
    mk(client, "suppliers", name="ACME Components G.m.b.H.")
    r1 = _gen(client)
    first = _cands(client)
    r2 = _gen(client)
    second = _cands(client)
    assert r1["candidates_new"] == 1 and r2["candidates_new"] == 0
    assert [(c["id"], c["score"], c["tier"]) for c in first] == [(c["id"], c["score"], c["tier"]) for c in second]
    assert r1["config_hash"] == r2["config_hash"]


def test_pair_is_ordered_and_unique(client):
    a = mk(client, "suppliers", name="Acme Components GmbH")
    b = mk(client, "suppliers", name="ACME Components G.m.b.H.")
    _gen(client)
    c = _pair(_cands(client), a, b)
    assert c["left_id"] < c["right_id"]
    r = client.get(
        f"{API}/candidates/compare", params={"entity_type": "SUPPLIER", "left_id": b["id"], "right_id": a["id"]}
    )
    assert r.status_code == 200 and r.json()["persisted"] is False and r.json()["score"] == c["score"]
    assert (
        client.get(
            f"{API}/candidates/compare", params={"entity_type": "SUPPLIER", "left_id": a["id"], "right_id": a["id"]}
        ).status_code
        == 422
    )


def test_facilities_distance_and_country(client):
    a = mk(
        client,
        "facilities",
        name="Rotterdam Hub",
        country_or_region="Netherlands",
        city="Rotterdam",
        facility_type="WAREHOUSE",
        latitude=51.92,
        longitude=4.48,
        location_status="VERIFIED",
    )
    b = mk(
        client,
        "facilities",
        name="Rotterdam Hub",
        country_or_region="Netherlands",
        city="Rotterdam",
        facility_type="WAREHOUSE",
        latitude=51.921,
        longitude=4.481,
        location_status="VERIFIED",
    )
    far = mk(
        client,
        "facilities",
        name="Rotterdam Hub",
        country_or_region="Netherlands",
        city="Rotterdam",
        facility_type="WAREHOUSE",
        latitude=35.0,
        longitude=139.0,
        location_status="VERIFIED",
    )
    _gen(client, "FACILITY")
    cs = _cands(client, "FACILITY")
    near = _pair(cs, a, b)
    assert near and any(s["signal"].startswith("geo") or s["family"] == "location" for s in near["evidence"]["signals"])
    far_c = _pair(cs, a, far)
    assert far_c is None or far_c["score"] < near["score"]


def test_bounds_truncate_and_report(settings, engine, seeded):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, engine)
    cfg = ResolutionConfig()
    app.state._res_cfg = cfg.model_copy(update={"max_pairs_per_run": 3})
    with TestClient(app) as c:
        for i in range(8):
            mk(c, "suppliers", name=f"Same Name Ltd {i % 2}", country_or_region="Italy")
        run = c.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"}).json()
        assert run["status"] == "TRUNCATED" and run["truncated_reason"]
        assert run["pairs_compared"] <= 3


def test_oversized_blocks_are_skipped_not_exploded(settings, engine, seeded):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings, engine)
    cfg = ResolutionConfig()
    app.state._res_cfg = cfg.model_copy(update={"max_block_size": 3})
    with TestClient(app) as c:
        for i in range(10):
            mk(c, "suppliers", name=f"Generic Supply {i}", country_or_region="Italy")
        run = c.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER"}).json()
        assert run["pairs_compared"] < 45  # all-pairs would be 45


def test_entity_ids_scope_limits_generation(client):
    a = mk(client, "suppliers", name="Acme Components GmbH")
    mk(client, "suppliers", name="ACME Components G.m.b.H.")
    c = mk(client, "suppliers", name="Zed Corp")
    run = client.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER", "entity_ids": [c["id"]]}).json()
    assert run["candidates_found"] == 0
    run = client.post(f"{API}/candidates/generate", json={"entity_type": "SUPPLIER", "entity_ids": [a["id"]]}).json()
    assert run["candidates_found"] == 1


def test_config_endpoints_expose_version(client):
    cfg = client.get(f"{API}/resolution/config").json()
    assert cfg["version"] == "rc-1" and len(cfg["hash"]) == 64
    _gen(client)
    runs = client.get(f"{API}/resolution/runs").json()["items"]
    assert runs and runs[0]["config_hash"] == cfg["hash"]
