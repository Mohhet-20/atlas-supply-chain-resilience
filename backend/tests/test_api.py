import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings
from app.main import create_app

API = "/api/v1"


def get(client, path, **params):
    return client.get(f"{API}{path}", params=params)


# ---- system ----
def test_health_is_independent_of_database(settings, tmp_path):
    from app.db.session import create_db_engine

    bare = create_db_engine(f"sqlite:///{(tmp_path / 'nope.db').as_posix()}")
    with TestClient(create_app(settings, bare)) as c:
        r = get(c, "/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "app": "ATLAS"}


def test_readiness_ok_when_migrated(client):
    r = get(client, "/readiness")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "checks": {"database": "ok", "schema": "ok"}}


def test_readiness_reports_unmigrated_without_leaking_details(settings, tmp_path):
    from app.db.session import create_db_engine

    bare = create_db_engine(f"sqlite:///{(tmp_path / 'bare.db').as_posix()}")
    with TestClient(create_app(settings, bare)) as c:
        r = get(c, "/readiness")
    assert r.status_code == 503
    assert r.json()["checks"]["schema"] == "not_migrated"
    assert str(tmp_path) not in r.text and "sqlite" not in r.text.lower()


# ---- dashboard ----
def test_dashboard_empty_database(client):
    d = get(client, "/dashboard/summary").json()
    assert (d["supplier_count"], d["facility_count"], d["relationship_count"], d["observation_count"]) == (0, 0, 0, 0)
    assert d["contains_synthetic_demo"] is False
    assert d["coverage_state"] == "insufficient_data" and d["coverage_indicator"] is None
    assert d["recent_observations"] == []
    assert all(m["state"] == "insufficient_data" for m in d["risk"]["metrics"])


def test_dashboard_seeded(client, seeded):
    d = get(client, "/dashboard/summary").json()
    assert (d["supplier_count"], d["facility_count"], d["relationship_count"], d["observation_count"]) == (7, 5, 11, 12)
    assert d["contains_synthetic_demo"] is True
    assert d["provenance_counts"]["SYNTHETIC_DEMO"] == 35
    assert d["provenance_counts"]["USER_PROVIDED"] == 0
    assert d["coverage_state"] == "ok" and 0 < d["coverage_indicator"] <= 1
    assert len(d["recent_observations"]) == 5
    times = [o["observed_at"] for o in d["recent_observations"]]
    assert times == sorted(times, reverse=True)
    keys = {m["key"] for m in d["risk"]["metrics"]}
    assert {"single_source_facilities", "supplier_dependency_concentration", "data_coverage_indicator"} <= keys
    conc = next(m for m in d["risk"]["metrics"] if m["key"] == "supplier_dependency_concentration")
    assert conc["inputs"] and conc["assumptions"] and conc["provenance"] == {"SYNTHETIC_DEMO": 7}


# ---- suppliers ----
def test_suppliers_empty_and_paginated(client, seeded):
    page = get(client, "/suppliers", limit=2, offset=0).json()
    assert page["total"] == 7 and len(page["items"]) == 2 and page["limit"] == 2
    last = get(client, "/suppliers", limit=2, offset=5).json()
    assert [i["supplier_code"] for i in last["items"]] == ["DEMO-SUP-006", "DEMO-SUP-007"]
    assert get(client, "/suppliers", offset=100).json()["items"] == []


def test_suppliers_empty_database(client):
    assert get(client, "/suppliers").json() == {"items": [], "total": 0, "limit": 25, "offset": 0}


def test_supplier_filters_and_search(client, seeded):
    assert get(client, "/suppliers", q="alpha").json()["total"] == 1
    assert get(client, "/suppliers", q="DEMO-SUP-00").json()["total"] == 7
    assert get(client, "/suppliers", status="ON_HOLD").json()["total"] == 1
    assert get(client, "/suppliers", category="Packaging").json()["total"] == 2
    assert get(client, "/suppliers", provenance="USER_PROVIDED").json()["total"] == 0
    # LIKE wildcards are escaped, not interpreted
    assert get(client, "/suppliers", q="%").json()["total"] == 0
    assert get(client, "/suppliers", q="_").json()["total"] == 0


def test_supplier_detail_includes_relationships_and_observations(client, seeded):
    items = get(client, "/suppliers", q="DEMO-SUP-001").json()["items"]
    d = get(client, f"/suppliers/{items[0]['id']}").json()
    assert d["provenance"] == "SYNTHETIC_DEMO"
    assert len(d["relationships"]) == 2
    rel = d["relationships"][0]
    assert (
        rel["relationship_type"] == "SUPPLIES"
        and rel["source"]["type"] == "SUPPLIER"
        and rel["target"]["type"] == "FACILITY"
    )
    assert len(d["recent_observations"]) == 2


def test_missing_supplier_returns_consistent_404(client):
    r = get(client, "/suppliers/12345")
    assert r.status_code == 404
    body = r.json()["error"]
    assert body["code"] == "not_found" and body["request_id"] and "Traceback" not in r.text


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": "abc"}, {"status": "BOGUS"}, {"q": "x" * 101}]
)
def test_invalid_query_parameters_are_422_and_do_not_echo_input(client, params):
    r = get(client, "/suppliers", **params)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and err["details"]
    assert all(set(d) == {"loc", "msg", "type"} for d in err["details"])
    assert "BOGUS" not in r.text and "xxxx" not in r.text


@pytest.mark.parametrize("bad", ["abc", "0", "-3", str(2**40)])
def test_invalid_id_path_is_422(client, bad):
    assert get(client, f"/suppliers/{bad}").status_code == 422


