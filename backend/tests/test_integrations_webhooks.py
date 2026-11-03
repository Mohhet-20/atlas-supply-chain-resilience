"""Integrations: outbound and inbound webhooks - signing, SSRF, retries, dead letter, auto-disable, replay, rotation, tenancy."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.alerting.notify import SendResult
from app.integrations import events, secrets
from app.integrations import webhooks as W
from app.models.integrations import WebhookDelivery, WebhookEndpoint
from app.models.types import utcnow
from tests.integrations_helpers import API, MASTER, URL, admin, client_for, h, mk_org, mk_user, post_inbound, sign
from tests.security_helpers import bearer

WH = f"{API}/webhooks"


@pytest.fixture
def c(settings, engine):
    with client_for(settings, engine) as client:
        yield client


def mk_out(c, headers=None, **over):
    r = c.post(WH, json={"name": "Receiver", "url": URL, "events": ["*"]} | over, headers=headers or admin())
    assert r.status_code == 201, r.text
    return r.json()


def event(i=1, **over):
    return {
        "event_id": f"evt-{i}",
        "event_type": "metric.observation",
        "occurred_at": "2026-01-01T00:00:00Z",
        "payload": {"metric": "stock_level", "value": 5.0, "unit": "units"},
        "entities": [],
    } | over


def test_secret_is_derived_not_stored_and_returned_once(c):
    ep = mk_out(c)
    assert ep["secret"].startswith("whsec_") or len(ep["secret"]) >= 32
    assert "secret" not in c.get(f"{WH}/{ep['endpoint_uid']}", headers=admin()).json()
    assert secrets.derive_secret(c.app.state.settings, "out", ep["endpoint_uid"], 1) == ep["secret"]
    with c.app.state.session_factory() as db:
        row = db.scalar(select(WebhookEndpoint).execution_options(skip_tenant=True))
        assert ep["secret"] not in json.dumps([getattr(row, k.name) for k in row.__table__.columns], default=str)


def test_creation_fails_closed_without_a_master_key(settings, engine):
    with client_for(settings, engine, integrations_master_key=None) as c:
        r = c.post(WH, json={"name": "No key", "url": URL, "events": ["*"]}, headers=admin())
        assert r.status_code == 503
        assert c.get(WH, headers=admin()).json()["total"] == 0  # the failed create left nothing behind


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://127.0.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.5/x",
        "https://[::1]/x",
        "https://evil.example.org/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com:8443/x",
        "ftp://hooks.example.com/x",
        "file:///etc/passwd",
        "https://localhost/x",
        "not a url",
    ],
)
def test_ssrf_destinations_are_refused(c, url):
    r = c.post(WH, json={"name": "Bad dest", "url": url, "events": ["*"]}, headers=admin())
    assert r.status_code == 422, (url, r.text)


def test_event_subscriptions_are_validated(c):
    assert (
        c.post(WH, json={"name": "Bad ev", "url": URL, "events": ["not.an.event"]}, headers=admin()).status_code == 422
    )
    assert c.post(WH, json={"name": "No ev", "url": URL, "events": []}, headers=admin()).status_code == 422
    assert c.post(WH, json={"name": "Inb url", "direction": "inbound", "url": URL}, headers=admin()).status_code == 422


def test_test_event_is_delivered_signed_and_verifiable(c):
    ep = mk_out(c)
    d = c.post(f"{WH}/{ep['endpoint_uid']}/test", headers=admin()).json()
    assert d["status"] == "queued"
    assert c.post(f"{WH}/deliver-now", headers=admin()).json()["result"] == {"delivered": 1}
    sent = c.app.state.webhook_sender.calls[0]
    assert sent["url"] == URL
    assert secrets.verify(ep["secret"], sent["headers"]["X-Atlas-Signature"], sent["body"], int(time.time()), 300)
    assert not secrets.verify(
        ep["secret"], sent["headers"]["X-Atlas-Signature"], sent["body"] + b" ", int(time.time()), 300
    )  # body tamper
    assert not secrets.verify(
        ep["secret"], sent["headers"]["X-Atlas-Signature"], sent["body"], int(time.time()) + 3600, 300
    )  # replay window
    env = json.loads(sent["body"])
    assert {
        "event_id",
        "event_type",
        "event_version",
        "schema_version",
        "organization_id",
        "occurred_at",
        "correlation_id",
        "source",
        "payload",
    } <= set(env)
    assert sent["headers"]["Idempotency-Key"] == env["event_id"]
    assert c.get(f"{WH}/{ep['endpoint_uid']}/deliveries", headers=admin()).json()["items"][0]["status"] == "delivered"


def test_retry_with_exponential_backoff_then_dead_letter_then_redelivery(settings, engine):
    with client_for(settings, engine, webhook_max_attempts=3, webhook_retry_base_seconds=30) as c:
        ep = mk_out(c)
        c.post(f"{WH}/{ep['endpoint_uid']}/test", headers=admin())
        c.app.state.webhook_sender.script = [SendResult("transient", "HTTP 503")] * 3
        st, cfg = c.app.state, c.app.state.settings
        now = utcnow()
        states = []
        for step in range(3):
            with st.session_factory() as db:
                W.deliver_due(db, cfg, st.webhook_sender, now + timedelta(hours=step), resolve=False)
                db.commit()
                d = db.scalar(select(WebhookDelivery).execution_options(skip_tenant=True))
                states.append((d.status, d.attempts))
        assert states == [("attempted", 1), ("attempted", 2), ("dead", 3)]
        assert W.backoff(cfg, 1) == 30 and W.backoff(cfg, 2) == 60 and W.backoff(cfg, 3) == 120
        dl = c.get(f"{WH}/{ep['endpoint_uid']}/deliveries?status=dead", headers=admin()).json()["items"][0]
        assert c.post(f"{WH}/deliveries/{dl['delivery_uid']}/redeliver", headers=admin()).status_code == 202
        assert c.post(f"{WH}/deliver-now", headers=admin()).json()["result"] == {"delivered": 1}
        again = c.app.state.webhook_sender.calls[-1]
        assert json.loads(again["body"])["event_id"] == dl["event_id"]  # same event id so receivers can dedupe
        assert (
            c.post(f"{WH}/deliveries/{dl['delivery_uid']}/redeliver", headers=admin()).status_code == 409
        )  # delivered ones cannot be replayed


def test_permanent_failure_is_not_retried_and_repeated_failures_disable_the_endpoint(c):
    ep = mk_out(c)
    st = c.app.state
    for _ in range(W.AUTO_DISABLE_AFTER):
        c.post(f"{WH}/{ep['endpoint_uid']}/test", headers=admin())
        st.webhook_sender.script = [SendResult("permanent", "HTTP 410")]
        res = c.post(f"{WH}/deliver-now", headers=admin()).json()["result"]
        assert res == {"failed": 1}
    got = c.get(f"{WH}/{ep['endpoint_uid']}", headers=admin()).json()
    assert got["status"] == "disabled" and "repeated" in got["disabled_reason"]
    assert c.post(f"{WH}/{ep['endpoint_uid']}/test", headers=admin()).status_code == 409
    assert c.post(f"{WH}/{ep['endpoint_uid']}/enable", headers=admin()).json()["consecutive_failures"] == 0


def test_alert_creation_enqueues_an_outbox_delivery_in_the_same_transaction(c):
    ep = mk_out(c, events=["alert.created"])
    with c.app.state.session_factory() as db:
        from app.db.tenancy import scope_session

        scope_session(db, 1)
        n = events.emit(db, 1, "alert.created", {"alert_uid": "AL-1", "title": "t", "severity": "HIGH"})
        db.rollback()  # the caller's transaction failed: nothing may be delivered
        assert n == 1
        assert db.scalar(select(WebhookDelivery).execution_options(skip_tenant=True)) is None
    assert c.get(f"{WH}/{ep['endpoint_uid']}/deliveries", headers=admin()).json()["total"] == 0
    assert events.emit.__doc__ and "alert.created" in events.CATALOGUE


def test_secret_rotation_invalidates_the_previous_secret(c):
    ep = mk_out(c)
    new = c.post(f"{WH}/{ep['endpoint_uid']}/rotate-secret", headers=admin()).json()
    assert new["secret"] != ep["secret"] and new["secret_version"] == 2
    c.post(f"{WH}/{ep['endpoint_uid']}/test", headers=admin())
    c.post(f"{WH}/deliver-now", headers=admin())
    sent = c.app.state.webhook_sender.calls[-1]
    assert secrets.verify(new["secret"], sent["headers"]["X-Atlas-Signature"], sent["body"], int(time.time()), 300)
    assert not secrets.verify(ep["secret"], sent["headers"]["X-Atlas-Signature"], sent["body"], int(time.time()), 300)


def test_authorisation_and_unauthorised_modification(c):
    ep = mk_out(c)
    uid = ep["endpoint_uid"]
    for role in ("viewer", "editor"):
        assert c.get(WH, headers=h(role)).status_code == 403
    assert c.get(WH, headers=h("reviewer")).status_code == 200
    for role in ("viewer", "editor", "reviewer"):
        for m, p in (
            ("post", f"{uid}/rotate-secret"),
            ("post", f"{uid}/disable"),
            ("delete", uid),
            ("post", f"{uid}/test"),
            ("post", ""),
        ):
            assert c.request(m.upper(), f"{WH}/{p}".rstrip("/"), headers=h(role), json={}).status_code == 403, (
                role,
                m,
                p,
            )
    assert "secret" not in c.get(f"{WH}/{uid}", headers=h("reviewer")).json()


def test_inbound_accepts_a_valid_signed_event_once(c):
    ep = c.post(WH, json={"name": "Partner feed", "direction": "inbound"}, headers=admin()).json()
    assert ep["receive_url"].endswith(ep["endpoint_uid"])
    r = post_inbound(c, ep["endpoint_uid"], ep["secret"], event(1))
    assert r.status_code == 202 and r.json()["status"] == "accepted", r.text
    dup = post_inbound(c, ep["endpoint_uid"], ep["secret"], event(1))
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate_event"  # replay of the same event id
    assert post_inbound(c, ep["endpoint_uid"], ep["secret"], event(2)).status_code == 202


def test_inbound_rejects_bad_signatures_stale_timestamps_and_unknown_endpoints_identically(c):
    ep = c.post(WH, json={"name": "Partner feed", "direction": "inbound"}, headers=admin()).json()
    uid, secret = ep["endpoint_uid"], ep["secret"]
    bad = [
        post_inbound(c, uid, "wrong-secret", event(1)),
        post_inbound(
            c, uid, secret, event(2), ts=int(time.time()) - 3600
        ),  # outside tolerance (replay of an old capture)
        post_inbound(c, uid, secret, event(3), sig="t=1,v1=00"),
        post_inbound(c, "WH-DOESNOTEXIST", secret, event(4)),
        c.post(f"{WH}/inbound/{uid}", json=event(5)),  # unsigned
        c.post(f"{WH}/inbound/{uid}", content=b"{}", headers={"X-Atlas-Signature": "garbage"}),
    ]
    assert {r.status_code for r in bad} == {401}
    assert len({json.dumps(r.json(), sort_keys=True) for r in bad}) == 1  # no oracle for which part failed
    # a body signed for one payload cannot be swapped for another
    body = json.dumps(event(6)).encode()
    sig = sign(secret, body)
    other = json.dumps(event(7)).encode()
    assert c.post(f"{WH}/inbound/{uid}", content=other, headers={"X-Atlas-Signature": sig}).status_code == 401


def test_inbound_validates_the_event_and_a_disabled_endpoint_stops_accepting(c):
    ep = c.post(WH, json={"name": "Partner feed", "direction": "inbound"}, headers=admin()).json()
    uid, secret = ep["endpoint_uid"], ep["secret"]
    assert post_inbound(c, uid, secret, event(1, event_type="os.system")).status_code == 422
    assert post_inbound(c, uid, secret, event(2, payload={"metric": "x"})).status_code == 422  # missing value/unit
    assert post_inbound(c, uid, secret, event(3, unexpected=True)).status_code == 422
    c.post(f"{WH}/{uid}/disable", headers=admin())
    assert post_inbound(c, uid, secret, event(4)).status_code == 401


def test_inbound_rejects_oversized_payloads(c):
    ep = c.post(WH, json={"name": "Partner feed", "direction": "inbound"}, headers=admin()).json()
    body = json.dumps(event(1, payload={"metric": "m", "value": 1, "unit": "u", "pad": "a" * 300_000})).encode()
    r = c.post(
        f"{WH}/inbound/{ep['endpoint_uid']}", content=body, headers={"X-Atlas-Signature": sign(ep["secret"], body)}
    )
    assert r.status_code == 413


def test_tenant_isolation_of_webhooks_and_deliveries(settings, engine):
    with client_for(settings, engine) as c:
        org2 = mk_org(c, "umbrella")
        mk_user(c, "bob", "admin", org_id=org2)
        mk_user(c, "alice", "admin")
        a, b = bearer(c, "alice"), bearer(c, "bob")
        ep = mk_out(c, headers=a)
        uid = ep["endpoint_uid"]
        c.post(f"{WH}/{uid}/test", headers=a)
        d = c.get(f"{WH}/{uid}/deliveries", headers=a).json()["items"][0]["delivery_uid"]
        assert c.get(WH, headers=b).json()["total"] == 0
        for m, p in (
            ("get", uid),
            ("post", f"{uid}/rotate-secret"),
            ("post", f"{uid}/test"),
            ("get", f"{uid}/deliveries"),
            ("delete", uid),
            ("post", f"deliveries/{d}/redeliver"),
        ):
            assert c.request(m.upper(), f"{WH}/{p}", headers=b, json={} if m == "post" else None).status_code == 404, (
                m,
                p,
            )
        # organisation 2's worker run neither sees nor sends organisation 1's queued delivery
        assert c.post(f"{WH}/deliver-now", headers=b).json()["result"] == {}
        assert c.app.state.webhook_sender.calls == []
        # an inbound endpoint's events land in the endpoint's own organisation
        inb = c.post(WH, json={"name": "Partner feed", "direction": "inbound"}, headers=b).json()
        assert post_inbound(c, inb["endpoint_uid"], inb["secret"], event(1)).status_code == 202
        assert c.get(f"{API}/alerting/events", headers=a).json()["total"] == 0
        assert c.get(f"{API}/alerting/events", headers=b).json()["total"] == 1
        assert (
            secrets.derive_secret(c.app.state.settings, "in", inb["endpoint_uid"], 1) == inb["secret"]
            and MASTER not in inb["secret"]
        )
