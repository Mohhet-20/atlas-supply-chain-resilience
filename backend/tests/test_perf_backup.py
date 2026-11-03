"""Performance: backup, verification and an actual restore of a migrated, populated database (the recovery claim rests on this)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from app.db.migrations_check import assert_schema_current, head_revision
from app.db.session import create_db_engine
from tests.integrations_helpers import API, admin, client_for, mk_integration

pytestmark = pytest.mark.sqlite_only  # SQLite files, PRAGMAs and DDL; see conftest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_db.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("backup_db", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backup_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_backup_verify_and_restore_round_trip(settings, engine, tmp_path, tool):
    db_file = Path(engine.url.database)
    with client_for(settings, engine) as c:
        mk_integration(c)
        assert c.get(f"{API}/integrations", headers=admin()).json()["total"] == 1
    out = tmp_path / "bk" / "atlas.db"
    rep = tool.backup(db_file, out)
    assert rep["integrity"] == "ok" and rep["revision"] == head_revision() and len(rep["sha256"]) == 64
    assert rep["row_counts"]["integrations"] == 1 and not (tmp_path / "bk" / "atlas.db.partial").exists()
    target = tmp_path / "restored" / "atlas.db"
    restored = tool.restore(out, target)
    assert restored["row_counts"] == rep["row_counts"]
    eng = create_db_engine(f"sqlite:///{target}")
    try:
        assert assert_schema_current(eng) == head_revision()  # the restored database is migrated and usable
    finally:
        eng.dispose()


def test_restore_refuses_to_overwrite_and_rejects_corrupt_backups(settings, engine, tmp_path, tool):
    db_file = Path(engine.url.database)
    out = tmp_path / "b.db"
    tool.backup(db_file, out)
    with pytest.raises(FileExistsError):
        tool.restore(out, db_file)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        tool.restore(bad, tmp_path / "x.db")
    assert not (tmp_path / "x.db").exists()
    assert tool.main(["verify", "--file", str(tmp_path / "missing.db")]) == 1
