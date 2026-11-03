"""Run lifecycle: transitions, limits, truncation, failure handling, cancellation, concurrency, idempotency and recovery."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError
from app.models.enums import SimStatus
from app.models.scenario import Scenario, SimulationImpact, SimulationRun
from app.models.types import utcnow
from app.scenarios import runs, service
from app.scenarios.runs import RunLimits
from tests.graph_helpers import node, ok
from tests.scenario_helpers import chain


def new_scenario(client, events, name="S", **assump):
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        sc = service.create_scenario(s, "tester", name=name, description=None, scenario_type=service.ScenarioType.CUSTOM, assumptions=assump or None, notes=None, template_key=None, max_scenarios=100)  # fmt: skip
        for e in events:
            service.add_event(s, "tester", sc, e, None, 25)
        sid = sc.id
    return sid


def sup_event(n):
    return {"event_type": "SUPPLIER_UNAVAILABLE", "target_type": "SUPPLIER", "target_id": n["id"], "duration_days": 10}


def fetch(client, run_id):
    with Session(client.app.state.engine) as s:
        r = s.get(SimulationRun, run_id)
        s.expunge(r)
        return r


def impact_count(client, run_id):
    with Session(client.app.state.engine) as s:
        return s.scalar(select(func.count()).select_from(SimulationImpact).where(SimulationImpact.run_id == run_id))


def test_completed_run_records_everything(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    r = runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits())
    r = fetch(client, r.id)
    assert r.status is SimStatus.COMPLETED and r.started_at and r.finished_at and r.duration_ms is not None
    assert (
        r.engine_version == "sim-1"
        and len(r.graph_hash) == 64
        and r.graph_snapshot["supply_relationships"]["count"] == 3
    )
    assert r.entities_affected == impact_count(client, r.id) == 4 and r.direct_count == 1 and r.indirect_count == 3
    assert r.entities_examined >= 4 and not r.truncated and r.failures == []
    assert any(w["code"] == "NO_BUFFER_MODEL" for w in r.warnings)


def test_node_limit_makes_run_partial_and_says_why(client):
    a = node(client, "suppliers", "Alder Parts")
    for n in range(8):
        ok(client, "SUPPLIES", a, node(client, "facilities", f"Site {n}"), supply_share_pct=10.0)
    sid = new_scenario(client, [sup_event(a)])
    r = fetch(client, runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits(max_nodes=5)).id)
    assert r.status is SimStatus.PARTIAL and r.truncated and "node_limit" in r.truncation_reasons
    assert r.entities_affected <= 5


def test_depth_limit_partial(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])], downstream_depth=1)
    r = fetch(client, runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits()).id)
    assert r.status is SimStatus.PARTIAL and "depth_limit" in r.truncation_reasons


def test_runtime_budget_exceeded_is_partial_not_success(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    r = fetch(
        client,
        runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits(time_budget_seconds=0.000001)).id,
    )
    assert r.status is SimStatus.PARTIAL and "time_budget" in r.truncation_reasons and r.truncated


def test_engine_failure_marks_failed_and_releases_the_lock(client, monkeypatch):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])

    def boom(*a, **k):
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(runs, "run_engine", boom)
    r = fetch(client, runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits()).id)
    assert r.status is SimStatus.FAILED and r.error == "unexpected RuntimeError" and r.finished_at
    assert impact_count(client, r.id) == 0
    monkeypatch.undo()  # retry after the failure: the active-run lock must be free
    again = fetch(client, runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits()).id)
    assert again.status is SimStatus.COMPLETED and again.id != r.id


def test_failure_while_saving_rolls_back_partial_results(client, monkeypatch):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    real = runs.SimulationEdgeImpact

    class Bad:
        def __init__(self, **kw):
            raise ValueError("cannot persist")

    monkeypatch.setattr(runs, "SimulationEdgeImpact", Bad)
    r = fetch(client, runs.run_now(client.app.state.session_factory, sid, "tester", RunLimits()).id)
    monkeypatch.setattr(runs, "SimulationEdgeImpact", real)
    assert r.status is SimStatus.FAILED and impact_count(client, r.id) == 0  # no half-written result set


def test_cancel_queued_run(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    f = client.app.state.session_factory
    with f() as s, s.begin():
        run, _ = runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits())
        runs.cancel_run(s, run)
        rid = run.id
    runs.execute_run(f, rid, RunLimits())  # a late executor must do nothing
    r = fetch(client, rid)
    assert r.status is SimStatus.CANCELLED and impact_count(client, rid) == 0
    with f() as s, pytest.raises(InvalidTransitionError):
        runs.cancel_run(s, s.get(SimulationRun, rid))


def test_cancel_running_run_stops_at_checkpoint(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    f = client.app.state.session_factory
    with f() as s, s.begin():
        run, _ = runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits())
        run.cancel_requested = True  # requested while the run was waiting or running
        rid = run.id
    runs.execute_run(f, rid, RunLimits())
    r = fetch(client, rid)
    assert r.status is SimStatus.CANCELLED and r.error == "cancelled by request" and impact_count(client, rid) == 0
    # the scenario can be run again afterwards
    assert fetch(client, runs.run_now(f, sid, "tester", RunLimits()).id).status is SimStatus.COMPLETED


def test_duplicate_execution_is_a_noop(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    f = client.app.state.session_factory
    r = runs.run_now(f, sid, "tester", RunLimits())
    first = impact_count(client, r.id)
    runs.execute_run(f, r.id, RunLimits())
    assert impact_count(client, r.id) == first and fetch(client, r.id).status is SimStatus.COMPLETED


def test_one_active_run_per_scenario_and_global_cap(client):
    w = chain(client)
    s1 = new_scenario(client, [sup_event(w["a"])], name="one")
    s2 = new_scenario(client, [sup_event(w["b"])], name="two")
    f = client.app.state.session_factory
    with f() as s, s.begin():
        runs.create_run(s, s.get(Scenario, s1), "tester", RunLimits(max_active_runs=1))
    with f() as s:
        with pytest.raises(ConflictError):
            runs.create_run(
                s, s.get(Scenario, s1), "tester", RunLimits(max_active_runs=5)
            )  # same scenario: already queued
        s.rollback()
        with pytest.raises(ConflictError, match="already queued or running"):
            runs.create_run(s, s.get(Scenario, s2), "tester", RunLimits(max_active_runs=1))  # global cap


def test_stale_lock_is_released(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    f = client.app.state.session_factory
    with f() as s, s.begin():
        run, _ = runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits())
        run.status, run.heartbeat_at, run.queued_at = (
            SimStatus.RUNNING,
            utcnow() - timedelta(hours=2),
            utcnow() - timedelta(hours=2),
        )
        stale = run.id
    new = fetch(client, runs.run_now(f, sid, "tester", RunLimits()).id)
    old = fetch(client, stale)
    assert (
        old.status is SimStatus.FAILED and old.error == "abandoned: no heartbeat" and new.status is SimStatus.COMPLETED
    )


def test_idempotency_key_replays_and_rejects_foreign_reuse(client):
    w = chain(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    other = new_scenario(client, [sup_event(w["b"])], name="other")
    f = client.app.state.session_factory
    with f() as s, s.begin():
        r1, c1 = runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits(), idempotency_key="key-000001")
        id1 = r1.id
    with f() as s, s.begin():
        r2, c2 = runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits(), idempotency_key="key-000001")
        assert (c1, c2, r2.id) == (True, False, id1)
    with f() as s:
        with pytest.raises(ConflictError):
            runs.create_run(s, s.get(Scenario, other), "tester", RunLimits(), idempotency_key="key-000001")
        s.rollback()
        with pytest.raises(ConflictError):
            runs.create_run(s, s.get(Scenario, sid), "someone-else", RunLimits(), idempotency_key="key-000001")


def test_invalid_scenario_cannot_start(client):
    w = chain(client)
    sid = new_scenario(
        client, [{"event_type": "SUPPLIER_CAPACITY_REDUCTION", "target_type": "SUPPLIER", "target_id": w["a"]["id"]}]
    )
    with client.app.state.session_factory() as s, pytest.raises(DomainValidationError, match="not valid"):
        runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits())


def test_relationship_removed_between_validation_and_run_is_recorded(client):
    w = chain(client)
    rels = client.get("/api/v1/graph/relationships", params={"limit": 10}).json()["items"]
    ref = next(r for r in rels if r["type"] == "TRANSFERS_TO")["ref"]
    sid = new_scenario(client, [{"event_type": "RELATIONSHIP_UNAVAILABLE", "relationship_ref": ref}])
    f = client.app.state.session_factory
    with f() as s, s.begin():
        run, _ = runs.create_run(s, s.get(Scenario, sid), "tester", RunLimits())
        rid = run.id
    row = next(r for r in rels if r["ref"] == ref)
    st = client.post(
        f"/api/v1/graph/relationships/{ref}/status",
        json={"status": "INACTIVE", "reason": "synthetic", "expected_version": row["version"]},
    )
    assert st.status_code == 200, st.text
    runs.execute_run(f, rid, RunLimits())
    r = fetch(client, rid)
    assert (
        r.status is SimStatus.PARTIAL
        and r.failures
        and r.failures[0]["error"].startswith("relationship_missing_or_not_confirmed")
    )
    _ = w
