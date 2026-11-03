from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import inspect, text

from alembic import command
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine

pytestmark = pytest.mark.sqlite_only  # SQLite files, PRAGMAs and DDL; see conftest


def test_upgrade_from_0004_preserves_existing_findings(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'm.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "0004")
    eng = create_db_engine(url)
    with eng.begin() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(data_quality_findings)"))}
    assert "dimension" not in cols
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO data_quality_findings (finding_key, rule_id, severity, message, resolution_status, created_at) VALUES ('k1','DQ002','ERROR','legacy finding','OPEN', CURRENT_TIMESTAMP), ('k2','DQ003','WARNING','legacy closed','WONT_FIX', CURRENT_TIMESTAMP)"
            )
        )
    eng.dispose()
    command.upgrade(cfg, "head")
    eng = create_db_engine(url)
    with eng.connect() as c:
        row = c.execute(
            text("SELECT rule_id, message, resolution_status, origin, version FROM data_quality_findings ORDER BY id")
        ).all()
    assert [r[2] for r in row] == ["OPEN", "WONT_FIX"]
    row = row[0]
    assert row[0] == "DQ002" and row[1] == "legacy finding" and row[2] == "OPEN"
    assert row[3] == "INGESTION" and row[4] == 1
    insp = inspect(eng)
    assert {"quality_eval_runs", "quality_scores", "quality_finding_events", "quality_rule_settings"} <= set(
        insp.get_table_names()
    )
    idx = {i["name"] for i in insp.get_indexes("data_quality_findings")}
    assert "uq_data_quality_findings_eval_key" in idx
    eng.dispose()


def test_downgrade_to_0004_and_back(tmp_path: pathlib.Path):
    url = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    cfg = alembic_config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004")
    eng = create_db_engine(url)
    assert "quality_scores" not in inspect(eng).get_table_names()
    eng.dispose()
    command.upgrade(cfg, "head")
