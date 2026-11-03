"""Migration 0006 -> 0007, constraints, append-only history and measured (not asserted-as-scale) performance."""

from __future__ import annotations

import pathlib
import time

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from app.models.enums import ScenarioStatus, ScenarioType, SimStatus
from app.models.scenario import Scenario, ScenarioHistory, SimulationRun
from app.scenarios.engine import EngineLimits
from tests.graph_helpers import node, ok
from tests.scenario_helpers import chain, simulate, sup_out

TABLES = {
    "scenarios",
    "scenario_events",
    "scenario_history",
    "simulation_runs",
    "simulation_impacts",
    "simulation_edge_impacts",
    "simulation_alternatives",
}


def test_upgrade_from_0006_keeps_data_and_adds_empty_scenario_tables(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0006")
    eng = create_db_engine(url)
    assert TABLES.isdisjoint(inspect(eng).get_table_names())
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppliers (supplier_code, name, name_normalized, country_or_region, category, status, provenance, version, created_at, updated_at) VALUES ('S-1','Legacy Supplier','legacy supplier','Germany','Parts','ACTIVE','USER_PROVIDED',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
    eng.dispose()
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    insp = inspect(eng)
    assert TABLES <= set(insp.get_table_names())
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM suppliers")).scalar() == "Legacy Supplier"
        for t in TABLES:
            assert c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0  # noqa: S608 - nothing is seeded or invented
        trig = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert {"trg_scenario_history_no_update", "trg_scenario_history_no_delete"} <= trig
    assert "uq_simulation_runs_active_scenario" in {i["name"] for i in insp.get_indexes("simulation_runs")}
    command.downgrade(cfg, "0006")
    assert TABLES.isdisjoint(inspect(eng).get_table_names())
    eng.dispose()


def _scenario(s: Session) -> Scenario:
    sc = Scenario(
        scenario_uid="SC-T",
        name="t",
        scenario_type=ScenarioType.CUSTOM,
        status=ScenarioStatus.DRAFT,
        version=1,
        assumptions={},
        config_version="sc-1",
        owner="t",
        updated_by="t",
    )
    s.add(sc)
    s.flush()
    return sc


def _run(sc: Scenario, uid: str, status: SimStatus, key: str | None = None) -> SimulationRun:
    return SimulationRun(
        run_uid=uid,
        scenario_id=sc.id,
        scenario_version=1,
        definition_hash="h",
        scenario_snapshot={},
        status=status,
        requested_by="t",
        idempotency_key=key,
    )


def test_one_active_run_per_scenario_is_enforced_by_the_database(client):
    with Session(client.app.state.engine) as s:
        sc = _scenario(s)
        s.add(_run(sc, "SR-1", SimStatus.QUEUED))
        s.flush()
        s.add(_run(sc, "SR-2", SimStatus.COMPLETED))  # finished runs are unlimited
        s.add(_run(sc, "SR-3", SimStatus.FAILED))
        s.flush()
        s.add(_run(sc, "SR-4", SimStatus.RUNNING))
        with pytest.raises(IntegrityError):
            s.flush()


def test_idempotency_key_unique_and_history_is_append_only(client):
    with Session(client.app.state.engine) as s:
        sc = _scenario(s)
        s.add(_run(sc, "SR-1", SimStatus.COMPLETED, "key-1"))
        s.flush()
        s.add(_run(sc, "SR-2", SimStatus.COMPLETED, "key-1"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        sc = _scenario(s)
        h = ScenarioHistory(scenario_id=sc.id, version=1, action="x", actor="t", summary="s", definition={})
        s.add(h)
        s.flush()
        h.summary = "changed"
        with pytest.raises(ValueError, match="append-only"):
            s.flush()
        s.rollback()
        sc = _scenario(s)
        s.add(ScenarioHistory(scenario_id=sc.id, version=1, action="x", actor="t", summary="s", definition={}))
        s.commit()
        with pytest.raises(DatabaseError):
            s.execute(text("DELETE FROM scenario_history"))


def test_benchmark_synthetic_network_is_measured_and_bounded(client, capsys):
    """Builds a layered synthetic network and records timings. The assertions are generous bounds, not scale claims."""
    root = node(client, "suppliers", "Root Supplier")
    layer = [root]
    count = 0
    t_build = time.perf_counter()
    for depth in range(3):
        nxt = []
        for parent in layer[:6]:
            for _n in range(4):
                kind = "facilities"
                child = node(client, kind, f"L{depth}-{count}")
                count += 1
                ok(
                    client,
                    "SUPPLIES" if parent["type"] == "SUPPLIER" else "TRANSFERS_TO",
                    parent,
                    child,
                    supply_share_pct=25.0,
                )
                nxt.append(child)
        layer = nxt
    build_s = time.perf_counter() - t_build
    t = time.perf_counter()
    r = simulate(client, [sup_out(root)], EngineLimits(), downstream_depth=4)
    elapsed = time.perf_counter() - t
    print(
        f"BENCH nodes_created={count + 1} build_s={build_s:.2f} impacted={len(r.impacts)} edges={len(r.edge_impacts)} engine_s={elapsed:.3f} truncated={r.truncated}"
    )
    assert len(r.impacts) == count + 1 and elapsed < 5.0
    capped = simulate(client, [sup_out(root)], EngineLimits(max_nodes=20), downstream_depth=4)
    assert capped.truncated and len(capped.impacts) <= 20


def test_query_count_is_bounded_per_level_not_per_node(client):
    w = chain(client)
    from sqlalchemy import event as sa_event

    counts = {"n": 0}
    eng = client.app.state.engine

    def count(*_a):
        counts["n"] += 1

    sa_event.listen(eng, "before_cursor_execute", count)
    try:
        simulate(client, [sup_out(w["a"])])
    finally:
        sa_event.remove(eng, "before_cursor_execute", count)
    assert counts["n"] < 60  # a handful of batched queries for a 4-entity result, not one per node/edge
