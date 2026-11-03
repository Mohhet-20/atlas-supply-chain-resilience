from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.facility import Facility
from app.models.inventory import InventoryPosition
from app.models.product import Product
from tests.inventory_helpers import (
    INV,
    TABLE,
    code,
    daily_demand,
    h,
    iso,
    load_demand,
    load_inventory,
    populate,
    scenario_impact,
    world,
)  # fmt: skip
from tests.registry_helpers import make_settings

HDR = "product_code,facility_code,observed_at,unit,on_hand,reserved\n"


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False)
    with TestClient(create_app(s, engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    s = make_settings(
        settings,
        api_tokens=TABLE,
        risk_auto_reassess=False,
        write_api_enabled=False,
        environment="production",
        cors_allowed_origins=["https://x.example"],
    )
    with TestClient(create_app(s, engine)) as c:
        yield c


def up(c, body: bytes, role="editor", **q):
    q = {"kind": "INVENTORY", "format": "CSV", "mode": "commit"} | q
    return c.post(f"{INV}/imports", params=q, content=body, headers=h(role) | {"Content-Type": "text/csv"})


def test_limits_and_roles(secured):
    assert secured.get(f"{INV}/limits").status_code == 401
    r = secured.get(f"{INV}/limits", headers=h("viewer"))
    assert r.status_code == 200 and "INVENTORY" in r.json()["fields"]
    assert up(secured, b"x", role="viewer").status_code == 403


def test_preview_writes_nothing_and_commit_is_idempotent(client):
    w = world(client)
    body = (HDR + f"{code(w, 'p1')},{code(w, 'f1')},{iso(1)},EA,100,10\n").encode()
    r = up(client, body, mode="preview")
    assert r.status_code == 200 and r.json()["committed"] is False and r.json()["would_write"] == 1
    assert client.get(f"{INV}/positions").json()["total"] == 0
    c1 = up(client, body).json()
    assert c1["created"] is True and c1["batch"]["rows_accepted"] == 1
    c2 = up(client, body).json()
    assert c2["created"] is False and c2["replayed_existing_batch"] is True and c2["batch"]["id"] == c1["batch"]["id"]
    assert client.get(f"{INV}/positions").json()["total"] == 1


def test_row_errors_are_reported_with_codes(client):
    w = world(client)
    p, f = code(w, "p1"), code(w, "f1")
    rows = [
        f"{p},{f},{iso(1)},EA,-5,0",  # negative
        f"{p},{f},{iso(2)},EA,50,60",  # reserved > on_hand
        f"NOPE,{f},{iso(1)},EA,5,0",  # unknown product
        f"{p},{f},{iso(-30)},EA,5,0",  # future
        f"{p},{f},{iso(3)},KG,5,0",  # unit mismatch with registry
        f"{p},{f},{iso(4)},EA,abc,0",  # not a number
        f"{p},{f},{iso(5)},EA,20,0",  # ok
    ]
    r = up(client, (HDR + "\n".join(rows) + "\n").encode()).json()
    assert r["batch"]["rows_accepted"] == 1 and r["batch"]["rows_rejected"] == 6
    bid = r["batch"]["id"]
    got = client.get(f"{INV}/imports/{bid}/rows", params={"outcome": "REJECTED"}).json()
    assert got["total"] == 6
    codes = {e["code"] for row in got["items"] for e in row["errors"]}
    assert len(codes) >= 5, codes
    assert client.get(f"{INV}/positions").json()["total"] == 1


def test_atomic_import_writes_nothing_on_any_error(client):
    w = world(client)
    p, f = code(w, "p1"), code(w, "f1")
    r = up(client, (HDR + f"{p},{f},{iso(1)},EA,5,0\n{p},{f},{iso(2)},EA,-1,0\n").encode(), atomic=True).json()
    assert r["batch"]["rows_accepted"] == 0 and r["committed"] is False
    assert client.get(f"{INV}/positions").json()["total"] == 0


def test_registry_never_modified_by_import(client):
    w = world(client)
    with Session(client.app.state.engine) as s:
        n = (s.scalar(select(func.count()).select_from(Product)), s.scalar(select(func.count()).select_from(Facility)))
    up(client, (HDR + f"UNKNOWN-CODE,{code(w, 'f1')},{iso(1)},EA,5,0\n").encode())
    with Session(client.app.state.engine) as s:
        assert n == (
            s.scalar(select(func.count()).select_from(Product)),
            s.scalar(select(func.count()).select_from(Facility)),
        )


def test_import_guards(client, secured):
    assert up(client, b"", mode="preview").status_code == 422
    assert client.post(f"{INV}/imports", params={"kind": "INVENTORY"}, content=b"a,b\n1,2\n").status_code == 415
    assert up(client, b"a,b\n1,2\n", kind="BOGUS").status_code == 422
    assert up(client, b"a,b\n1,2\n", mapping="{bad").status_code == 422
    assert up(client, b"a,b\n1,2\n", date_format="%Q").status_code == 422
    assert up(client, b"a,b\n1,2\n", mode="preview").status_code == 422  # required fields have no column


def test_oversized_upload_and_too_many_rows(settings, engine):
    s = make_settings(
        settings, api_tokens=TABLE, risk_auto_reassess=False, max_upload_bytes=2000, inventory_max_import_rows=10
    )
    with TestClient(create_app(s, engine)) as c:
        assert up(c, b"x" * 5000).status_code == 413
        rows = "".join(f"P{i},F,{iso(1)},EA,5,0\n" for i in range(11))
        assert up(c, (HDR + rows).encode()).status_code == 422


def test_write_disabled(readonly):
    w_body = (HDR + "P,F,2026-01-01,EA,5,0\n").encode()
    assert up(readonly, w_body).status_code == 403
    assert readonly.post(f"{INV}/analyses", json={}, headers=h("editor")).status_code == 403
    assert readonly.post(f"{INV}/policies", json={}, headers=h("editor")).status_code in (403, 422)
    assert readonly.get(f"{INV}/limits", headers=h("viewer")).status_code == 200


def test_history_snapshots_and_derived_quantities(client):
    w = world(client)
    p, f = code(w, "p1"), code(w, "f1")
    load_inventory(
        client, [[p, f, iso(3), "EA", 100, 10, "", 5, "", "", ""], [p, f, iso(1), "EA", 80, "", "", "", "", "", ""]]
    )
    pid, fid = w["p1"]["id"], w["f1"]["id"]
    h1 = client.get(f"{INV}/history", params={"product_id": pid, "facility_id": fid}).json()
    assert h1["total"] == 2 and h1["items"][0]["on_hand"] == 100
    assert client.get(f"{INV}/positions").json()["total"] == 1
    assert client.get(f"{INV}/positions", params={"latest": "false"}).json()["total"] == 2
    d1 = client.get(f"{INV}/positions/{h1['items'][0]['id']}").json()
    assert d1["snapshot"]["usable_source"] is None and d1["derived"]["usable"] == 85
    assert client.get(f"{INV}/positions/999999").status_code == 404


def test_demand_rate_endpoint_and_gaps_are_not_zero(client):
    w = world(client)
    load_demand(client, daily_demand(w, "p1", "f1", 10, days=10))
    pid, fid = w["p1"]["id"], w["f1"]["id"]
    r = client.get(f"{INV}/demand/rate", params={"product_id": pid, "facility_id": fid}).json()
    assert r["status"] in ("insufficient_demand_history", "calculable")
    assert r["rate_per_day"] in (None, 10.0) and r["missing_days"] == 20
    assert client.get(f"{INV}/demand/rate", params={"product_id": 99999, "facility_id": fid}).status_code == 404
    assert client.get(f"{INV}/demand").json()["total"] == 10


def test_policy_lifecycle(client):
    w = world(client)
    pid, fid = w["p1"]["id"], w["f1"]["id"]
    body = {
        "product_id": pid,
        "facility_id": fid,
        "unit": "EA",
        "safety_stock": 20,
        "reorder_point": 60,
        "owner": "planner",
        "lead_time": {"value": 1, "unit": "weeks"},
    }
    r = client.post(f"{INV}/policies", json=body)
    assert r.status_code == 201 and r.json()["lead_time_days"] == 7 and r.json()["version"] == 1
    assert client.post(f"{INV}/policies", json=body).status_code == 409
    bad = {**body, "reorder_point": 5}
    assert client.post(f"{INV}/policies", json={**bad, "product_id": w["p2"]["id"]}).status_code == 422
    assert client.post(f"{INV}/policies", json={**body, "unit": "KG", "product_id": w["p2"]["id"]}).status_code == 422
    assert (
        client.post(f"{INV}/policies", json={**body, "organisation_id": 7, "product_id": w["p2"]["id"]}).status_code
        == 422
    )
    assert client.post(f"{INV}/policies", json={**body, "product_id": 99999}).status_code == 404
    rev = client.post(
        f"{INV}/policies/{r.json()['id']}/revise",
        json={k: v for k, v in body.items() if k not in ("product_id", "facility_id")}
        | {"safety_stock": 30, "reorder_point": 80},
    )
    assert rev.status_code == 201 and rev.json()["version"] == 2
    allp = client.get(f"{INV}/policies", params={"product_id": pid, "include_superseded": "true"}).json()
    assert [x["status"] for x in allp["items"]] == ["ACTIVE", "SUPERSEDED"]
    assert client.get(f"{INV}/policies", params={"product_id": pid}).json()["total"] == 1
    assert client.post(f"{INV}/policies/{r.json()['id']}/revise", json={"unit": "EA", "owner": "x"}).status_code == 409
    assert client.post(f"{INV}/policies/{rev.json()['id']}/retire").status_code == 200
    assert client.get(f"{INV}/policies", params={"product_id": pid}).json()["total"] == 0


def test_missing_policies_listed(client):
    w = world(client)
    populate(client, w)
    m = client.get(f"{INV}/policies/missing").json()
    assert m["total"] == 3 and all(i["product_name"] for i in m["items"])


def test_analysis_flow_and_idempotency(client):
    w = world(client)
    populate(client, w)
    r = client.post(
        f"{INV}/analyses", json={"config": {"horizon_days": 45}}, headers={"Idempotency-Key": "key-12345678"}
    )
    assert r.status_code == 202, r.text
    aid = r.json()["id"]
    again = client.post(
        f"{INV}/analyses", json={"config": {"horizon_days": 45}}, headers={"Idempotency-Key": "key-12345678"}
    )
    assert again.json()["id"] == aid and again.headers.get("Idempotent-Replayed") == "true"
    a = client.get(f"{INV}/analyses/{aid}").json()
    assert a["status"] == "COMPLETED" and a["input_hash"] and a["inputs"]["pairs"] == 3
    s = client.get(f"{INV}/analyses/{aid}/summary").json()
    assert s["summary"]["pairs_analysed"] == 3 and "Decision support" in s["disclaimer"]
    fl = client.get(f"{INV}/analyses/{aid}/findings").json()
    assert fl["total"] == 3 and fl["items"] == sorted(fl["items"], key=lambda x: -x["severity_rank"])
    assert client.get(f"{INV}/analyses/{aid}/findings", params={"status": "projected_stockout"}).json()["total"] == 1
    fid = next(i["id"] for i in fl["items"] if i["status"] == "projected_stockout")
    det = client.get(f"{INV}/analyses/{aid}/findings/{fid}").json()
    assert (
        det["projection"]["series"] and det["assumptions"] and det["evidence"] and det["quantities"]["formula"]
        if "formula" in det["quantities"]
        else True
    )
    assert client.get(f"{INV}/analyses/{aid}/findings/999999").status_code == 404
    assert client.post(f"{INV}/analyses", json={"config": {"horizon_days": 9999}}).status_code == 422
    assert client.post(f"{INV}/analyses", json={"organization_id": 1}).status_code == 422
    assert client.post(f"{INV}/analyses", json={"impact_analysis_id": 999}).status_code == 404
    assert client.post(f"{INV}/analyses/{aid}/cancel").status_code == 409
    assert client.get(f"{INV}/analyses", params={"kind": "BASELINE"}).json()["total"] >= 1


def test_scenario_compare_and_canonical_data_unchanged(client):
    w = world(client)
    populate(client, w)
    iid = scenario_impact(client, w)
    with Session(client.app.state.engine) as s:
        before = s.scalar(select(func.count()).select_from(InventoryPosition))
    base = client.post(f"{INV}/analyses", json={}).json()["id"]
    sc = client.post(f"{INV}/analyses", json={"impact_analysis_id": iid}).json()
    assert sc["kind"] == "SCENARIO" and sc["impact_analysis_id"] == iid
    cmp = client.get(f"{INV}/analyses/compare", params={"baseline_id": base, "scenario_id": sc["id"]}).json()
    assert cmp["changed"] == 1 and cmp["not_assessable"] == 1 and cmp["items"][0]["change"] == "EARLIER_CROSSING"
    assert cmp["items"][0]["baseline_stockout_day"] > cmp["items"][0]["scenario_stockout_day"]
    assert (
        client.get(f"{INV}/analyses/compare", params={"baseline_id": sc["id"], "scenario_id": base}).json()[
            "comparable"
        ]
        is False
    )
    with Session(client.app.state.engine) as s:
        assert before == s.scalar(select(func.count()).select_from(InventoryPosition))


def test_results_unavailable_until_finished(client):
    assert client.get(f"{INV}/analyses/999/findings").status_code == 404
    assert client.get(f"{INV}/analyses/0").status_code == 422
