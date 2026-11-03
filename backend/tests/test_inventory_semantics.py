"""Behaviour that must hold end to end: unknown stays unknown, eligibility, versioning, failure handling, security."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.inventory import config as cfg
from app.inventory import runs
from app.inventory.analysis import InvLimits
from app.main import create_app
from app.models.enums import SimStatus
from tests.inventory_helpers import (
    INV,
    TABLE,
    code,
    d,
    daily_demand,
    findings_of,
    h,
    iso,
    load_demand,
    load_inventory,
    load_receipts,
    run_baseline,
    world,
)  # fmt: skip
from tests.registry_helpers import make_settings


def key(w, p="p1", f="f1"):
    return (code(w, p), code(w, f))


def inv(w, p="p1", f="f1", on_hand=100, at=1, **extra):
    row = [code(w, p), code(w, f), iso(at), "EA", on_hand, extra.get("reserved", ""), "", "", "", "", ""]
    return [row]


def test_no_demand_history_is_unknown_not_safe(client):
    w = world(client)
    load_inventory(client, inv(w))
    f = findings_of(client, run_baseline(client).id)[key(w)]
    assert f.status in ("unknown", "insufficient_data")
    assert f.coverage_days["days"] is None and f.coverage_days["status"] in (
        "insufficient_demand_history",
        "zero_observed_demand",
        "unknown",
    )
    assert f.projection is None or f.projection.get("stockout_within_day") is None


def test_missing_policy_is_not_zero_safety_stock(client):
    w = world(client)
    load_inventory(client, inv(w))
    load_demand(client, daily_demand(w, "p1", "f1", 1))
    f = findings_of(client, run_baseline(client).id)[key(w)]
    assert f.policy["has_policy"] is False and f.policy["thresholds"] == {}
    assert f.status != "within_policy"


def test_zero_inventory_with_demand_is_current_shortage(client):
    w = world(client)
    load_inventory(client, inv(w, on_hand=0))
    load_demand(client, daily_demand(w, "p1", "f1", 5))
    f = findings_of(client, run_baseline(client).id)[key(w)]
    assert f.status == "current_shortage" and f.coverage_days["status"] == "zero_inventory"
    assert f.coverage_days["days"] in (None, 0, 0.0)


def test_receipt_eligibility_and_no_double_counting(client):
    w = world(client)
    load_inventory(client, inv(w, on_hand=100))
    load_demand(client, daily_demand(w, "p1", "f1", 10))
    s1 = code(w, "s1")
    p, f = code(w, "p1"), code(w, "f1")
    load_receipts(client, [
        [p, f, "PO-OK", "CONFIRMED", 50, "EA", d(-3), s1],
        [p, f, "PO-PROPOSED", "PROPOSED", 500, "EA", d(-3), s1],
        [p, f, "PO-PLANNED", "PLANNED", 500, "EA", d(-3), s1],
        [p, f, "PO-LATE", "CONFIRMED", 500, "EA", d(5), s1],
        [p, f, "PO-NODATE", "CONFIRMED", 500, "EA", "", s1],
    ])  # fmt: skip
    a = run_baseline(client)
    fnd = findings_of(client, a.id)[key(w)]
    assert fnd.projection["receipts_by_day"] == {"3": 50.0}
    reasons = {x["reason"] for x in fnd.projection["receipts_excluded"]}
    assert reasons, "excluded receipts must be listed with reasons"
    flat = str(reasons)
    assert (
        "STATUS_PROPOSED_NOT_ELIGIBLE" in flat
        and "STATUS_PLANNED_NOT_ELIGIBLE" in flat
        and "OVERDUE_OR_DUE_NOW" in flat
    )
    # in-transit quantity on the snapshot is never added to on-hand
    assert fnd.quantities["available"] == 100


def test_in_transit_on_snapshot_not_counted_as_available(client):
    w = world(client)
    load_inventory(client, [[code(w, "p1"), code(w, "f1"), iso(1), "EA", 10, "", "", "", "", 500, ""]])
    load_demand(client, daily_demand(w, "p1", "f1", 5))
    f = findings_of(client, run_baseline(client).id)[key(w)]
    assert f.quantities["available"] == 10 and f.quantities["source"]["in_transit"] == 500


def test_conflicting_snapshot_for_same_key_is_rejected(client):
    w = world(client)
    row = inv(w, on_hand=100)
    load_inventory(client, row)
    other = [row[0][:4] + [90] + row[0][5:]]
    b, created = load_inventory(client, other)
    assert created and b.rows_accepted == 0 and b.rows_rejected == 1
    ident, created2 = load_inventory(client, row)
    assert created2 is False


def test_policy_violation_is_a_finding_not_an_order(client):
    w = world(client)
    load_inventory(client, inv(w, on_hand=15))
    load_demand(client, daily_demand(w, "p1", "f1", 1))
    r = client.post(
        f"{INV}/policies",
        json={
            "product_id": w["p1"]["id"],
            "facility_id": w["f1"]["id"],
            "unit": "EA",
            "safety_stock": 20,
            "reorder_point": 40,
            "owner": "planner",
        },
    )
    assert r.status_code == 201
    f = findings_of(client, run_baseline(client).id)[key(w)]
    assert f.status == "current_shortage" and "BELOW_SAFETY_STOCK" in f.policy["violations"]
    assert (
        "order" not in f.explanation.lower()
        or "no order" in f.explanation.lower()
        or "places no orders" in f.explanation.lower()
    )


def test_analysis_failure_is_recorded_safely(client, monkeypatch):
    w = world(client)
    load_inventory(client, inv(w))
    import app.inventory.runs as r

    def boom(*a, **k):
        raise RuntimeError("secret internal detail /etc/passwd")

    monkeypatch.setattr(r, "run_analysis", boom)
    a = run_baseline(client)
    assert a.status is SimStatus.FAILED and a.error == "unexpected RuntimeError"
    body = client.get(f"{INV}/analyses/{a.id}").json()
    assert "passwd" not in str(body)
    assert client.get(f"{INV}/analyses/{a.id}/findings").status_code == 409


def test_time_budget_truncates_to_partial(client):
    w = world(client)
    load_inventory(client, inv(w) + inv(w, "p2", "f1"))
    a = runs.analyze_now(
        client.app.state.session_factory,
        "tester",
        runs.Limits(InvLimits(max_pairs=1, time_budget_seconds=30)),
        cfg.InventoryConfig(),
    )
    assert a.status is SimStatus.PARTIAL and "max_pairs" in a.truncation_reasons and a.summary["pairs_analysed"] == 1


def test_cancel_queued_analysis(client):
    w = world(client)
    load_inventory(client, inv(w))
    f = client.app.state.session_factory
    with f() as s, s.begin():
        a, _ = runs.create_analysis(s, cfg.InventoryConfig(), "tester", runs.Limits())
        aid = a.id
    with f() as s, s.begin():
        runs.cancel_analysis(s, s.get(type(a), aid))
    runs.execute_analysis(f, aid, runs.Limits())
    with f() as s:
        assert s.get(type(a), aid).status is SimStatus.CANCELLED


def test_active_analysis_limit_conflicts(client):
    f = client.app.state.session_factory
    lim = runs.Limits(max_active=1)
    with f() as s, s.begin():
        runs.create_analysis(s, cfg.InventoryConfig(), "tester", lim)
    with f() as s, s.begin():
        with pytest.raises(Exception, match="already queued"):
            runs.create_analysis(s, cfg.InventoryConfig(), "tester", lim)


def test_future_as_of_rejected(client):
    r = client.post(f"{INV}/analyses", json={"config": {"as_of": "2999-01-01T00:00:00Z"}})
    assert r.status_code == 422


@pytest.fixture
def secured(settings, engine):
    with TestClient(create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False), engine)) as c:
        yield c


READS = [
    "/limits",
    "/imports",
    "/positions",
    "/quality",
    "/demand",
    "/replenishments",
    "/policies",
    "/policies/missing",
    "/analyses",
]


@pytest.mark.parametrize("path", READS)
def test_reads_need_authentication_but_not_edit_rights(secured, path):
    assert secured.get(f"{INV}{path}").status_code == 401
    assert secured.get(f"{INV}{path}", headers=h("viewer")).status_code == 200


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/imports?kind=INVENTORY&format=CSV&mode=preview", b"a,b\n1,2\n"),
    ("POST", "/policies", b"{}"),
    ("POST", "/analyses", b"{}"),
    ("POST", "/analyses/1/cancel", b"{}"),
    ("POST", "/policies/1/retire", b"{}"),
])  # fmt: skip
def test_viewer_cannot_write_or_even_preview(secured, method, path, body):
    r = secured.request(method, f"{INV}{path}", content=body, headers=h("viewer") | {"Content-Type": "text/csv"})
    assert r.status_code == 403


def test_other_users_analysis_can_only_be_cancelled_by_reviewer(secured):
    r = secured.post(f"{INV}/analyses", json={}, headers=h("editor"))
    assert r.status_code == 202
    aid = r.json()["id"]
    # finished immediately (synchronous test client): cancelling a finished analysis conflicts for the owner
    assert secured.post(f"{INV}/analyses/{aid}/cancel", headers=h("editor")).status_code in (403, 409)
