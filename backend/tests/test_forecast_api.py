"""Forecast API: contract, roles, write gate, idempotency, bounds and end-to-end synthetic flow (no tenancy exists)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.forecast_helpers import FC, TABLE, h, pid, synthetic_world
from tests.registry_helpers import make_settings


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False)
    with TestClient(create_app(s, engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, write_api_enabled=False)
    with TestClient(create_app(s, engine)) as c:
        yield c


def _body(client, w, p="p1", f="f1", **cfg):
    return {
        "product_id": pid(client, w, p),
        "facility_id": pid(client, w, f),
        "config": {"horizon": 14, "n_folds": 5, **cfg},
    }


def test_reference_endpoints(client):
    lim = client.get(f"{FC}/limits").json()
    assert (
        lim["max_horizon_daily"] == 90
        and "no tenancy" in lim["organisation_model"]
        and set(lim["models"]) >= {"naive", "croston_sba"}
    )
    ms = client.get(f"{FC}/models").json()["items"]
    assert {m["key"] for m in ms} == set(lim["models"]) and all(m["version"] and m["interval_support"] for m in ms)
    ok = client.post(f"{FC}/config/validate", json={"config": {"horizon": 14}}).json()
    assert ok["valid"] and ok["config_digest"]
    bad = client.post(f"{FC}/config/validate", json={"config": {"horizon": 5000, "bogus": 1}}).json()
    assert not bad["valid"]
    over = client.post(f"{FC}/config/validate", json={"config": {"horizon": 120}}).json()
    assert not over["valid"] and "limit" in over["errors"][0]["message"]
    assert client.post(f"{FC}/config/validate", json={"config": {"candidates": ["lstm"]}}).json()["valid"] is False


def test_series_diagnostics_patterns_and_preview(client):
    w = synthetic_world(client)
    s = client.get(f"{FC}/series").json()
    assert s["total"] == 6 and all(i["target"] == "demand" and i["observations"] > 0 for i in s["items"])
    page = client.get(f"{FC}/series", params={"limit": 2, "offset": 2}).json()
    assert len(page["items"]) == 2 and page["total"] == 6
    d = client.get(
        f"{FC}/series/diagnostics", params={"product_id": pid(client, w, "p2"), "facility_id": pid(client, w, "f1")}
    ).json()
    assert d["preparation"]["status"] == "ok" and "seasonal" in d["patterns"]["labels"] and d["patterns"]["criteria"]
    assert d["diagnostics"]["seasonality"]["acf"] > 0.3 and d["points"][0]["observed"] is True
    short = client.get(
        f"{FC}/series/diagnostics", params={"product_id": pid(client, w, "p3"), "facility_id": pid(client, w, "f2")}
    ).json()
    assert short["diagnostics"] is None and short["patterns"]["labels"] == ["insufficient_history"]
    pt = client.get(
        f"{FC}/series/patterns", params={"product_id": pid(client, w, "p2"), "facility_id": pid(client, w, "f2")}
    ).json()
    assert "intermittent" in pt["patterns"]["labels"]
    pv = client.post(f"{FC}/candidates/preview", json=_body(client, w, "p1", "f1")).json()
    assert "naive" in pv["eligible"] and pv["ineligible"]["croston_sba"] and pv["validation_plan"]["folds"] >= 3
    assert client.get(f"{FC}/series/diagnostics", params={"product_id": 99999, "facility_id": 1}).status_code == 404


def test_run_flow_values_intervals_metrics_rationale(client):
    w = synthetic_world(client)
    r = client.post(
        f"{FC}/runs", json=_body(client, w, interval_levels=[0.8]), headers={"Idempotency-Key": "key-abcdefgh"}
    )
    assert r.status_code == 202
    rid = r.json()["id"]
    again = client.post(
        f"{FC}/runs", json=_body(client, w, interval_levels=[0.8]), headers={"Idempotency-Key": "key-abcdefgh"}
    )
    assert again.status_code == 202 and again.json()["id"] == rid and again.headers["Idempotent-Replayed"] == "true"
    clash = client.post(f"{FC}/runs", json=_body(client, w, horizon=7), headers={"Idempotency-Key": "key-abcdefgh"})
    assert clash.status_code == 409
    run = client.get(f"{FC}/runs/{rid}").json()
    assert (
        run["status"] in ("COMPLETED", "PARTIAL")
        and run["provenance"] == "MODEL_ESTIMATE"
        and run["reproducibility"]["deterministic"]
    )
    v = client.get(f"{FC}/runs/{rid}/values").json()
    assert (
        v["kind"] == "forecast"
        and len(v["items"]) == 14
        and v["items"][0]["unit"] == "EA"
        and "not an observation" in v["disclaimer"]
    )
    assert all(i["kind"] == "forecast" and i["model_version"] and i["origin"] for i in v["items"])
    iv = client.get(f"{FC}/runs/{rid}/intervals").json()
    assert (
        iv["calibration"][0]["level"] == 0.8 and "assumptions" in iv and "coverage_evaluation" in iv["calibration"][0]
    )
    m = client.get(f"{FC}/runs/{rid}/metrics").json()
    assert m["formulas"]["wape"]["undefined_when"] and run["selected_model"] in m["models"]
    ra = client.get(f"{FC}/runs/{rid}/rationale").json()
    assert ra["selection"]["rationale"] and ra["validation"]["leakage_controls"]
    bt_ = client.get(f"{FC}/runs/{rid}/backtests").json()
    assert bt_["validation"]["fold_train_lengths"]
    assert client.get(f"{FC}/runs/{rid}/failures").json()["status"] in ("COMPLETED", "PARTIAL")
    el = client.get(f"{FC}/runs/{rid}/inventory-eligibility").json()
    assert el["selectable"] and el["unit"] == "EA"
    lst = client.get(f"{FC}/runs", params={"product_id": pid(client, w, "p1"), "limit": 5}).json()
    assert lst["total"] == 1 and lst["items"][0]["id"] == rid
    assert client.get(f"{FC}/runs", params={"status": "FAILED"}).json()["total"] == 0
    other = client.post(f"{FC}/runs", json=_body(client, w, "p2", "f1")).json()["id"]
    cmp = client.get(f"{FC}/runs/compare", params={"ids": f"{rid},{other}"}).json()
    assert len(cmp["items"]) == 2 and cmp["items"][0]["backtest_overall"]["mae"] is not None
    assert client.get(f"{FC}/runs/compare", params={"ids": "1,2,3,4,5"}).status_code == 422


def test_declined_run_values_and_eligibility(client):
    w = synthetic_world(client)
    rid = client.post(f"{FC}/runs", json=_body(client, w, "p3", "f2")).json()["id"]
    run = client.get(f"{FC}/runs/{rid}").json()
    assert run["result_status"] == "insufficient_data"
    v = client.get(f"{FC}/runs/{rid}/values").json()
    assert v["items"] == [] and "declined" in v["note"]
    assert client.get(f"{FC}/runs/{rid}/inventory-eligibility").json()["selectable"] is False
    assert client.post(f"{FC}/runs/{rid}/evaluations").status_code == 422


def test_validation_and_unknown_fields_rejected(client):
    w = synthetic_world(client)
    assert client.post(f"{FC}/runs", json={**_body(client, w), "organization_id": 7}).status_code == 422
    assert client.post(f"{FC}/runs", json=_body(client, w, horizon=500)).status_code == 422
    assert client.post(f"{FC}/runs", json={"product_id": 99999, "facility_id": pid(client, w, "f1")}).status_code == 404
    assert client.post(f"{FC}/runs", json=_body(client, w, candidates=["lstm"])).status_code == 422
    assert client.post(f"{FC}/runs", json=_body(client, w, missing_policy="zero_fill_by_contract")).status_code == 422


def test_roles_and_write_gate(client, secured, readonly):
    w = synthetic_world(client)  # data is created through the open client; all three share one engine
    b = _body(client, w)
    assert secured.get(f"{FC}/limits").status_code == 401
    assert secured.post(f"{FC}/runs", json=b, headers=h("viewer")).status_code == 403
    assert secured.get(f"{FC}/runs", headers=h("viewer")).status_code == 200
    rid = secured.post(f"{FC}/runs", json=b, headers=h("editor")).json()["id"]
    assert secured.post(f"{FC}/runs/{rid}/cancel", headers=h("viewer")).status_code == 403
    assert secured.post(f"{FC}/runs/{rid}/cancel", headers=h("editor")).status_code == 409  # already finished
    assert secured.post(f"{FC}/monitoring/check", headers=h("viewer")).status_code == 403
    assert secured.post(f"{FC}/monitoring/check", headers=h("editor")).status_code == 200
    assert secured.post(f"{FC}/runs/{rid}/evaluations", headers=h("viewer")).status_code == 403
    assert readonly.get(f"{FC}/runs", headers=h("viewer")).status_code == 200
    blocked = readonly.post(f"{FC}/runs", json=b, headers=h("editor"))
    assert blocked.status_code == 403 and "disabled" in blocked.text
    assert readonly.post(f"{FC}/monitoring/check", headers=h("editor")).status_code == 403


def test_forecast_vs_actual_endpoints_and_audit(client):
    w = synthetic_world(client)
    from datetime import timedelta

    from tests.forecast_helpers import TODAY

    origin = (TODAY - timedelta(days=20)).isoformat()
    rid = client.post(f"{FC}/runs", json=_body(client, w, origin=origin)).json()["id"]
    fva = client.get(f"{FC}/runs/{rid}/forecast-vs-actual").json()
    assert (
        fva["counts"]["final"] == 13
        and fva["metrics"]["final"]["mae"]["value"] is not None
        and "never changed" in fva["note"]
    )
    before = client.get(f"{FC}/runs/{rid}/values").json()["items"]
    e1 = client.post(f"{FC}/runs/{rid}/evaluations")
    e2 = client.post(f"{FC}/runs/{rid}/evaluations")
    assert e1.status_code == 201 and e2.status_code == 200 and e1.json()["id"] == e2.json()["id"]
    hist = client.get(f"{FC}/runs/{rid}/evaluations").json()
    assert (
        hist["total"] == 1
        and client.get(f"{FC}/performance", params={"product_id": pid(client, w, "p1")}).json()["total"] == 1
    )
    assert client.get(f"{FC}/runs/{rid}/values").json()["items"] == before
    chk = client.post(f"{FC}/monitoring/check").json()
    assert "never retrains" in chk["note"]
    assert client.get(f"{FC}/alerts").json()["total"] == chk["count"]
    assert client.get(f"{FC}/alerts", params={"kind": "stale_forecast"}).status_code == 200


def test_inventory_analysis_accepts_forecast_run_over_api(client):
    w = synthetic_world(client)
    rid = client.post(f"{FC}/runs", json=_body(client, w, horizon=28)).json()["id"]
    a = client.post("/api/v1/inventory/analyses", json={"config": {"forecast_run_id": rid, "horizon_days": 28}})
    assert a.status_code == 202
    aid = a.json()["id"]
    fs = client.get(f"/api/v1/inventory/analyses/{aid}/findings").json()["items"]
    pj = next(f for f in fs if f["product_code"] == w["p1"]["code"] and f["facility_code"] == w["f1"]["code"])
    detail = client.get(f"/api/v1/inventory/analyses/{aid}/findings/{pj['id']}").json()
    assert detail["projection"]["demand_source"]["forecast_run_id"] == rid
    bad = client.post("/api/v1/inventory/analyses", json={"config": {"forecast_run_id": 999999}})
    assert bad.status_code == 404
