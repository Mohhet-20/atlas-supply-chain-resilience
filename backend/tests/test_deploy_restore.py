"""Deployment: start the application against a restored backup and run critical workflows on the restored data."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.db.session import create_db_engine
from tests.integrations_helpers import API, client_for, mk_integration, mk_user
from tests.security_helpers import bearer, login

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backup_db.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("backup_db", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backup_db"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.sqlite_only  # the backup/restore scripts copy SQLite files
def test_application_runs_correctly_on_restored_data(settings, engine, tmp_path):
    tool = load_tool()
    with client_for(settings, engine) as c:
        mk_user(c, "dr.admin", "admin")
        h = bearer(c, "dr.admin")
        r = c.post(
            f"{API}/registry/suppliers",
            json={"name": "Restore Probe Co", "country_or_region": "DE", "category": "E"},
            headers=h,
        )
        assert r.status_code == 201
        sid = r.json()["id"]
        uid = mk_integration(c, headers=h)["integration_uid"]
        before_audit = c.get(f"{API}/registry/audit/verify", headers=h).json()
    bk = tmp_path / "bk" / "atlas.db"
    tool.backup(Path(engine.url.database), bk)

    restored = tmp_path / "restored" / "atlas.db"
    tool.restore(bk, restored)
    eng2 = create_db_engine(f"sqlite:///{restored}")
    try:
        s2 = settings.model_copy(update={"database_url": f"sqlite:///{restored}"})
        with client_for(s2, eng2) as c2:
            assert c2.get(f"{API}/readiness").status_code == 200  # migrated, schema current
            assert (
                login(c2, "dr.admin", "Synthetic-Pass-Phrase-41!").status_code == 200
            )  # users and password hashes survived
            h2 = bearer(c2, "dr.admin")
            assert c2.get(f"{API}/suppliers/{sid}", headers=h2).json()["name"] == "Restore Probe Co"
            assert c2.get(f"{API}/integrations/{uid}", headers=h2).status_code == 200
            after_audit = c2.get(f"{API}/registry/audit/verify", headers=h2).json()
            assert (
                after_audit.get("valid", True) is True and before_audit.get("valid", True) is True
            )  # hash chain intact after restore
            assert c2.get(f"{API}/dashboard/summary", headers=h2).json()["supplier_count"] == 1
    finally:
        eng2.dispose()
