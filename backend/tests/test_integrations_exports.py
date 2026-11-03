"""Integrations: asynchronous exports - authorisation, tenancy, expiry, formula-injection protection, truncation, no path traversal."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.integrations import exports
from app.models.integrations import ExportJob
from app.models.types import utcnow
from tests.integrations_helpers import API, admin, client_for, enable, h, mk_integration, mk_org, mk_user
from tests.security_helpers import bearer

EX = f"{API}/exports"


@pytest.fixture
def c(settings, engine):
    with client_for(settings, engine) as client:
        yield client


def make(c, headers=None, **body):
    r = c.post(EX, json={"dataset": "integration_runs", "format": "csv"} | body, headers=headers or h("editor"))
    assert r.status_code == 202, r.text
    return r.json()


def finish(c, uid, headers=None):
    return c.get(f"{EX}/{uid}", headers=headers or h("editor")).json()


def seed_runs(c):
    uid = mk_integration(c, config={"records": 5})["integration_uid"]
    enable(c, uid)
    assert c.post(f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=admin()).status_code == 202


def test_export_runs_in_the_background_and_downloads_with_safe_headers(c):
    seed_runs(c)
    job = make(c)
    done = finish(c, job["export_uid"])
    assert done["status"] == "ready" and done["row_count"] == 1 and done["expires_at"]
    r = c.get(done["download_url"], headers=h("editor"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert r.text.splitlines()[0].startswith("run_uid,mode")
    js = make(c, format="json")
    body = c.get(finish(c, js["export_uid"])["download_url"], headers=h("editor")).json()
    assert body["dataset"] == "integration_runs" and body["truncated"] is False and len(body["rows"]) == 1


def test_authorisation_viewers_cannot_export_and_anonymous_is_refused(c):
    assert c.post(EX, json={"dataset": "integration_runs"}, headers=h("viewer")).status_code == 403
    assert c.get(EX, headers=h("viewer")).status_code == 403
    assert c.get(EX).status_code == 401
    job = make(c)
    assert c.get(f"{EX}/{job['export_uid']}/download", headers=h("viewer")).status_code == 403


def test_validation_of_dataset_format_and_filters(c):
    for body in (
        {"dataset": "users"},
        {"dataset": "../../etc/passwd"},
        {"dataset": "alerts", "format": "xlsx"},
        {"dataset": "alerts", "filters": {"password": "x"}},
        {"dataset": "alerts", "unknown": 1},
    ):
        assert c.post(EX, json=body, headers=h("editor")).status_code == 422, body
    assert c.get(f"{EX}/datasets", headers=h("editor")).json()["items"]


def test_expired_exports_are_refused_and_swept(c):
    job = make(c)
    uid = job["export_uid"]
    path = exports.exports_dir(c.app.state.settings) / f"{uid}.csv"
    assert path.is_file()
    with c.app.state.session_factory() as db:
        db.scalar(
            select(ExportJob).where(ExportJob.export_uid == uid).execution_options(skip_tenant=True)
        ).expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    assert c.get(f"{EX}/{uid}/download", headers=h("editor")).status_code == 409
    assert exports.sweep_expired(c.app.state.session_factory, c.app.state.settings) == 1
    assert not path.exists() and finish(c, uid)["status"] == "expired"


def test_csv_cells_that_look_like_formulas_are_neutralised(c, monkeypatch):
    uid = mk_integration(c, name='=HYPERLINK("http://evil","x")', config={"records": 1})["integration_uid"]
    enable(c, uid)
    c.post(f"{API}/integrations/{uid}/sync", json={"mode": "full"}, headers=admin())
    job = make(c, dataset="alerts")  # alerts carry free text from sources; the helper is shared by every dataset
    assert finish(c, job["export_uid"])["status"] == "ready"
    from app.ingestion.export import neutralize

    assert (
        neutralize("=1+1").startswith("'")
        and neutralize("@SUM(A1)").startswith("'")
        and neutralize("-2+3").startswith("'")
    )
    raw = exports.exports_dir(c.app.state.settings) / f"{job['export_uid']}.csv"
    assert not any(line.startswith(("=", "@", "+")) for line in raw.read_text().splitlines())


def test_large_exports_are_truncated_and_say_so(settings, engine):
    with client_for(settings, engine, export_max_rows=3) as c:
        seed = mk_integration(c, config={"records": 5})["integration_uid"]
        enable(c, seed)
        for _ in range(5):
            from tests.test_integrations_sync import execute, start

            execute(c, start(c, seed), __import__("tests.test_integrations_sync", fromlist=["Scripted"]).Scripted())
        job = make(c, format="json")
        done = finish(c, job["export_uid"])
        assert done["row_count"] == 3 and "truncated" in (done["error"] or "")
        body = c.get(done["download_url"], headers=h("editor")).json()
        assert body["truncated"] is True and len(body["rows"]) == 3


def test_tenant_isolation_including_domain_datasets(settings, engine):
    with client_for(settings, engine) as c:
        org2 = mk_org(c, "umbrella")
        mk_user(c, "bob", "editor", org_id=org2)
        mk_user(c, "alice", "editor")
        a, b = bearer(c, "alice"), bearer(c, "bob")
        job = make(c, headers=a)
        uid = job["export_uid"]
        assert c.get(EX, headers=b).json()["total"] == 0
        assert c.get(f"{EX}/{uid}", headers=b).status_code == 404
        assert c.get(f"{EX}/{uid}/download", headers=b).status_code == 404
        # domain datasets are organisation-scoped (migration 0022): each organisation exports only its own suppliers
        base = {"country_or_region": "Germany", "category": "Electronics"}
        assert c.post("/api/v1/registry/suppliers", json=base | {"name": "Alice Metals"}, headers=a).status_code == 201
        assert c.post("/api/v1/registry/suppliers", json=base | {"name": "Bob Castings"}, headers=b).status_code == 201
        for headers, mine, theirs in ((a, "Alice Metals", "Bob Castings"), (b, "Bob Castings", "Alice Metals")):
            r = c.post(EX, json={"dataset": "suppliers", "format": "json"}, headers=headers)
            assert r.status_code == 202, r.text
            done = finish(c, r.json()["export_uid"], headers=headers)
            text = c.get(done["download_url"], headers=headers).text
            assert mine in text and theirs not in text


def test_download_cannot_be_pointed_outside_the_exports_directory(c):
    job = make(c)
    with c.app.state.session_factory() as db:
        db.scalar(
            select(ExportJob).where(ExportJob.export_uid == job["export_uid"]).execution_options(skip_tenant=True)
        ).file_name = "../../etc/passwd"
        db.commit()
    assert c.get(f"{EX}/{job['export_uid']}/download", headers=h("editor")).status_code in (404, 409)
