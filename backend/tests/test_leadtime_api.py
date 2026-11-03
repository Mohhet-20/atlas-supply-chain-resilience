"""Lead-time API: contract, roles, write gate, idempotency, pagination, bounds, end-to-end synthetic flow (no tenancy)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.leadtime_helpers import (
    LT,
    TABLE,
    code_map,
    csv_bytes,
    gen_rows,
    h,
    load_world_deliveries,
    remap,
    special_rows,
    world,
)
from tests.registry_helpers import make_settings


@pytest.fixture
def secured(settings, engine):
    with TestClient(create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False), engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    with TestClient(
        create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, write_api_enabled=False), engine)
    ) as c:
        yield c


def _ids(w):
    return w["P1"]["id"], w["F1"]["id"], w["S1"]["id"]


def test_reference_endpoints(client):
    lim = client.get(f"{LT}/limits").json()
    assert lim["algorithm_version"] == "lt-1" and "tenancy" in lim and "ORDER_TO_RECEIPT" in lim["definitions"]
    m = client.get(f"{LT}/models").json()
    assert {x["key"] for x in m["lead_time"]} >= {"global_median", "ridge_leadtime"} and {x["key"] for x in m["late_risk"]} >= {"base_rate", "logistic_late"}  # fmt: skip
    ok = client.post(f"{LT}/config/validate", json={"target": "LATE_RISK"}).json()
    assert ok["valid"] and len(ok["config_hash"]) == 64
    assert client.post(f"{LT}/config/validate", json={"target": "LATE_RISK", "models": ["lstm"]}).status_code == 422
    assert client.post(f"{LT}/config/validate", json={"organization_id": 1}).status_code == 422


def test_import_preview_commit_and_rows(client):
    w = world(client)
    rows = remap(gen_rows(30) + list(special_rows().values()), code_map(w))
    body = csv_bytes(rows)
    hdr = {"Content-Type": "text/csv"}
    pv = client.post(f"{LT}/imports?kind=DELIVERIES&mode=preview", content=body, headers=hdr).json()
    assert pv["committed"] is False and pv["would_write"] > 0
    assert client.get(f"{LT}/deliveries").json()["total"] == 0  # preview wrote nothing
    c = client.post(
        f"{LT}/imports?kind=DELIVERIES&mode=commit&source_label=synthetic&provenance=SYNTHETIC_DEMO",
        content=body,
        headers=hdr,
    ).json()
    assert c["committed"] and c["created"]
    again = client.post(
        f"{LT}/imports?kind=DELIVERIES&mode=commit&source_label=synthetic&provenance=SYNTHETIC_DEMO",
        content=body,
        headers=hdr,
    ).json()
    assert again["replayed_existing_batch"] is True
    bid = c["batch"]["id"]
    assert client.get(f"{LT}/imports/{bid}").json()["rows_accepted"] > 0
    assert client.get(f"{LT}/imports/{bid}/rows?limit=5").json()["limit"] == 5
    assert client.get(f"{LT}/imports?kind=DELIVERIES").json()["total"] >= 1
    assert client.post(f"{LT}/imports?kind=DELIVERIES&mode=commit", content=b"", headers=hdr).status_code == 422
    assert (
        client.post(
            f"{LT}/imports?kind=DELIVERIES&mode=preview&time_zone=Mars/Base", content=body, headers=hdr
        ).status_code
        == 422
    )


def test_deliveries_filters_pagination_and_detail(client):
    w, cm, _ = load_world_deliveries(client, n=60)
    pid, fid, sid = _ids(w)
    page = client.get(f"{LT}/deliveries?limit=10&offset=5").json()
    assert len(page["items"]) == 10 and page["total"] >= 60 and page["offset"] == 5
    only = client.get(f"{LT}/deliveries?supplier_id={sid}&state=IN_TRANSIT").json()
    assert only["total"] == 1 and only["items"][0]["delivery_ref"] == "PO-IT"
    assert client.get(f"{LT}/deliveries?q=PO-OPEN").json()["total"] == 1
    d = client.get(f"{LT}/deliveries/PO-REV?product_id={pid}").json()
    assert d["delivery_ref"] == "PO-REV" and d["original_promised_at"] != d["latest_promised_at"]
    assert client.get(f"{LT}/deliveries/NOPE?product_id={pid}").status_code == 404
    assert client.get(f"{LT}/deliveries?limit=1000").status_code == 422
    assert client.get(f"{LT}/deliveries?as_of=2020-01-01T00:00:00").status_code == 422  # naive as_of


def test_analytics_endpoints(client):
    w, cm, _ = load_world_deliveries(client, n=200)
    s = client.get(f"{LT}/analytics/summary").json()
    assert s["lead_time"]["n"] > 150 and "punctuality" in s and s["counts"]["censored"] == 2
    g = client.get(f"{LT}/analytics/groups?by=supplier").json()
    assert g["groups"] if "groups" in g else g
    assert client.get(f"{LT}/analytics/groups?by=bogus").status_code == 422
    assert client.get(f"{LT}/analytics/summary?unit=BUSINESS_DAYS&timezone=Asia/Kolkata").status_code == 200
    assert client.get(f"{LT}/analytics/summary?unit=FORTNIGHTS").status_code == 422
    assert client.get(f"{LT}/analytics/trend?window_days=60").status_code == 200
    q = client.get(f"{LT}/quality").json()
    assert q["deliveries"] > 150 and "findings" in q


def test_run_assessment_and_reliability_flow(client):
    w, cm, _ = load_world_deliveries(client, n=300)
    pid, fid, sid = _ids(w)
    key = {"Idempotency-Key": "lt-run-0001"}
    r = client.post(f"{LT}/runs", json={"target": "LEAD_TIME"}, headers=key)
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    again = client.post(f"{LT}/runs", json={"target": "LEAD_TIME"}, headers=key)
    assert again.headers.get("Idempotent-Replayed") == "true" and again.json()["id"] == rid
    assert client.post(f"{LT}/runs", json={"target": "LATE_RISK"}, headers=key).status_code == 409
    run = client.get(f"{LT}/runs/{rid}").json()
    assert run["status"] in ("COMPLETED", "PARTIAL"), run["error"]
    for part in ("metrics", "splits", "intervals", "explanation", "failures"):
        assert client.get(f"{LT}/runs/{rid}/{part}").status_code == 200, part
    assert client.get(f"{LT}/runs/{rid}/metrics").json()["selection"]
    assert client.get(f"{LT}/runs?target=LEAD_TIME").json()["total"] == 1
    a = client.post(f"{LT}/assessments", json={"delivery_ref": "PO-OPEN", "product_id": pid, "leadtime_run_id": rid}, headers={"Idempotency-Key": "lt-assess-01"})  # fmt: skip
    assert a.status_code == 201, a.text
    ab = a.json()
    assert ab["status"] == "ASSESSED" and "supplier confirmation" in ab["statement"].lower() or ab["statement"]
    rep = client.post(f"{LT}/assessments", json={"delivery_ref": "PO-OPEN", "product_id": pid, "leadtime_run_id": rid}, headers={"Idempotency-Key": "lt-assess-01"})  # fmt: skip
    assert rep.status_code == 200 and rep.json()["id"] == ab["id"]
    assert client.get(f"{LT}/assessments/{ab['id']}").json()["factors"] is not None
    assert client.get(f"{LT}/assessments/{ab['id']}/outcome").status_code == 200
    assert client.get(f"{LT}/assessments?delivery_ref=PO-OPEN").json()["total"] == 1
    cancelled = client.post(f"{LT}/assessments", json={"delivery_ref": "PO-CAN", "product_id": pid}).json()
    assert cancelled["status"] == "UNAVAILABLE" and cancelled["unavailable_reason"]
    assert client.post(f"{LT}/assessments", json={"delivery_ref": "NOPE", "product_id": pid}).status_code == 404
    assert (
        client.post(f"{LT}/assessments", json={"delivery_ref": "PO-OPEN", "product_id": pid, "tenant": "x"}).status_code
        == 422
    )
    rel = client.get(f"{LT}/suppliers/{sid}/reliability").json()
    assert rel["summary"]["lead_time"]["n"] > 50 and "separate" in rel["note"]
    assert client.get(f"{LT}/runs/999999").status_code == 404


def test_run_validation(client):
    assert client.post(f"{LT}/runs", json={"target": "LEAD_TIME"}).status_code == 422  # nothing to train on
    assert client.post(f"{LT}/runs", json={"target": "NOPE"}).status_code == 422
    assert client.post(f"{LT}/runs", json={"target": "LEAD_TIME", "train_fraction": 0.99}).status_code == 422


def test_roles_and_write_gate(client, secured, readonly):
    load_world_deliveries(client, n=120)
    assert secured.get(f"{LT}/limits").status_code == 401
    assert secured.get(f"{LT}/deliveries", headers=h("viewer")).status_code == 200
    assert secured.post(f"{LT}/runs", json={"target": "LEAD_TIME"}, headers=h("viewer")).status_code == 403
    assert (
        secured.post(f"{LT}/imports?kind=DELIVERIES&mode=commit", content=b"x", headers=h("viewer")).status_code == 403
    )
    rid = secured.post(f"{LT}/runs", json={"target": "LEAD_TIME"}, headers=h("editor")).json()["id"]
    assert secured.post(f"{LT}/runs/{rid}/cancel", headers=h("viewer")).status_code == 403
    assert secured.post(f"{LT}/runs/{rid}/cancel", headers=h("editor")).status_code == 409  # already finished
    assert (
        secured.post(
            f"{LT}/assessments", json={"delivery_ref": "PO-OPEN", "product_id": 1}, headers=h("viewer")
        ).status_code
        == 403
    )
    assert readonly.get(f"{LT}/runs", headers=h("viewer")).status_code == 200
    blocked = readonly.post(f"{LT}/runs", json={"target": "LEAD_TIME"}, headers=h("editor"))
    assert blocked.status_code == 403 and "disabled" in blocked.text
    assert readonly.post(
        f"{LT}/imports?kind=DELIVERIES&mode=commit", content=b"x", headers=h("editor")
    ).status_code in (403, 422)
    assert (
        readonly.post(
            f"{LT}/imports?kind=DELIVERIES&mode=preview",
            content=b"a,b\n1,2\n",
            headers={**h("editor"), "Content-Type": "text/csv"},
        ).status_code
        != 403
    )  # preview is a read; bad columns give 422
