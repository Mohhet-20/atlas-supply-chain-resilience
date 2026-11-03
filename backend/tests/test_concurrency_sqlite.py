"""SQLite allows one writer. These tests hold a real write lock from a second connection while the API works, as
the load test (scripts/load_test.py) did, and check the API's behaviour: writers queue, a writer that waits too long
gets a clean 503 with Retry-After (never a 500 or a dropped connection), and reads with a session token keep working.
SYNTHETIC data."""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.db.session import create_db_engine
from tests.registry_helpers import API, make_settings
from tests.security_helpers import bearer, make_app, mk_user

pytestmark = pytest.mark.sqlite_only  # SQLite files, PRAGMAs and DDL; see conftest

BODY = {"name": "Queued Supplier", "country_or_region": "Germany", "category": "Electronics"}


def _hold_write_lock(path: str, seconds: float) -> threading.Thread:
    ready = threading.Event()

    def hold() -> None:
        con = sqlite3.connect(path, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        ready.set()
        time.sleep(seconds)
        con.execute("ROLLBACK")
        con.close()

    t = threading.Thread(target=hold)
    t.start()
    ready.wait()
    return t


def _app(settings, db_url, busy_ms):
    engine = create_db_engine(db_url, settings=make_settings(settings, sqlite_busy_timeout_ms=busy_ms))
    return make_app(settings, engine), engine


def test_a_writer_waits_for_a_competing_writer_instead_of_failing(settings, db_url):
    app, engine = _app(settings, db_url, busy_ms=5000)
    with TestClient(app) as c:
        mk_user(c, "writer", "editor")
        h = bearer(c, "writer")
        holder = _hold_write_lock(db_url.removeprefix("sqlite:///"), 0.5)
        r = c.post(f"{API}/suppliers", json=BODY, headers=h)
        holder.join()
        assert r.status_code == 201, r.text
    engine.dispose()


def test_a_writer_that_waits_too_long_gets_503_and_reads_keep_working(settings, db_url):
    app, engine = _app(settings, db_url, busy_ms=150)
    with TestClient(app, raise_server_exceptions=False) as c:
        mk_user(c, "writer", "editor")
        h = bearer(c, "writer")
        path = db_url.removeprefix("sqlite:///")
        # make the next request's session-activity write due, so the read below also tries to write
        con = sqlite3.connect(path)
        con.execute("UPDATE auth_sessions SET last_seen_at = datetime(last_seen_at, '-5 minutes')")
        con.commit()
        con.close()
        holder = _hold_write_lock(path, 1.5)
        try:
            w = c.post(f"{API}/suppliers", json=BODY, headers=h)
            read = c.get("/api/v1/suppliers", headers=h)
        finally:
            holder.join()
        assert w.status_code == 503, w.text
        assert w.headers["Retry-After"] == "1"
        assert w.json()["error"]["code"] == "database_busy"
        assert read.status_code == 200, read.text  # the skipped activity write did not fail the read
    engine.dispose()
