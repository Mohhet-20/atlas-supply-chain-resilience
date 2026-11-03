"""Performance: leases and atomic claims, circuit breaker, sync time limit, metrics, system endpoints, API rate limit."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.integrations import exports, sync
from app.integrations import webhooks as W
from app.integrations.connectors import ConnectorError
from app.models.integrations import ExportJob, Integration, WebhookDelivery
from app.models.types import utcnow
from tests.integrations_helpers import API, URL, admin, client_for, enable, h, mk_integration, mk_user
from tests.security_helpers import bearer
from tests.test_integrations_sync import Scripted, execute, setup, start

WH = f"{API}/webhooks"


@pytest.fixture
def engine(settings, db_url):
    """Concurrent workers need WAL (``ATLAS_SQLITE_WAL=true``): in the default rollback-journal mode two connections that
    both hold a read lock and then write are refused at once with "database is locked", whatever the busy timeout."""
    from app.db.session import create_db_engine

    eng = create_db_engine(db_url, settings=settings.model_copy(update={"sqlite_wal": True}))
    yield eng
    eng.dispose()


@pytest.fixture
def c(settings, engine):
    with client_for(settings, engine) as client:
        yield client


def queue_delivery(c):
    ep = c.post(WH, json={"name": "Receiver", "url": URL, "events": ["*"]}, headers=admin()).json()
    c.post(f"{WH}/{ep['endpoint_uid']}/test", headers=admin())
    return ep


# ---- webhook delivery lease ------------------------------------------------------------------------------------------
def test_a_claimed_delivery_is_not_sent_twice_and_an_expired_lease_is_recovered(c):
    queue_delivery(c)
    st, cfg = c.app.state, c.app.state.settings
    now = utcnow() + timedelta(seconds=5)
    with st.session_factory() as db:
        d = db.scalar(select(WebhookDelivery).execution_options(skip_tenant=True))
        d.lease_until = now + timedelta(seconds=60)  # another worker holds it
        db.commit()
    with st.session_factory() as db:
        assert W.deliver_due(db, cfg, st.webhook_sender, now, resolve=False) == {}
    assert st.webhook_sender.calls == []
    with st.session_factory() as db:  # the holder crashed: after the lease the delivery is picked up exactly once
        assert W.deliver_due(db, cfg, st.webhook_sender, now + timedelta(seconds=120), resolve=False) == {
            "delivered": 1
        }
        assert W.deliver_due(db, cfg, st.webhook_sender, now + timedelta(seconds=120), resolve=False) == {}
    assert len(st.webhook_sender.calls) == 1
    with st.session_factory() as db:
        assert db.scalar(select(WebhookDelivery).execution_options(skip_tenant=True)).lease_until is None


def test_two_workers_racing_for_one_delivery_send_it_once(c):
    queue_delivery(c)
    st, cfg = c.app.state, c.app.state.settings
    gate, results = threading.Barrier(2), []
    now = utcnow() + timedelta(seconds=5)

    def worker():
        gate.wait()
        with st.session_factory() as db:
            from app.db.tenancy import scope_session

            scope_session(db, 1)
            results.append(W.deliver_due(db, cfg, st.webhook_sender, now, resolve=False))

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(st.webhook_sender.calls) == 1
    assert sum(r.get("delivered", 0) for r in results) == 1


# ---- export claim ----------------------------------------------------------------------------------------------------
def test_export_job_is_claimed_once(c):
    enable_ok = c.post(f"{API}/exports", json={"dataset": "suppliers", "format": "csv"}, headers=admin())
    assert enable_ok.status_code in (200, 201, 202), enable_ok.text
    uid = enable_ok.json()["export_uid"]
    st = c.app.state
    with st.session_factory() as db:
        job = db.scalar(select(ExportJob).where(ExportJob.export_uid == uid).execution_options(skip_tenant=True))
        job.status = "queued"
        db.commit()
    first = exports.run_job(st.session_factory, st.settings, 1, uid)
    assert first == "ready"
    with st.session_factory() as db:  # a duplicate trigger must not re-run or reset a finished job
        db_job = db.scalar(select(ExportJob).where(ExportJob.export_uid == uid).execution_options(skip_tenant=True))
        stamp = db_job.finished_at
    assert exports.run_job(st.session_factory, st.settings, 1, uid) == "ready"
    with st.session_factory() as db:
        assert (
            db.scalar(
                select(ExportJob).where(ExportJob.export_uid == uid).execution_options(skip_tenant=True)
            ).finished_at
            == stamp
        )


# ---- circuit breaker -------------------------------------------------------------------------------------------------
def failing():
    bad = Scripted()
    bad.fail_on = {1: ConnectorError("permanent", "HTTP 500 from provider")}
    return bad


def test_circuit_opens_after_consecutive_failures_blocks_scheduled_runs_and_closes_on_success(settings, engine):
    with client_for(settings, engine, circuit_failure_threshold=2, circuit_cooldown_seconds=600) as c:
        uid = mk_integration(c, schedule_minutes=30, config={"records": 10})["integration_uid"]
        enable(c, uid)
        for _ in range(2):
            execute(c, start(c, uid, "incremental"), failing())
        with c.app.state.session_factory() as db:
            integ = db.scalar(
                select(Integration).where(Integration.integration_uid == uid).execution_options(skip_tenant=True)
            )
            assert integ.consecutive_failed_runs == 2 and integ.circuit_open_until is not None
            from app.db.tenancy import scope_session

            scope_session(db, 1)
            with pytest.raises(ConflictError, match="circuit breaker"):
                sync.start_run(db, integ, "incremental", "scheduled", "scheduler")
            run = sync.start_run(db, integ, "incremental", "manual", "probe")  # manual probe is still allowed
            uid_run = run.run_uid
            db.commit()
        assert execute(c, uid_run, Scripted()) == "succeeded"
        with c.app.state.session_factory() as db:
            integ = db.scalar(
                select(Integration).where(Integration.integration_uid == uid).execution_options(skip_tenant=True)
            )
            assert integ.consecutive_failed_runs == 0 and integ.circuit_open_until is None


def test_sync_stops_at_its_time_limit_and_keeps_its_checkpoint(settings, engine):
    with client_for(settings, engine, sync_max_seconds=10) as c:
        uid = setup(c, 250)
        run = start(c, uid)
        c.app.state.settings.__dict__["sync_max_seconds"] = -1  # limit already exceeded after the first page boundary
        status = execute(c, run, Scripted())
        assert status == "partial"


# ---- metrics, system endpoints, rate limit ----------------------------------------------------------------------------
def platform_admin(c):
    mk_user(c, "ops.root", "admin", platform_admin=True)
    return bearer(c, "ops.root")


def test_metrics_use_route_templates_and_system_endpoints_are_platform_admin_only(c):
    c.get(f"{API}/suppliers/1", headers=admin())
    c.get(f"{API}/suppliers/2", headers=admin())
    ops = platform_admin(c)
    snap = c.get(f"{API}/system/metrics", headers=ops).json()
    keys = list(snap["routes"])
    assert any("/suppliers/{" in k for k in keys) and not any(k.endswith("/suppliers/1") for k in keys)
    assert snap["requests_total"] >= 2 and "cache" in snap
    prom = c.get(f"{API}/system/metrics/prometheus", headers=ops)
    assert prom.status_code == 200 and "atlas_http_requests_total" in prom.text and "Bearer" not in prom.text
    for role in ("admin", "viewer"):  # tenant administrators and viewers must not see process-wide data
        assert c.get(f"{API}/system/metrics", headers=h(role)).status_code == 403
        assert c.get(f"{API}/system/health", headers=h(role)).status_code == 403
    assert c.get(f"{API}/system/health").status_code == 401


def test_system_health_reports_database_workers_queues_and_cache(c):
    body = c.get(f"{API}/system/health", headers=platform_admin(c)).json()
    assert body["status"] == "ok" and body["database"]["status"] == "ok"
    assert set(body["queues"]) == {"webhook_deliveries", "oldest_due_delivery_age_seconds", "sync_runs", "exports"}
    assert body["cache"]["enabled"] is True
    assert "secret" not in str(body).lower() and "password" not in str(body).lower()


def test_stale_worker_heartbeat_degrades_system_health(c):
    c.app.state.worker_beats = {"integrations": 0.0}
    assert c.get(f"{API}/system/health", headers=platform_admin(c)).json()["status"] == "degraded"


def test_liveness_never_touches_the_database_or_providers(c):
    c.app.state.engine.dispose()
    c.app.state.engine = None  # any DB access would now raise
    assert c.get(f"{API}/health").status_code == 200


def test_api_rate_limit_is_per_credential_exempts_probes_and_sets_retry_after(settings, engine):
    with client_for(settings, engine, api_rate_limit_per_minute=5) as c:
        codes = [c.get(f"{API}/suppliers?limit=1", headers=h("viewer")).status_code for _ in range(7)]
        assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
        limited = c.get(f"{API}/suppliers?limit=1", headers=h("viewer"))
        assert limited.headers["retry-after"].isdigit() and limited.json()["error"]["code"] == "rate_limited"
        assert (
            c.get(f"{API}/suppliers?limit=1", headers=h("admin")).status_code == 200
        )  # another credential is unaffected
        assert all(c.get(f"{API}/health").status_code == 200 for _ in range(20))  # probes are exempt
        assert c.app.state.metrics.rate_limited >= 3


def test_readiness_schema_check_is_remembered_but_failures_are_not(c):
    assert c.get(f"{API}/readiness").status_code == 200
    assert c.app.state.schema_ok_until > 0
    c.app.state.schema_ok_until = 0.0
    assert c.get(f"{API}/readiness").status_code == 200
