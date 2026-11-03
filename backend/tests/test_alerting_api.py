"""Alerting API: roles, strict parameters, end-to-end lifecycle, inbox privacy, concurrency, audit, validation. Synthetic data; a fake provider replaces the network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.registry import AuditEvent
from tests.alerting_helpers import FakeProvider
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings

A = "/api/v1/alerting"


@pytest.fixture
def secured(settings, engine):
    app = create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False), engine)
    app.state.alert_provider = FakeProvider()
    with TestClient(app) as c:
        yield c


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rule_json(**kw):
    d = {
        "name": "High temp", "detector": "event_threshold", "severity": "HIGH",
        "params": {"metric": "temp", "operator": "gte", "threshold": 50, "unit": "C"},
        "notify": {"channels": ["in_app"], "recipients": ["editor-user"], "min_severity": "LOW"},
    }  # fmt: skip
    return d | kw


def ev(key="k1", value=60):
    return {
        "event_type": "metric.observation", "source": "sensor-feed", "idempotency_key": key, "occurred_at": now_iso(),
        "payload": {"metric": "temp", "value": value, "unit": "C"}, "entities": [{"entity_type": "facility", "entity_id": "F-1"}],
    }  # fmt: skip


FIXED = ev()


def make_alert(c):
    pol = c.post(
        f"{A}/policies",
        json={
            "name": "Std",
            "ack_minutes": 10,
            "resolve_minutes": 120,
            "escalate_after_minutes": 10,
            "targets": ["reviewer-user"],
        },
        headers=h("reviewer"),
    )
    assert pol.status_code == 201, pol.text
    r = c.post(f"{A}/rules", json=rule_json(escalation_policy=pol.json()["policy_uid"]), headers=h("reviewer"))
    assert r.status_code == 201, r.text
    e = c.post(f"{A}/events", json=FIXED, headers=h("editor"))
    assert e.status_code == 201, e.text
    items = c.get(f"{A}/alerts", headers=h("viewer")).json()["items"]
    assert len(items) == 1
    return items[0], r.json()


def test_auth_and_roles(secured):
    c = secured
    assert c.get(f"{A}/health").status_code == 401
    assert c.get(f"{A}/health", headers=h("viewer")).status_code == 200
    assert c.post(f"{A}/events", json=ev(), headers=h("viewer")).status_code == 403
    assert c.post(f"{A}/evaluate", json={}, headers=h("editor")).status_code == 403
    assert c.post(f"{A}/rules", json=rule_json(), headers=h("editor")).status_code == 403
    assert c.get(f"{A}/events", headers=h("editor")).status_code == 403
    assert c.post(f"{A}/events", json=ev(), headers=h("editor")).status_code == 201


def test_strict_parameters(secured):
    c = secured
    for url in (
        f"{A}/alerts?tenant_id=1",
        f"{A}/alerts?organisation=x",
        f"{A}/health?org=1",
        f"{A}/notifications?foo=1",
    ):
        r = c.get(url, headers=h("viewer"))
        assert r.status_code == 422, url
        assert "comes from the credential" in r.text
    assert c.post(f"{A}/events", json=ev() | {"tenant_id": "t"}, headers=h("editor")).status_code == 422
    assert c.get(f"{A}/alerts?limit=0", headers=h("viewer")).status_code == 422
    assert c.get(f"{A}/alerts?limit=101", headers=h("viewer")).status_code == 422
    assert c.get(f"{A}/alerts?sort=bogus", headers=h("viewer")).status_code == 422


def test_health_never_leaks_webhook_urls(settings, engine):
    s = make_settings(
        settings,
        api_tokens=TABLE,
        alerting_webhooks='{"ops":"https://hooks.example.com/secret-path-123"}',
        alerting_webhook_hosts="hooks.example.com",
    )
    with TestClient(create_app(s, engine)) as c:
        r = c.get(f"{A}/health", headers=h("viewer"))
        assert r.status_code == 200
        assert "secret-path-123" not in r.text
        assert r.json()["channels"]["webhook"]["configured"] == ["ops"]
        assert r.json()["channels"]["email"]["available"] is False


def test_lifecycle_end_to_end(secured):
    c = secured
    alert, _ = make_alert(c)
    uid = alert["alert_uid"]
    assert alert["status"] == "open" and alert["basis"] == "observed"
    # inbox privacy
    mine = c.get(f"{A}/notifications", headers=h("editor")).json()
    assert mine["total"] == 1
    assert c.get(f"{A}/notifications", headers=h("viewer")).json()["total"] == 0
    assert c.get(f"{A}/notifications/count", headers=h("editor")).json()["unread"] == 1
    nid = mine["items"][0]["id"]
    assert c.post(f"{A}/notifications/{nid}/read", headers=h("viewer")).status_code == 404
    assert c.post(f"{A}/notifications/{nid}/read", headers=h("editor")).status_code == 200
    assert c.get(f"{A}/notifications/count", headers=h("editor")).json()["unread"] == 0
    # concurrency + validation
    v = alert["version"]
    ok = c.post(f"{A}/alerts/{uid}/acknowledge", json={"expected_version": v}, headers=h("editor"))
    assert ok.status_code == 200 and ok.json()["status"] == "acknowledged"
    assert c.post(f"{A}/alerts/{uid}/acknowledge", json={"expected_version": v}, headers=h("editor")).status_code in (
        409,
        422,
    )
    assert c.post(f"{A}/alerts/{uid}/resolve", json={}, headers=h("editor")).status_code == 422
    assert (
        c.post(f"{A}/alerts/{uid}/notes", json={"text": "checked the sensor"}, headers=h("editor")).status_code == 201
    )
    assert (
        c.post(f"{A}/alerts/{uid}/assign", json={"assignee": "reviewer-user"}, headers=h("editor")).status_code == 200
    )
    assert (
        c.post(f"{A}/alerts/{uid}/resolve", json={"reason": "sensor recalibrated"}, headers=h("editor")).status_code
        == 200
    )
    assert c.post(f"{A}/alerts/{uid}/close", json={"reason": "done"}, headers=h("editor")).status_code == 403
    assert c.post(f"{A}/alerts/{uid}/close", json={"reason": "done"}, headers=h("reviewer")).status_code == 200
    assert c.post(f"{A}/alerts/{uid}/acknowledge", json={}, headers=h("editor")).status_code in (409, 422)
    # detail history; deliveries are reviewer-only
    d = c.get(f"{A}/alerts/{uid}", headers=h("viewer")).json()
    assert [t["to"] for t in d["transitions"]][-3:] == ["acknowledged", "resolved", "closed"]
    assert d["deliveries"] is None and d["can_transition_to"] == []
    assert c.get(f"{A}/alerts/{uid}", headers=h("reviewer")).json()["deliveries"] is not None
    assert c.get(f"{A}/alerts/NOPE-0000", headers=h("viewer")).status_code == 404


def test_audit_trail(secured, session: Session):
    c = secured
    alert, _ = make_alert(c)
    c.post(f"{A}/alerts/{alert['alert_uid']}/acknowledge", json={}, headers=h("editor"))
    actions = {a for (a,) in session.execute(select(AuditEvent.action).where(AuditEvent.action.like("alerting.%")))}
    assert {
        "alerting.policy.created",
        "alerting.rule.created",
        "alerting.event.ingested",
        "alerting.alert.acknowledge",
    } <= actions


def test_event_idempotency_and_validation(secured):
    c = secured
    make_alert(c)
    again = c.post(f"{A}/events", json=FIXED, headers=h("editor"))
    changed = c.post(
        f"{A}/events", json=FIXED | {"payload": {"metric": "temp", "value": 99, "unit": "C"}}, headers=h("editor")
    )
    assert changed.status_code == 409
    assert again.status_code == 200 and again.json()["duplicate"] is True
    assert len(c.get(f"{A}/alerts", headers=h("viewer")).json()["items"]) == 1
    bad = ev("k2") | {"event_type": "nope"}
    assert c.post(f"{A}/events", json=bad, headers=h("editor")).status_code == 422
    big = ev("k3")
    big["payload"]["x"] = "y" * 9000
    assert c.post(f"{A}/events", json=big, headers=h("editor")).status_code in (413, 422)


def test_rules_api(secured):
    c = secured
    v = c.post(f"{A}/rules/validate", json=rule_json(), headers=h("reviewer")).json()
    assert v["valid"] is True
    cap = c.post(
        f"{A}/rules/validate", json=rule_json(detector="capacity_pressure", params={}), headers=h("reviewer")
    ).json()
    assert cap["valid"] is False and cap["problems"]
    assert (
        c.post(f"{A}/rules", json=rule_json(detector="capacity_pressure", params={}), headers=h("reviewer")).status_code
        == 422
    )
    hook = rule_json(notify={"channels": ["webhook"], "recipients": ["ops"], "min_severity": "LOW"})
    assert c.post(f"{A}/rules", json=hook, headers=h("reviewer")).status_code == 422
    r = c.post(f"{A}/rules", json=rule_json(), headers=h("reviewer")).json()
    uid = r["rule_uid"]
    upd = rule_json(severity="CRITICAL") | {"expected_version": r["version"]}
    ok = c.put(f"{A}/rules/{uid}", json=upd, headers=h("reviewer"))
    assert ok.status_code == 200 and ok.json()["version"] == r["version"] + 1
    assert c.put(f"{A}/rules/{uid}", json=upd, headers=h("reviewer")).status_code == 409
    assert len(c.get(f"{A}/rules/{uid}/versions", headers=h("viewer")).json()["items"]) == 2
    assert c.post(f"{A}/rules/{uid}/disable", headers=h("reviewer")).status_code == 200
    assert c.post(f"{A}/rules/{uid}/bogus", headers=h("reviewer")).status_code == 422
    ev_run = c.post(f"{A}/evaluate", json={}, headers=h("reviewer"))
    assert ev_run.status_code == 200
    assert c.get(f"{A}/rules/{uid}/runs", headers=h("reviewer")).status_code == 200


def test_suppression_preferences_analytics_sla(secured):
    c = secured
    alert, rule = make_alert(c)
    s = c.post(
        f"{A}/suppressions",
        json={"rule_uid": rule["rule_uid"], "reason": "maintenance", "minutes": 30},
        headers=h("reviewer"),
    )
    assert s.status_code == 201, s.text
    assert (
        c.post(f"{A}/suppressions", json={"reason": "no scope", "minutes": 30}, headers=h("reviewer")).status_code
        == 422
    )
    assert (
        c.post(
            f"{A}/suppressions", json={"rule_uid": rule["rule_uid"], "reason": "x", "minutes": 30}, headers=h("editor")
        ).status_code
        == 403
    )
    sid = s.json().get("id") or c.get(f"{A}/suppressions", headers=h("reviewer")).json()["items"][0]["id"]
    assert c.post(f"{A}/suppressions/{sid}/revoke", headers=h("reviewer")).status_code == 200
    bad = c.put(f"{A}/preferences", json={"quiet_start_hour": 22}, headers=h("viewer"))
    assert bad.status_code == 422
    good = c.put(
        f"{A}/preferences",
        json={"min_severity": "HIGH", "quiet_start_hour": 22, "quiet_end_hour": 6},
        headers=h("viewer"),
    )
    assert good.status_code == 200
    p = c.get(f"{A}/preferences", headers=h("viewer")).json()
    assert p["min_severity"] == "HIGH" and p["channels"]["email"]["available"] is False
    assert c.get(f"{A}/preferences", headers=h("editor")).json()["min_severity"] == "LOW"
    an = c.get(f"{A}/analytics?window_days=7", headers=h("viewer"))
    assert an.status_code == 200
    assert c.get(f"{A}/analytics?window_days=5", headers=h("viewer")).status_code == 422
    sl = c.get(f"{A}/sla", headers=h("viewer")).json()
    assert set(sl["counts"]) == {"overdue", "due_soon", "on_track"}
    assert c.post(f"{A}/tick", headers=h("reviewer")).status_code == 200


def test_timer_is_off_by_default_and_ticks_when_enabled(settings, engine):
    import threading
    import time

    from app.alerting import service as S

    calls: list[int] = []
    orig = S.tick
    S.tick = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    try:
        with TestClient(create_app(make_settings(settings, api_tokens=TABLE), engine)):
            time.sleep(0.3)
            assert calls == [] and "atlas-alert-tick" not in [t.name for t in threading.enumerate()]
        with TestClient(create_app(make_settings(settings, api_tokens=TABLE, alerting_tick_seconds=0.1), engine)):
            deadline = time.time() + 5
            while not calls and time.time() < deadline:
                time.sleep(0.05)
            assert calls
        assert "atlas-alert-tick" not in [t.name for t in threading.enumerate()]  # stopped on shutdown
    finally:
        S.tick = orig