# ---- facilities ----
def test_facility_listing_filters(client, seeded):
    assert get(client, "/facilities").json()["total"] == 5
    assert get(client, "/facilities", facility_type="WAREHOUSE").json()["total"] == 1
    assert get(client, "/facilities", q="hub").json()["items"][0]["facility_code"] == "DEMO-FAC-003"
    assert get(client, "/facilities", facility_type="NOPE").status_code == 422


def test_facility_detail_has_inbound_and_outbound(client, seeded):
    fid = get(client, "/facilities", q="DEMO-FAC-003").json()["items"][0]["id"]
    d = get(client, f"/facilities/{fid}").json()
    assert {r["relationship_code"] for r in d["inbound_relationships"]} == {
        "DEMO-REL-005",
        "DEMO-REL-007",
        "DEMO-REL-008",
    }
    assert [r["relationship_code"] for r in d["outbound_relationships"]] == ["DEMO-REL-009"]
    assert all(r["provenance"] == "SYNTHETIC_DEMO" for r in d["inbound_relationships"])
    assert d["recent_observations"]


def test_missing_facility_404(client):
    assert get(client, "/facilities/777").status_code == 404


# ---- network ----
def test_network_summary_empty(client):
    d = get(client, "/network/summary").json()
    assert d["nodes"] == [] and d["edges"] == [] and d["truncated"] is False


def test_network_summary_seeded_and_truncation(client, seeded):
    d = get(client, "/network/summary").json()
    assert (d["supplier_count"], d["facility_count"], d["relationship_count"]) == (7, 5, 11)
    assert len(d["nodes"]) == 12 and len(d["edges"]) == 11 and d["truncated"] is False
    ids = {(n["type"], n["id"]) for n in d["nodes"]}
    assert all(
        (e["source"]["type"], e["source"]["id"]) in ids and (e["target"]["type"], e["target"]["id"]) in ids
        for e in d["edges"]
    )
    small = get(client, "/network/summary", max_edges=3).json()
    assert len(small["edges"]) == 3 and small["truncated"] is True
    assert get(client, "/network/summary", max_edges=0).status_code == 422


# ---- observations ----
def test_observations_listing_and_filters(client, seeded):
    page = get(client, "/observations", limit=5).json()
    assert page["total"] == 12 and len(page["items"]) == 5
    o = page["items"][0]
    assert o["unit"] and o["observed_at"].endswith(("Z", "+00:00")) and o["provenance"] == "SYNTHETIC_DEMO"
    assert get(client, "/observations", metric_name="inventory_on_hand").json()["total"] == 3
    sid = get(client, "/suppliers", q="DEMO-SUP-001").json()["items"][0]["id"]
    assert get(client, "/observations", supplier_id=sid).json()["total"] == 2
    assert get(client, "/observations", supplier_id=0).status_code == 422


def test_observations_empty(client):
    assert get(client, "/observations").json()["total"] == 0


# ---- cross-cutting ----
def test_request_id_generated_echoed_and_sanitized(client):
    assert len(get(client, "/health").headers["x-request-id"]) == 32
    r = client.get(f"{API}/health", headers={"X-Request-ID": "trace-abc.123"})
    assert r.headers["x-request-id"] == "trace-abc.123"
    r = client.get(f"{API}/health", headers={"X-Request-ID": "bad id\twith spaces"})
    assert r.headers["x-request-id"] != "bad id\twith spaces"


def test_unknown_route_and_method_use_error_envelope(client):
    r = client.get("/nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = client.post(f"{API}/suppliers", json={})
    assert r.status_code == 405 and r.json()["error"]["code"] == "method_not_allowed"


def test_unhandled_error_is_generic_and_hides_internals(settings, engine):
    app = create_app(settings, engine)

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret-token /etc/passwd sqlite:///x.db")

    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/boom")
    assert r.status_code == 500
    assert r.json()["error"] == {
        "code": "internal_error",
        "message": "An internal error occurred.",
        "request_id": r.headers["x-request-id"],
    }
    assert "secret-token" not in r.text and "Traceback" not in r.text


def test_oversized_payload_rejected(settings, engine):
    small = settings.model_copy(update={"max_request_bytes": 1024})
    with TestClient(create_app(small, engine)) as c:
        r = c.post(f"{API}/suppliers", content=b"x" * 5000)
    assert r.status_code == 413 and r.json()["error"]["code"] == "payload_too_large"


def test_cors_allows_configured_origin_only(client):
    ok = client.options(
        f"{API}/suppliers", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"}
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = client.options(
        f"{API}/suppliers", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in bad.headers


def test_production_hides_docs(db_url, tmp_path, engine):
    s = Settings(
        _env_file=None,
        environment="production",
        cors_allowed_origins=["https://x.example"],
        database_url=db_url,
        data_dir=tmp_path,
    )
    with TestClient(create_app(s, engine)) as c:
        assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


def test_openapi_documents_all_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    expected = {
        "/health",
        "/readiness",
        "/dashboard/summary",
        "/suppliers",
        "/suppliers/{supplier_id}",
        "/facilities",
        "/facilities/{facility_id}",
        "/network/summary",
        "/observations",
    }
    assert {f"{API}{p}" for p in expected} <= set(paths)
    assert all(op.get("summary") for item in paths.values() for op in item.values())


def test_read_sessions_roll_back_and_close_on_error(client, engine, seeded):
    # connection pool must still serve requests after an error path
    assert get(client, "/suppliers/99999").status_code == 404
    assert get(client, "/suppliers").status_code == 200
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM suppliers")).scalar() == 7
