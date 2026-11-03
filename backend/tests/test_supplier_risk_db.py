"""Migration 0005 -> 0006, database constraints, and query-count bounds for the supplier-risk read models."""

from __future__ import annotations

import pathlib
import uuid

import pytest
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from alembic import command
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from tests.graph_helpers import node, ok
from tests.supplier_risk_helpers import R, assess_all

RISK_TABLES = {"risk_runs", "risk_assessments", "risk_factors", "risk_evidence", "risk_reviews", "risk_alerts"}


def test_upgrade_from_0005_keeps_existing_data_and_adds_empty_risk_tables(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0005")
    eng = create_db_engine(url)
    assert RISK_TABLES.isdisjoint(inspect(eng).get_table_names())
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO suppliers (supplier_code, name, name_normalized, country_or_region, category, status, provenance, version, created_at, updated_at) "
                "VALUES ('S-1','Legacy Supplier','legacy supplier','Germany','Parts','ACTIVE','USER_PROVIDED',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
    eng.dispose()
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    insp = inspect(eng)
    assert RISK_TABLES <= set(insp.get_table_names())
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM suppliers")).scalar() == "Legacy Supplier"
        for t in RISK_TABLES:
            assert (
                c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() == 0  # noqa: S608 - constant table names
            )  # nothing is invented for existing suppliers
        version = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0023"
    assert "uq_risk_assessments_current_supplier" in {i["name"] for i in insp.get_indexes("risk_assessments")}
    trig = {r[0] for r in eng.connect().execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert {"trg_risk_reviews_no_update", "trg_risk_reviews_no_delete"} <= trig
    eng.dispose()


def test_downgrade_to_0005_removes_only_the_risk_tables_and_upgrades_again(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005")
    eng = create_db_engine(url)
    names = set(inspect(eng).get_table_names())
    assert RISK_TABLES.isdisjoint(names) and "quality_scores" in names and "suppliers" in names
    eng.dispose()
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    assert RISK_TABLES <= set(inspect(eng).get_table_names())
    eng.dispose()


def _run(c, key="NETWORK", status="QUEUED"):
    c.execute(
        text(
            "INSERT INTO risk_runs (run_uid, scope_type, scope_ref, scope_key, status, trigger, requested_by, queued_at, examined, assessed, partial, "
            "insufficient, created, unchanged, failed, skipped, alerts_created, alerts_updated, failures, truncated, truncation_reasons) "
            "VALUES (:uid, 'NETWORK', '{}', :key, :status, 'API', 't', CURRENT_TIMESTAMP, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '[]', FALSE, '[]')"
        ),
        {"uid": f"RR-{uuid.uuid4().hex[:16]}", "key": key, "status": status},
    )


def test_only_one_queued_or_running_run_per_scope_in_the_database(engine):
    with engine.begin() as c:
        _run(c, "NETWORK", "QUEUED")
        _run(c, "SUPPLIER:1", "RUNNING")  # a different scope is fine
        _run(c, "NETWORK", "COMPLETED")  # finished runs do not hold the slot
        _run(c, "NETWORK", "COMPLETED")
    with pytest.raises(IntegrityError), engine.begin() as c:
        _run(c, "NETWORK", "RUNNING")


def test_score_and_status_consistency_constraints(engine, client):
    two = [node(client, "suppliers", "A"), node(client, "suppliers", "B")]
    ok(client, "SUPPLIES", two[0], node(client, "facilities", "F"))
    client.app.state.settings.risk_auto_reassess = False
    run = assess_all(client)
    with engine.connect() as c:
        rid = c.execute(text("SELECT id FROM risk_assessments WHERE status='INSUFFICIENT_EVIDENCE' LIMIT 1")).scalar()
    assert rid is not None and run.examined == 2
    for sql in (
        "UPDATE risk_assessments SET risk_score = 5 WHERE id = :i",  # insufficient evidence must not carry a score
        "UPDATE risk_assessments SET classification = 'LOW' WHERE id = :i",
        "UPDATE risk_assessments SET evidence_coverage = 1.5 WHERE id = :i",
    ):
        with pytest.raises(IntegrityError), engine.begin() as c:
            c.execute(text(sql), {"i": rid})
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "UPDATE risk_factors SET score = 50 WHERE id = (SELECT id FROM risk_factors WHERE state != 'SCORED' LIMIT 1)"
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text(
                "UPDATE risk_factors SET score = 101 WHERE id = (SELECT id FROM risk_factors WHERE state = 'SCORED' LIMIT 1)"
            )
        ) if False else c.execute(text("UPDATE risk_factors SET score = 101, state = 'SCORED'"))


def test_review_rows_cannot_be_updated_or_deleted_by_raw_sql(engine, client):
    client.app.state.settings.risk_auto_reassess = False
    a = node(client, "suppliers", "Reviewed")
    ok(client, "SUPPLIES", a, node(client, "facilities", "FF"))
    assess_all(client)
    aid = client.get(f"{R}/suppliers/{a['id']}").json()["assessment"]["id"]
    assert (
        client.post(
            f"{R}/suppliers/{a['id']}/reviews", json={"assessment_id": aid, "action": "ACKNOWLEDGE"}
        ).status_code
        == 201
    )
    for sql in ("UPDATE risk_reviews SET actor = 'x'", "DELETE FROM risk_reviews"):
        with pytest.raises(DatabaseError), engine.begin() as c:
            c.execute(text(sql))


# ------------------------------------------------------------------------------------------------ query counts


def _count_queries(client, path: str) -> int:
    eng = client.app.state.engine
    n = {"q": 0}

    def hook(*_a, **_k):
        n["q"] += 1

    event.listen(eng, "before_cursor_execute", hook)
    try:
        r = client.get(f"{R}{path}")
        assert r.status_code == 200, r.text
    finally:
        event.remove(eng, "before_cursor_execute", hook)
    return n["q"]


def test_list_overview_and_geography_do_not_issue_a_query_per_supplier(client):
    client.app.state.settings.risk_auto_reassess = False
    f = node(client, "facilities", "Shared Plant")
    for i in range(4):
        ok(client, "SUPPLIES", node(client, "suppliers", f"Small {i}"), f)
    assess_all(client)
    small = {p: _count_queries(client, p) for p in ("/suppliers?limit=100", "/overview", "/geography")}
    for i in range(30):
        ok(client, "SUPPLIES", node(client, "suppliers", f"Large {i}"), f)
    assess_all(client)
    large = {p: _count_queries(client, p) for p in ("/suppliers?limit=100", "/overview", "/geography")}
    assert large == small, (small, large)  # constant in the number of suppliers


def test_fact_loading_is_batched_so_its_query_count_does_not_grow_with_the_number_of_suppliers(client):
    from datetime import date

    from sqlalchemy.orm import Session

    from app.risk import inputs

    client.app.state.settings.risk_auto_reassess = False
    f = node(client, "facilities", "Busy Plant")
    ids = [node(client, "suppliers", f"Bulk {i}", country_or_region="Chile")["id"] for i in range(25)]
    for sid in ids:
        ok(client, "SUPPLIES", {"type": "SUPPLIER", "id": sid}, f)
    eng = client.app.state.engine
    counts: list[int] = []
    for subset in (ids[:3], ids):
        n = {"q": 0}

        def hook(*_a, _n=n):
            _n["q"] += 1

        event.listen(eng, "before_cursor_execute", hook)
        try:
            with Session(eng) as s:
                facts, _net = inputs.load_facts(s, subset, date(2026, 10, 22))
        finally:
            event.remove(eng, "before_cursor_execute", hook)
        assert len(facts) == len(subset)
        counts.append(n["q"])
    assert counts[1] <= counts[0] + 2, counts  # batched: more suppliers never means more queries


def test_a_network_run_does_a_bounded_number_of_edge_reads_per_supplier(client):
    client.app.state.settings.risk_auto_reassess = False
    f = node(client, "facilities", "Busy Plant")
    for i in range(20):
        ok(client, "SUPPLIES", node(client, "suppliers", f"Bulk {i}"), f)
    eng = client.app.state.engine
    n = {"reads": 0}

    def hook(_c, _cur, statement, *_a):
        if "FROM supply_relationships" in statement and statement.lstrip().upper().startswith("SELECT"):
            n["reads"] += 1

    event.listen(eng, "before_cursor_execute", hook)
    try:
        run = assess_all(client)
    finally:
        event.remove(eng, "before_cursor_execute", hook)
    assert run.examined == 20
    assert n["reads"] <= 3 * 20 + 5, n  # one batched load plus a constant number of traversal reads per supplier
