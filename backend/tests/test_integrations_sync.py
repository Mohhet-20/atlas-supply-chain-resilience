"""Integrations: synchronisation engine - pagination, retries, partial failure, checkpoints, resume, overlap, cancel, scheduling, mapping."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.api.v1.alerting import build_ctx
from app.core.errors import ConflictError
from app.integrations import mapping, sync
from app.integrations.connectors import ConnectorError, Page, SandboxInventory
from app.models.integrations import Integration, IntegrationRun, SyncCheckpoint, SyncError
from app.models.types import utcnow
from tests.integrations_helpers import API, admin, client_for, enable, mk_integration


class Scripted(SandboxInventory):
    """The sandbox feed with injectable failures: `fail_on` maps a call number to an error; `corrupt` maps record index to a bad value."""

    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str | None]] = []
        self.fail_on: dict[int, ConnectorError] = {}
        self.corrupt: set[str] = set()

    def fetch(self, config, credential, cursor, since, limit, hosts) -> Page:  # noqa: ANN001
        n = len(self.calls) + 1
        self.calls.append((cursor, since))
        if n in self.fail_on:
            raise self.fail_on[n]
        page = super().fetch(config, credential, cursor, since, limit, hosts)
        for r in page.records:
            if r["external_id"] in self.corrupt:
                r["quantity"] = "not-a-number"
        return page


@pytest.fixture
def env(settings, engine):
    with client_for(settings, engine) as c:
        yield c


def setup(c, records=250, **kw):
    uid = mk_integration(c, config={"records": records}, **kw)["integration_uid"]
    enable(c, uid)
    return uid


def start(c, uid, mode="full", resume=False):
    with c.app.state.session_factory() as db:
        from app.db.tenancy import scope_session

        scope_session(db, 1)
        integ = db.scalar(select(Integration).where(Integration.integration_uid == uid))
        run = sync.start_run(db, integ, mode, "manual", "test", resume=resume)
        db.commit()
        return run.run_uid


def execute(c, run_uid, conn):
    return sync.execute_run(
        c.app.state.session_factory,
        c.app.state.settings,
        1,
        run_uid,
        build_ctx(c.app, org_id=1),
        connector=conn,
        sleep=lambda s: None,
    )


def run_row(c, run_uid):
    with c.app.state.session_factory() as db:
        return db.scalar(
            select(IntegrationRun).where(IntegrationRun.run_uid == run_uid).execution_options(skip_tenant=True)
        )


def test_pagination_counts_and_checkpoint_per_page(env):
    uid, conn = setup(env), Scripted()
    run = start(env, uid)
    assert execute(env, run, conn) == "succeeded"
    r = run_row(env, run)
    assert (r.discovered, r.processed, r.created, r.skipped, r.failed, r.rejected) == (250, 250, 250, 0, 0, 0)
    assert [c[0] for c in conn.calls] == [None, "100", "200"]  # three pages
    assert r.checkpoint["cursor"] is None and r.duration_ms is not None


def test_transient_errors_are_retried_with_backoff_then_succeed(env):
    uid, conn = setup(env, 50), Scripted()
    conn.fail_on = {1: ConnectorError("transient", "HTTP 503"), 2: ConnectorError("transient", "timeout")}
    run = start(env, uid)
    assert execute(env, run, conn) == "succeeded"
    assert run_row(env, run).retries == 2 and run_row(env, run).created == 50


def test_exhausted_retries_and_auth_errors_fail_the_run_without_hammering(env):
    uid = setup(env, 50)
    conn = Scripted()
    conn.fail_on = {i: ConnectorError("transient", "HTTP 503") for i in range(1, 10)}
    run = start(env, uid)
    assert execute(env, run, conn) == "failed" and len(conn.calls) == env.app.state.settings.sync_page_retry_attempts
    auth = Scripted()
    auth.fail_on = {1: ConnectorError("auth", "401 from provider")}
    run2 = start(env, uid)
    assert execute(env, run2, auth) == "failed" and len(auth.calls) == 1  # credential problems are never retried
    assert "auth" in run_row(env, run2).error_summary
    with env.app.state.session_factory() as db:
        assert db.scalar(select(Integration).where(Integration.integration_uid == uid)).status == "failed"
    assert env.get(f"{API}/integrations/{uid}/health", headers=admin()).json()["state"] in ("degraded", "unavailable")


def test_bad_records_are_rejected_individually_recorded_and_the_watermark_does_not_advance(env):
    uid, conn = setup(env, 30), Scripted()
    conn.corrupt = {"SBX-00003", "SBX-00010"}
    run = start(env, uid)
    assert execute(env, run, conn) == "partial"
    r = run_row(env, run)
    assert r.created == 28 and r.rejected == 2
    with env.app.state.session_factory() as db:
        errs = db.scalars(select(SyncError).where(SyncError.run_id == r.id).execution_options(skip_tenant=True)).all()
        assert {e.record_ref for e in errs} == {"SBX-00003", "SBX-00010"}
        assert (
            db.scalar(select(SyncCheckpoint).execution_options(skip_tenant=True)) is None
        )  # incremental window not advanced
        assert db.scalar(select(Integration).where(Integration.integration_uid == uid)).status == "degraded"


def test_failed_run_resumes_from_its_checkpoint_without_duplicates(env):
    uid, conn = setup(env, 250), Scripted()
    conn.fail_on = {2: ConnectorError("permanent", "HTTP 400")}
    first = start(env, uid)
    assert execute(env, first, conn) == "failed"
    r1 = run_row(env, first)
    assert r1.created == 100 and r1.checkpoint["cursor"] == "100"  # page one was committed with its checkpoint
    conn2 = Scripted()
    second = start(env, uid, resume=True)
    assert execute(env, second, conn2) == "succeeded"
    assert conn2.calls[0][0] == "100"  # did not start again from the beginning
    assert run_row(env, second).created == 150
    again = start(env, uid)  # a full re-run proves nothing was duplicated
    execute(env, again, Scripted())
    assert run_row(env, again).created == 0 and run_row(env, again).skipped == 250


def test_incremental_run_uses_the_watermark_of_the_last_clean_run(env):
    uid = setup(env, 40)
    execute(env, start(env, uid, "full"), Scripted())
    conn = Scripted()
    run = start(env, uid, "incremental")
    assert execute(env, run, conn) == "succeeded"
    assert conn.calls[0][1] is not None  # `since` was sent
    assert run_row(env, run).discovered == 0 and run_row(env, run).created == 0


def test_only_one_active_run_and_stale_runs_are_reaped(env):
    uid = setup(env, 10)
    first = start(env, uid)
    with pytest.raises(ConflictError):
        start(env, uid)  # database-enforced, not just a check
    r = run_row(env, first)
    with env.app.state.session_factory() as db:
        row = db.scalar(
            select(IntegrationRun).where(IntegrationRun.run_uid == first).execution_options(skip_tenant=True)
        )
        row.heartbeat_at = utcnow() - timedelta(minutes=30)  # the worker died
        db.commit()
    second = start(env, uid)
    assert run_row(env, first).status == "failed" and "abandoned" in run_row(env, first).error_summary
    assert r.status == "queued" and second != first


def test_overlap_is_refused_through_the_api_with_409(env):
    uid = setup(env, 10)
    start(env, uid)
    assert env.post(f"{API}/integrations/{uid}/sync", json={}, headers=admin()).status_code == 409


def test_cancellation_between_pages(env):
    uid, conn = setup(env, 250), Scripted()
    run = start(env, uid)
    queued = env.post(f"{API}/integrations/runs/{run}/cancel", headers=admin())
    assert queued.status_code == 200 and queued.json()["status"] == "cancelled"  # queued runs cancel at once
    assert conn.calls == []
    assert env.post(f"{API}/integrations/runs/{run}/cancel", headers=admin()).status_code == 409
    run2 = start(env, uid)
    with env.app.state.session_factory() as db:
        db.scalar(
            select(IntegrationRun).where(IntegrationRun.run_uid == run2).execution_options(skip_tenant=True)
        ).cancel_requested = True
        db.commit()
    assert execute(env, run2, conn) == "cancelled" and conn.calls == []


def test_record_limit_stops_the_run_as_partial(settings, engine):
    with client_for(settings, engine, sync_max_records=20) as c:
        uid = setup(c, 60)
        run = start(c, uid)
        assert execute(c, run, Scripted()) == "partial"
        assert run_row(c, run).created == 20 and "limit" in run_row(c, run).error_summary


def test_scheduling_runs_due_integrations_once_without_stampede(env):
    uid = mk_integration(env, schedule_minutes=60, config={"records": 10})["integration_uid"]
    enable(env, uid)
    st, ctx = env.app.state, build_ctx(env.app, org_id=1)
    now = utcnow() + timedelta(seconds=1)
    first = sync.run_due(st.session_factory, st.settings, 1, ctx, now=now)
    assert len(first) == 1
    assert sync.run_due(st.session_factory, st.settings, 1, ctx, now=now) == []  # next_run_at already advanced
    with st.session_factory() as db:
        integ = db.scalar(
            select(Integration).where(Integration.integration_uid == uid).execution_options(skip_tenant=True)
        )
        assert integ.next_run_at > now + timedelta(minutes=59)
        assert (
            db.scalar(
                select(IntegrationRun).where(IntegrationRun.trigger == "scheduled").execution_options(skip_tenant=True)
            ).status
            == "succeeded"
        )
    assert (
        sync.run_due(st.session_factory, st.settings, 1, ctx, now=now + timedelta(minutes=61)) != []
    )  # due again an hour later


def test_scheduled_failures_are_reported_in_integration_state(env):
    uid = mk_integration(env, schedule_minutes=30, config={"records": 10})["integration_uid"]
    enable(env, uid)
    st = env.app.state
    run = start(env, uid, "incremental")
    bad = Scripted()
    bad.fail_on = {1: ConnectorError("permanent", "HTTP 500 from provider")}
    execute(env, run, bad)
    got = env.get(f"{API}/integrations/{uid}", headers=admin()).json()
    assert got["status"] == "failed" and got["last_failure_at"] and "HTTP 500" in got["error_summary"]
    assert st.settings.sync_max_records > 0


def test_mapping_validation_conversion_and_provenance():
    assert mapping.validate_spec(mapping.SANDBOX_MAPPING) == []
    assert mapping.validate_spec({"event_type": "os.system"})
    assert any(
        "unit" in e
        for e in mapping.validate_spec(
            {
                **mapping.SANDBOX_MAPPING,
                "fields": {"value": {"from": "q", "type": "float", "unit_from": "bogus", "unit_to": "kg"}},
            }
        )
    )
    rec = {
        "external_id": "X-1",
        "updated_at": "2026-03-01T10:00:00+02:00",
        "facility": "F-1",
        "metric": "m",
        "quantity": "5",
        "unit": "u",
    }
    out = mapping.map_record(mapping.SANDBOX_MAPPING, rec, "IG-ABC")
    assert out["occurred_at"].utcoffset().total_seconds() == 0 and out["occurred_at"].hour == 8  # converted to UTC
    assert out["payload"]["value"] == 5.0
    assert out["source_record_id"] == "X-1" and "IG-ABC" in out["idempotency_key"]  # provenance: source + record
    with pytest.raises(mapping.MappingError):
        mapping.map_record(mapping.SANDBOX_MAPPING, {**rec, "external_id": ""}, "IG-ABC")
    with pytest.raises(mapping.MappingError):
        mapping.map_record(mapping.SANDBOX_MAPPING, {**rec, "updated_at": "yesterday-ish"}, "IG-ABC")
    # the same record always maps to the same idempotency key
    assert out["idempotency_key"] == mapping.map_record(mapping.SANDBOX_MAPPING, rec, "IG-ABC")["idempotency_key"]
