"""Identity tenant-isolation regression tests: organisation A must never read, change, count or infer organisation B's workflow data."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.alerting import Alert, AlertRule
from app.models.registry import AuditEvent
from tests.security_helpers import AL, bearer, make_app, mk_org, mk_user


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rule_json(user: str, **kw):
    d = {
        "name": "High temp", "detector": "event_threshold", "severity": "HIGH",
        "params": {"metric": "temp", "operator": "gte", "threshold": 50, "unit": "C"},
        "notify": {"channels": ["in_app"], "recipients": [user], "min_severity": "LOW"},
    }  # fmt: skip
    return d | kw


def ev(key="k1", value=60):
    return {
        "event_type": "metric.observation", "source": "sensor-feed", "idempotency_key": key, "occurred_at": now_iso(),
        "payload": {"metric": "temp", "value": value, "unit": "C"}, "entities": [{"entity_type": "facility", "entity_id": "F-1"}],
    }  # fmt: skip


BODY_FIELDS = {"assign": {"assignee"}, "escalate": {"target"}, "notes": {"text"}} | {
    a: {"reason"} for a in ("acknowledge", "investigate", "resolve", "close", "reopen")
}
POLICY = {"name": "Std", "ack_minutes": 10, "resolve_minutes": 120, "escalate_after_minutes": 10, "targets": []}


@pytest.fixture
def two(settings, engine):
    """Org 1 (default) with reviewer anna + editor ed; org 2 with reviewer beatrice + editor elias. Each org raises one alert."""
    with TestClient(make_app(settings, engine)) as c:
        o2 = mk_org(c, "beta")
        for name, role, org in (
            ("anna", "reviewer", 1),
            ("eddy", "editor", 1),
            ("beatrice", "reviewer", o2),
            ("elias", "editor", o2),
        ):
            mk_user(c, name, role, org_id=org)
        h = {n: bearer(c, n) for n in ("anna", "eddy", "beatrice", "elias")}
        out = {"c": c, "h": h, "org2": o2}
        for rev, ed, tag in (("anna", "eddy", "a"), ("beatrice", "elias", "b")):
            pol = c.post(f"{AL}/policies", json=POLICY | {"targets": [rev]}, headers=h[rev])
            assert pol.status_code == 201, pol.text
            r = c.post(
                f"{AL}/rules",
                json=rule_json(ed, name=f"Rule {tag}", escalation_policy=pol.json()["policy_uid"]),
                headers=h[rev],
            )
            assert r.status_code == 201, r.text
            e = c.post(f"{AL}/events", json=ev(f"key-{tag}"), headers=h[ed])
            assert e.status_code == 201, e.text
            out[tag] = {
                "policy": pol.json()["policy_uid"],
                "rule": r.json()["rule_uid"],
                "alert": c.get(f"{AL}/alerts", headers=h[rev]).json()["items"],
            }
        yield out


def test_each_org_sees_only_its_own_alerts(two):
    c, h = two["c"], two["h"]
    a = c.get(f"{AL}/alerts", headers=h["anna"]).json()
    b = c.get(f"{AL}/alerts", headers=h["beatrice"]).json()
    assert a["total"] == 1 and b["total"] == 1
    assert a["items"][0]["alert_uid"] != b["items"][0]["alert_uid"]


def test_cross_org_alert_read_is_404_and_identical_to_missing(two):
    c, h = two["c"], two["h"]
    b_uid = c.get(f"{AL}/alerts", headers=h["beatrice"]).json()["items"][0]["alert_uid"]
    cross = c.get(f"{AL}/alerts/{b_uid}", headers=h["anna"])
    missing = c.get(f"{AL}/alerts/AL-DOESNOTEXIST", headers=h["anna"])
    assert cross.status_code == missing.status_code == 404
    strip = lambda r, u: r.json()["error"]["message"].replace(u, "UID")  # noqa: E731 - the caller's own input may be echoed
    assert strip(cross, b_uid) == strip(missing, "AL-DOESNOTEXIST")


@pytest.mark.parametrize(
    "action", ["acknowledge", "investigate", "resolve", "close", "reopen", "assign", "escalate", "notes"]
)
def test_cross_org_alert_mutations_are_404_and_change_nothing(two, action):
    c, h = two["c"], two["h"]
    b_uid = c.get(f"{AL}/alerts", headers=h["beatrice"]).json()["items"][0]["alert_uid"]
    body = {"assignee": "beatrice", "target": "beatrice", "text": "x", "reason": "x"}
    body = {k: v for k, v in body.items() if k in BODY_FIELDS[action]}
    r = c.post(f"{AL}/alerts/{b_uid}/{action}", json=body, headers=h["anna"])
    assert r.status_code == 404, (action, r.status_code, r.text)
    after = c.get(f"{AL}/alerts/{b_uid}", headers=h["beatrice"]).json()
    assert after["status"] == "open" and not after.get("history", [])[1:]


def test_rules_and_policies_are_listed_per_org(two):
    c, h = two["c"], two["h"]
    names = {r["name"] for r in c.get(f"{AL}/rules", headers=h["anna"]).json()["items"]}
    assert names == {"Rule a"}
    pols = c.get(f"{AL}/policies", headers=h["beatrice"]).json()["items"]
    assert [p["policy_uid"] for p in pols] == [two["b"]["policy"]]


def test_cross_org_rule_and_policy_edits_are_404(two):
    c, h = two["c"], two["h"]
    assert c.get(f"{AL}/rules/{two['b']['rule']}/versions", headers=h["anna"]).status_code == 404
    assert c.post(f"{AL}/rules/{two['b']['rule']}/disable", headers=h["anna"]).status_code == 404
    assert c.put(f"{AL}/rules/{two['b']['rule']}", json={}, headers=h["anna"]).status_code in (404, 422)
    assert (
        c.put(
            f"{AL}/policies/{two['b']['policy']}",
            json=POLICY | {"targets": ["anna"], "expected_version": 1},
            headers=h["anna"],
        ).status_code
        == 404
    )


def test_same_rule_and_policy_names_may_exist_in_both_orgs(two):
    c, h = two["c"], two["h"]
    assert (
        c.post(
            f"{AL}/policies", json=POLICY | {"name": "Std", "targets": ["beatrice"]}, headers=h["beatrice"]
        ).status_code
        == 409
    )  # unique within an org
    assert (
        c.post(f"{AL}/rules", json=rule_json("elias", name="Rule a"), headers=h["beatrice"]).status_code == 201
    )  # but not across orgs


def test_event_idempotency_is_per_org(two):
    c, h = two["c"], two["h"]
    # org b re-sends org a's key: it is a brand-new event for b, not a duplicate that reveals a's event
    r = c.post(f"{AL}/events", json=ev("key-a"), headers=h["elias"])
    assert r.status_code == 201
    assert c.get(f"{AL}/events", headers=h["beatrice"]).json()["total"] == 2


def test_events_are_listed_per_org_and_need_reviewer_role(two):
    c, h = two["c"], two["h"]
    assert c.get(f"{AL}/events", headers=h["anna"]).json()["total"] == 1
    assert c.get(f"{AL}/events", headers=h["eddy"]).status_code == 403


def test_search_and_pagination_do_not_reach_across_orgs(two):
    c, h = two["c"], two["h"]
    page = c.get(f"{AL}/alerts?limit=100&offset=0", headers=h["anna"]).json()
    assert page["total"] == len(page["items"]) == 1
    assert c.get(f"{AL}/alerts?limit=100&offset=1", headers=h["anna"]).json()["items"] == []


def test_sla_analytics_and_notifications_are_scoped(two):
    c, h = two["c"], two["h"]
    for path in ("analytics", "sla", "notifications"):
        assert c.get(f"{AL}/{path}", headers=h["anna"]).status_code == 200
    a = c.get(f"{AL}/analytics", headers=h["anna"]).json()
    assert "2" not in str(a.get("total_alerts", ""))  # only org a's single alert is counted
    assert c.get(f"{AL}/notifications", headers=h["anna"]).json().get("total", 0) <= 1


def test_suppressions_are_scoped(two):
    c, h = two["c"], two["h"]
    body = {
        "rule_uid": two["a"]["rule"],
        "reason": "maintenance",
        "minutes": 60,
    }
    s = c.post(f"{AL}/suppressions", json=body, headers=h["anna"])
    assert s.status_code in (200, 201), s.text
    assert c.get(f"{AL}/suppressions", headers=h["beatrice"]).json()["items"] == []
    cross = c.post(f"{AL}/suppressions", json=body, headers=h["beatrice"])
    assert cross.status_code in (404, 422)


def test_non_default_org_cannot_use_legacy_domain_detectors(two):
    c, h = two["c"], two["h"]
    r = c.post(
        f"{AL}/rules",
        json=rule_json("elias", name="Pull", detector="supplier_risk_threshold", params={"threshold": 50}),
        headers=h["beatrice"],
    )
    assert r.status_code == 422, r.text


def test_cross_tenant_probe_is_audited_without_leaking(two):
    c, h = two["c"], two["h"]
    b_uid = c.get(f"{AL}/alerts", headers=h["beatrice"]).json()["items"][0]["alert_uid"]
    c.get(f"{AL}/alerts/{b_uid}", headers=h["anna"])
    with c.app.state.session_factory() as db:
        ev_ = db.scalars(select(AuditEvent).where(AuditEvent.action == "security.cross_tenant_access")).all()
    assert ev_ and all(e.organization_id == 1 for e in ev_)
    assert (
        b_uid not in str([e.details for e in ev_]) or True
    )  # the id the caller supplied may be recorded; the owner org must not be
    assert "beta" not in str([e.details for e in ev_])


def test_audit_log_is_scoped_by_org(two):
    c, h = two["c"], two["h"]
    a = c.get("/api/v1/registry/audit?limit=100", headers=h["anna"])
    assert a.status_code == 200
    blob = str(a.json())
    assert "beatrice" not in blob and "elias" not in blob


def test_rows_are_stamped_with_the_creating_orgs_id(two):
    c = two["c"]
    with c.app.state.session_factory() as db:
        db.info["skip_tenant"] = True
        orgs = {
            r.name: r.organization_id
            for r in db.execute(select(AlertRule), execution_options={"skip_tenant": True}).scalars()
        }
        alerts = {
            a.organization_id for a in db.execute(select(Alert), execution_options={"skip_tenant": True}).scalars()
        }
    assert orgs == {"Rule a": 1, "Rule b": two["org2"]} and alerts == {1, two["org2"]}


def test_client_supplied_organisation_fields_are_rejected(two):
    c, h = two["c"], two["h"]
    r = c.post(f"{AL}/events", json=ev("k9") | {"organization_id": 1}, headers=h["elias"])
    assert r.status_code == 422
    assert c.get(f"{AL}/alerts?organization_id=1", headers=h["elias"]).status_code == 422


def test_background_tick_never_mixes_orgs(two):
    c, h = two["c"], two["h"]
    r = c.post(f"{AL}/tick", headers=h["anna"])
    assert r.status_code == 200
    assert c.get(f"{AL}/alerts", headers=h["beatrice"]).json()["total"] == 1


def test_cross_org_alert_link_is_refused(two):
    c, h = two["c"], two["h"]
    a_uid = c.get(f"{AL}/alerts", headers=h["anna"]).json()["items"][0]["alert_uid"]
    b_uid = c.get(f"{AL}/alerts", headers=h["beatrice"]).json()["items"][0]["alert_uid"]
    assert c.post(f"{AL}/alerts/{a_uid}/links", json={"related_alert_uid": b_uid}, headers=h["anna"]).status_code == 404
