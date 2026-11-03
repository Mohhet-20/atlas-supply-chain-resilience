"""The synthetic demo world is built by the real engines in its own database and never touches the app database."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_world.py"


def _run(*args: str):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=300)  # noqa: S603


def test_demo_world_builds_and_persists_engine_outputs(tmp_path):
    db = tmp_path / "demo.db"
    r = _run("--db", str(db))
    assert r.returncode == 0, r.stderr[-800:]
    assert "DEMO WORLD (synthetic)" in r.stdout
    con = sqlite3.connect(db)
    try:
        names = [n for (n,) in con.execute("select name from suppliers")]
        assert names and all(n.startswith("DEMO ") for n in names)
        assert con.execute("select count(*) from risk_assessments where is_current = 1").fetchone()[0] == 3
        assert con.execute("select count(*) from impact_findings").fetchone()[0] > 0
        assert con.execute("select count(*) from stockout_findings").fetchone()[0] == 2
        # every demo registry record is labelled synthetic, never user-provided
        for table in ("suppliers", "facilities", "products", "supply_relationships"):
            assert {p for (p,) in con.execute(f"select distinct provenance from {table}")} == {"SYNTHETIC_DEMO"}  # noqa: S608
        # the confirmed inbound order is visible to the as-of analyses (it used to be dropped by its import timestamp)
        assert "'inventory_position': 290.0" in r.stdout
        # alert rules evaluated over engine outputs raise real, labelled alerts
        assert con.execute("select count(*) from alerts").fetchone()[0] == 2
    finally:
        con.close()
    # rerun with --reset starts from an empty database again (idempotent, repeatable)
    assert _run("--db", str(db), "--reset").returncode == 0


def test_demo_world_refuses_the_application_database_name(tmp_path):
    r = _run("--db", str(tmp_path / "atlas.db"))
    assert r.returncode == 2 and "never share the application database" in r.stderr
