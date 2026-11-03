from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

P = "/api/v1"
CSV = {"Content-Type": "text/csv"}
GOOD = "external_id,name,country_or_region,category\nS-1,Acme,Germany,Electronics\nS-2,Bolt,Mexico,Metal\n"
BAD = GOOD + "S-3,,Chile,Metal\n"


@pytest.fixture
def src(client):
    r = client.post(f"{P}/ingestion/sources", json={"namespace": "erp-a", "name": "ERP A"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def q(src, **kw):
    base = {"source_id": src, "entity_type": "SUPPLIER"}
    base.update(kw)
    return base


def test_formats_rules_summary(client):
    f = client.get(f"{P}/ingestion/formats").json()
    assert {x["format"] for x in f["formats"]} == {"CSV", "JSON"} and f["limits"]["max_upload_bytes"] > 0
    assert any(x["name"] == "external_id" and x["required"] for x in f["entity_types"]["SUPPLIER"])
    assert {r["rule_id"] for r in client.get(f"{P}/ingestion/rules").json()} >= {"DQ001", "DQ019"}
    assert client.get(f"{P}/ingestion/summary").json()["batches"] == 0


def test_source_namespace_rules(client, src):
    assert client.post(f"{P}/ingestion/sources", json={"namespace": "erp-a", "name": "x"}).status_code == 409
    assert client.post(f"{P}/ingestion/sources", json={"namespace": "Bad Space", "name": "x"}).status_code == 422
    assert client.get(f"{P}/ingestion/sources/{src}").json()["provenance"] == "IMPORTED_EXTERNAL"
    assert client.get(f"{P}/ingestion/sources/999").status_code == 404


def test_preview_then_commit_flow(client, src):
    pre = client.post(f"{P}/ingestion/preview", params=q(src, filename="s.csv"), content=GOOD, headers=CSV)
    assert pre.status_code == 200
    body = pre.json()
    assert body["persisted"] is False and body["counts"]["to_create"] == 2 and body["would_apply"]
    assert client.get(f"{P}/suppliers", params={"q": "Acme"}).json()["total"] == 0  # nothing stored
    assert client.get(f"{P}/ingestion/batches").json()["total"] == 0
    c = client.post(
        f"{P}/ingestion/commit",
        params=q(src, expected_plan_digest=body["plan_digest"], filename="s.csv"),
        content=GOOD,
        headers=CSV,
    )
    assert c.status_code == 201 and c.json()["ok"] and c.json()["batch"]["created_count"] == 2
    again = client.post(f"{P}/ingestion/commit", params=q(src, filename="s.csv"), content=GOOD, headers=CSV)
    assert again.status_code == 200 and again.json()["replay"] is True
    sup = client.get(f"{P}/suppliers", params={"q": "Acme"}).json()["items"][0]
    assert sup["source_namespace"] == "erp-a" and sup["external_id"] == "S-1"
    post = client.post(f"{P}/ingestion/preview", params=q(src), content=GOOD, headers=CSV).json()
    assert post["already_applied_batch"] == c.json()["batch"]["batch_uid"]


def test_atomic_rejection_is_not_success_and_findings_are_queryable(client, src):
    r = client.post(f"{P}/ingestion/commit", params=q(src, policy="ATOMIC"), content=BAD, headers=CSV)
    assert r.status_code == 422 and r.json()["ok"] is False and r.json()["batch"]["status"] == "REJECTED"
    bid = r.json()["batch"]["id"]
    assert client.get(f"{P}/suppliers").json()["total"] == 0
    rows = client.get(f"{P}/ingestion/batches/{bid}/rows", params={"outcome": "REJECTED"}).json()
    assert rows["items"][0]["row_index"] == 3 and rows["items"][0]["source_line"] == 4
    fnd = client.get(f"{P}/ingestion/findings", params={"batch_id": bid, "severity": "ERROR"}).json()
    assert fnd["total"] == 1 and fnd["items"][0]["rule_id"] == "DQ001"
    fid = fnd["items"][0]["id"]
    upd = client.put(
        f"{P}/ingestion/findings/{fid}/resolution", json={"resolution_status": "ACKNOWLEDGED", "note": "seen"}
    )
    assert upd.status_code == 200 and upd.json()["resolution_status"] == "ACKNOWLEDGED"
    assert client.get(f"{P}/ingestion/findings/{fid}").json()["resolution_note"] == "seen"
    assert client.get(f"{P}/ingestion/findings", params={"rule_id": "BAD"}).status_code == 422


def test_stale_digest_conflicts(client, src):
    pre = client.post(f"{P}/ingestion/preview", params=q(src), content=GOOD, headers=CSV).json()
    client.post(
        f"{P}/ingestion/commit",
        params=q(src),
        content="external_id,name,country_or_region,category\nS-1,Other,X,Y\n",
        headers=CSV,
    )
    r = client.post(
        f"{P}/ingestion/commit", params=q(src, expected_plan_digest=pre["plan_digest"]), content=GOOD, headers=CSV
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "stale_preview"


def test_json_ingestion_and_lineage(client, src):
    doc = json.dumps(
        {"records": [{"external_id": "S-9", "name": "Json Co", "country_or_region": "Peru", "category": "Mining"}]}
    )
    r = client.post(
        f"{P}/ingestion/commit",
        params=q(src, filename="a.json"),
        content=doc,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 201 and r.json()["batch"]["source_format"] == "JSON"
    sup = client.get(f"{P}/suppliers", params={"q": "Json"}).json()["items"][0]
    lin = client.get(f"{P}/lineage/SUPPLIER/{sup['id']}").json()
    assert lin["total"] == 1 and lin["items"][0]["file_name"] == "a.json" and lin["items"][0]["outcome"] == "CREATED"
    assert lin["items"][0]["source_namespace"] == "erp-a" and lin["items"][0]["normalization_version"] == "1"


def test_request_validation(client, src):
    assert client.post(f"{P}/ingestion/preview", params=q(src), content=GOOD).status_code == 415  # no content type
    assert client.post(f"{P}/ingestion/preview", params=q(src), content="", headers=CSV).status_code == 422
    assert client.post(f"{P}/ingestion/preview", params=q(999), content=GOOD, headers=CSV).status_code == 404
    assert (
        client.post(f"{P}/ingestion/preview", params=q(src, entity_type="NOPE"), content=GOOD, headers=CSV).status_code
        == 422
    )
    assert (
        client.post(f"{P}/ingestion/preview", params=q(src, encoding="rot13"), content=GOOD, headers=CSV).status_code
        == 422
    )
    assert (
        client.post(f"{P}/ingestion/preview", params=q(src, delimiter="x"), content=GOOD, headers=CSV).status_code
        == 422
    )
    assert (
        client.post(
            f"{P}/ingestion/commit", params=q(src, expected_plan_digest="zz"), content=GOOD, headers=CSV
        ).status_code
        == 422
    )


def test_oversize_upload_is_413(settings, engine):
    small = settings.model_copy(update={"max_upload_bytes": 1024})
    with TestClient(create_app(small, engine)) as c:
        sid = c.post(f"{P}/ingestion/sources", json={"namespace": "big", "name": "Big"}).json()["id"]
        r = c.post(f"{P}/ingestion/preview", params=q(sid), content="a," * 2000, headers=CSV)
        assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"
        assert c.get(f"{P}/health").status_code == 200


def test_writes_are_disabled_in_production_by_default(engine, db_url, tmp_path):
    tok = "e" * 20 + "tokenBBBB"
    auth = {"Authorization": f"Bearer {tok}"}
    prod = Settings(
        _env_file=None,
        environment="production",
        cors_allowed_origins=["https://x.example"],
        database_url=db_url,
        data_dir=tmp_path,
        api_tokens=f"{tok}:editor-user:editor",
    )
    with TestClient(create_app(prod, engine)) as c:
        # Identity: without a token configured/sent production never reaches business logic (deny by default).
        assert c.get(f"{P}/ingestion/summary").status_code == 401
        assert (
            c.post(f"{P}/ingestion/sources", json={"namespace": "abc", "name": "n"}, headers=auth).json()["error"][
                "code"
            ]
            == "write_api_disabled"
        )
        assert (
            c.post(
                f"{P}/ingestion/commit",
                params={"source_id": 1, "entity_type": "SUPPLIER"},
                content=GOOD,
                headers=CSV | auth,
            ).status_code
            == 403
        )
        assert c.get(f"{P}/ingestion/summary", headers=auth).status_code == 200  # reads and dry-runs stay available


def test_production_without_configured_tokens_refuses_every_data_route(engine, db_url, tmp_path):
    prod = Settings(
        _env_file=None,
        environment="production",
        cors_allowed_origins=["https://x.example"],
        database_url=db_url,
        data_dir=tmp_path,
    )
    with TestClient(create_app(prod, engine)) as c:
        r = c.get(f"{P}/ingestion/summary")
        assert r.status_code == 503 and r.json()["error"]["code"] == "auth_not_configured"


def test_findings_csv_export_neutralizes_formulas(client, src):
    evil = "external_id,name,country_or_region,category\nS-1,=HYPERLINK(\"http://evil\"),DE,X\nS-2,+cmd|' /C calc'!A0,DE,X\n"  # noqa: E501
    client.post(f"{P}/ingestion/commit", params=q(src), content=evil, headers=CSV)
    client.post(
        f"{P}/ingestion/commit",
        params=q(src, policy="ATOMIC"),
        content="external_id,name,country_or_region,category\n=1+1,n,c,k\n",
        headers=CSV,
    )
    r = client.get(f"{P}/ingestion/findings/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["x-content-type-options"] == "nosniff"
    for line in r.text.splitlines()[1:]:
        assert not line.startswith(("=", "+", "@"))
    assert "'=1+1" in r.text  # observed value kept as text
    assert client.get(f"{P}/suppliers", params={"q": "HYPERLINK"}).json()["total"] == 1  # stored as plain data


def test_mapping_profile_lifecycle_over_api(client, src):
    d = {
        "columns": {
            "external_id": {"source": "Id"},
            "name": {"source": "Name"},
            "country_or_region": {"source": "Land"},
            "category": {"source": "Cat"},
        },
        "unknown_field_policy": "ignore",
    }
    v = client.post(
        f"{P}/ingestion/mapping-profiles/validate", json={"entity_type": "SUPPLIER", "definition": d}
    ).json()
    assert v == {"valid": True, "problems": []}
    bad = client.post(
        f"{P}/ingestion/mapping-profiles/validate",
        json={"entity_type": "SUPPLIER", "definition": {"columns": {"nope": {"source": "x"}}}},
    ).json()
    assert not bad["valid"] and len(bad["problems"]) >= 2
    p1 = client.post(
        f"{P}/ingestion/mapping-profiles",
        json={"name": "erp supplier", "entity_type": "SUPPLIER", "source_id": src, "definition": d},
    ).json()
    assert p1["version"] == 1 and p1["status"] == "DRAFT"
    data = "Id,Name,Land,Cat,Junk\nA1,Alpha,DE,Parts,x\n"
    draft_commit = client.post(
        f"{P}/ingestion/commit", params=q(src, mapping_profile_id=p1["id"], format="CSV"), content=data
    )
    assert draft_commit.status_code == 422  # drafts cannot be committed
    assert client.post(f"{P}/ingestion/mapping-profiles/{p1['id']}/activate").json()["status"] == "ACTIVE"
    ok = client.post(f"{P}/ingestion/commit", params=q(src, mapping_profile_id=p1["id"], format="CSV"), content=data)
    assert ok.status_code == 201 and ok.json()["batch"]["mapping_profile_ref"] == "erp supplier:v1"
    p2 = client.post(
        f"{P}/ingestion/mapping-profiles", json={"name": "erp supplier", "entity_type": "SUPPLIER", "definition": d}
    ).json()
    assert p2["version"] == 2
    client.post(f"{P}/ingestion/mapping-profiles/{p2['id']}/activate")
    assert client.get(f"{P}/ingestion/mapping-profiles/{p1['id']}").json()["status"] == "RETIRED"
    assert client.post(f"{P}/ingestion/mapping-profiles/{p1['id']}/activate").status_code == 409
    assert client.get(f"{P}/ingestion/mapping-profiles", params={"status": "ACTIVE"}).json()["total"] == 1


def test_inspect_suggests_mapping(client):
    r = client.post(
        f"{P}/ingestion/inspect",
        params={"entity_type": "SUPPLIER"},
        content="Supplier ID,Supplier Name,Country,Category,Zzz\n1,A,B,C,D\n",
        headers=CSV,
    ).json()
    assert r["suggested_columns"]["external_id"]["source"] == "Supplier ID"
    assert r["suggested_columns"]["country_or_region"]["source"] == "Country"
    assert r["unmapped_columns"] == ["Zzz"] and r["row_count"] == 1


def test_products_and_links_endpoints(client, seeded):
    prods = client.get(f"{P}/products").json()
    assert prods["total"] == 4 and prods["items"][0]["source_namespace"] == "atlas-demo"
    pid = next(p["id"] for p in prods["items"] if p["product_code"] == "DEMO-PRD-001")
    d = client.get(f"{P}/products/{pid}").json()
    assert {link["link_type"] for link in d["links"]} == {"SUPPLIES_PRODUCT", "STORES"}
    assert client.get(f"{P}/product-links", params={"link_type": "PRODUCES"}).json()["total"] == 2
    assert client.get(f"{P}/products/9999").status_code == 404
    fac = client.get(f"{P}/facilities", params={"q": "Raw Materials"}).json()["items"][0]
    assert fac["owner"]["supplier_code"] == "DEMO-SUP-007"
