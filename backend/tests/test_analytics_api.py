"""Analytics API: roles, strict parameters, empty versus zero, filter consistency, real risk/stockout/run data, windows, cache, export. Synthetic data only."""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.main import create_app
from app.models.decision import DecisionCase
from app.models.enums import SimStatus
from app.models.registry import AuditEvent
from app.models.resilience import ResilienceRun
from app.models.supplier import Supplier
from tests.decision_helpers import alt, run
from tests.inventory_helpers import populate, run_baseline, world
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings
from tests.supplier_risk_helpers import assess_all

A = "/api/v1/analytics"


@pytest.fixture
def secured(settings, engine):
    with TestClient(
        create_app(
            make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, analytics_cache_ttl_seconds=0), engine
        )
    ) as c:
        yield c


@pytest.fixture
def cached(settings, engine):
    with TestClient(
        create_app(
            make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, analytics_cache_ttl_seconds=60), engine
        )
    ) as c:
        yield c


def val(client, area, mid, role="viewer", **params):
    r = client.get(f"{A}/area/{area}", params=params, headers=h(role))
    assert r.status_code == 200, r.text
    return next(m for m in r.json()["metrics"] if m["id"] == mid)


def test_roles_registry_and_unavailable_list(secured):
    assert secured.get(f"{A}/health").status_code == 401
    j = secured.get(f"{A}/health", headers=h("viewer")).json()
    assert j["windows"] == [7, 30, 90] and {u["id"] for u in j["unavailable"]} >= {
        "ON_TIME_DELIVERY_RATE",
        "INVENTORY_VALUE",
        "COMPOSITE_HEALTH_SCORE",
    }
    assert "ORGANISATION_SCOPE" not in {u["id"] for u in j["unavailable"]}  # data is organisation-scoped
    m = secured.get(f"{A}/metrics", headers=h("viewer")).json()
    assert len(m["metrics"]) >= 25 and all(x["calculation"] and x["missing"] for x in m["metrics"])


def test_unknown_and_organisation_parameters_are_rejected(secured):
    for q in ("organisation_id=3", "tenant=x", "bogus=1"):
        r = secured.get(f"{A}/overview?{q}", headers=h("viewer"))
        assert r.status_code == 422, q
    assert secured.get(f"{A}/overview?window_days=45", headers=h("viewer")).status_code == 422
    assert secured.get(f"{A}/overview?risk_class=SEVERE", headers=h("viewer")).status_code == 422
    assert secured.get(f"{A}/alerts?limit=101", headers=h("viewer")).status_code == 422
    assert secured.get(f"{A}/area/finance", headers=h("viewer")).status_code == 422


def test_empty_database_reports_unknown_not_zero_and_makes_no_findings(secured):
    j = secured.get(f"{A}/overview", headers=h("viewer")).json()
    by = {m["id"]: m for m in j["metrics"]}
    assert by["INV_AT_RISK_PAIRS"]["state"] == "no_data" and by["INV_AT_RISK_PAIRS"]["value"] is None
    assert by["SCN_MAX_REACH"]["state"] == "no_data" and by["LOG_CONFIGURED_LEAD_TIME_MEDIAN"]["state"] == "no_data"
    assert by["RISK_HIGH_OR_CRITICAL"]["state"] == "no_data"
    assert (
        by["NET_SUPPLIERS_ACTIVE"]["value"] == 0 and by["SCN_RUNS"]["value"] == 0
    )  # a count of nothing is a true zero
    assert j["findings"] == [] and j["meta"]["timezone"] == "UTC"


def test_supplier_risk_distribution_matches_the_risk_engine_and_unknown_is_not_low(secured, client):
    w = world(client)
    assess_all(client)
    j = secured.get(f"{A}/suppliers", headers=h("viewer")).json()
    dist = next(m for m in j["metrics"] if m["id"] == "RISK_ASSESSED_SUPPLIERS")["breakdown"]
    with Session(secured.app.state.engine) as s:
        n = len(list(s.scalars(select(Supplier.id).where(Supplier.status == "ACTIVE"))))
    assert sum(dist.values()) == n >= 1
    assert j["table"]["total"] == n
    unk = [r for r in j["table"]["items"] if r["classification"] == "UNKNOWN"]
    assert all(r["risk_score"] is None or r["status"] != "INSUFFICIENT_EVIDENCE" for r in unk)
    assert all("not a prediction" in r["kind"] for r in j["table"]["items"])
    assert len(j["trend"]["points"]) == 31 and w


def test_global_filters_apply_consistently_across_views(secured, client):
    world(client)
    with Session(secured.app.state.engine) as s:
        s.execute(text("update suppliers set country_or_region='Germany' where id=(select min(id) from suppliers)"))
        s.execute(
            text(
                "insert into suppliers (supplier_code,name,country_or_region,category,status,provenance,name_normalized,version,created_at,updated_at) values ('Z9','Zed','Spain','Metals','ACTIVE','SYNTHETIC_DEMO','zed',1,'2026-01-01','2026-01-01')"
            )
        )
        s.commit()
    total = val(secured, "network", "NET_SUPPLIERS_ACTIVE")["value"]
    de = val(secured, "network", "NET_SUPPLIERS_ACTIVE", region="Germany")["value"]
    es = val(secured, "network", "NET_SUPPLIERS_ACTIVE", region="Spain")["value"]
    assert de + es == total and es == 1 and de >= 1
    ov = secured.get(f"{A}/overview?region=Spain", headers=h("viewer")).json()
    assert next(m for m in ov["metrics"] if m["id"] == "NET_SUPPLIERS_ACTIVE")["value"] == es
    sup = secured.get(f"{A}/suppliers?region=Spain", headers=h("viewer")).json()
    assert sup["table"]["total"] == es and sup["meta"]["applied_filters"]["region"] == "Spain"
    assert val(secured, "supplier_risk", "RISK_UNASSESSED_SUPPLIERS", region="Spain")["value"] == es
    # a filter a metric does not honour is reported, not silently applied
    meta = secured.get(f"{A}/area/inventory?region=Spain", headers=h("viewer")).json()["meta"]
    assert meta["ignored_filters"] == ["region"]
    only_unknown = secured.get(f"{A}/suppliers?risk_class=UNKNOWN", headers=h("viewer")).json()["table"]["total"]
    assert only_unknown == total


def test_inventory_uses_the_latest_real_stockout_analysis(secured, client):
    w = world(client)
    assert val(secured, "inventory", "INV_AT_RISK_PAIRS")["state"] == "no_data"
    populate(client, w)
    an = run_baseline(client)
    j = secured.get(f"{A}/inventory", headers=h("viewer")).json()
    by = {m["id"]: m for m in j["metrics"]}
    assert (
        by["INV_PAIRS_ANALYSED"]["state"] == "ok"
        and by["INV_PAIRS_ANALYSED"]["value"] == sum(by["INV_PAIRS_ANALYSED"]["breakdown"].values()) >= 3
    )
    assert by["INV_UNKNOWN_PAIRS"]["value"] == by["INV_PAIRS_ANALYSED"]["breakdown"].get("unknown", 0) + by[
        "INV_PAIRS_ANALYSED"
    ]["breakdown"].get("insufficient_data", 0)
    assert by["INV_POSITION_FRESHNESS"]["state"] == "ok"
    assert j["table"]["analysis"]["analysis_uid"] == an.analysis_uid and "recombine" in j["table"]["note"]
    assert all(r["status"] in ("current_shortage", "projected_stockout", "low_coverage") for r in j["table"]["items"])
    bd = by["INV_PAIRS_ANALYSED"]["breakdown"]
    only = secured.get(f"{A}/inventory?status=unknown", headers=h("viewer")).json()["table"]
    assert only["total"] == bd.get("unknown", 0) and all(r["status"] == "unknown" for r in only["items"])


def test_run_windows_comparison_and_unsuccessful_runs(secured, session):
    now = datetime.now(timezone.utc)
    run(session, "RR-NEW1", [alt("A", 10.0, 1.0)])
    run(session, "RR-NEW2", [alt("A", 10.0, 1.0)])
    run(session, "RR-OLD1", [alt("A", 10.0, 1.0)])
    run(session, "RR-INF", [alt("A", 10.0, 1.0)], status="infeasible")
    session.execute(
        text("update resilience_runs set queued_at=:t where run_uid='RR-OLD1'"),
        {"t": (now - timedelta(days=40)).replace(tzinfo=None)},
    )
    session.execute(
        text("update resilience_runs set queued_at=:t where run_uid='RR-NEW2'"),
        {"t": (now - timedelta(days=5)).replace(tzinfo=None)},
    )
    session.commit()
    m = val(secured, "optimization", "OPT_RESILIENCE_RUNS")
    assert (
        m["value"] == 3
        and m["comparison"]["previous"] == 1
        and m["comparison"]["verdict"] == "changed"
        and m["comparison"]["change_pct"] == 200.0
    )
    assert val(secured, "optimization", "OPT_RESILIENCE_RUNS", window_days=7)["value"] == 3
    assert val(secured, "optimization", "OPT_UNSUCCESSFUL_RUNS")["value"] == 1  # the infeasible one
    assert (
        val(secured, "optimization", "OPT_UNSUCCESSFUL_RUNS")["comparison"]["change_pct"] is None
    )  # previous window is 0
    session.rollback()
    d = secured.get(f"{A}/disruption", headers=h("viewer")).json()
    assert (
        "not active disruptions" in d["note"]
        and len(d["runs"]) == 4
        and all(r["link"].startswith("/resilience/runs/") for r in d["runs"])
    )
    assert any("approximate" in d["note"] for _ in [0])


def test_failed_resilience_run_counts_as_unsuccessful_not_zero(secured, session):
    r = run(session, "RR-F", [alt("A", 1.0, 1.0)])
    session.execute(text("update resilience_runs set status='FAILED' where run_uid='RR-F'"))
    session.commit()
    assert val(secured, "optimization", "OPT_UNSUCCESSFUL_RUNS")["value"] == 1
    assert val(secured, "optimization", "OPT_RESILIENCE_RUNS")["value"] == 0 and r.status is not None
    session.rollback()


def test_decision_queue_counts_come_from_cases(secured, session):
    for i, st in enumerate(("UNDER_REVIEW", "UNDER_REVIEW", "EVALUATED", "DRAFT")):
        session.add(DecisionCase(case_uid=f"DC-T{i}", title=f"t{i}", status=st, version=1, created_by="u"))
    session.commit()
    assert val(secured, "operations", "OPS_DECISIONS_UNDER_REVIEW")["value"] == 2
    assert val(secured, "operations", "OPS_DECISIONS_AWAITING_SUBMISSION")["value"] == 1
    f = secured.get(f"{A}/overview", headers=h("viewer")).json()["findings"]
    assert any(x["metric"] == "OPS_DECISIONS_UNDER_REVIEW" and x["kind"] == "finding" for x in f)
    session.rollback()


def test_network_is_structural_and_paginated(secured, client):
    world(client)
    j = secured.get(f"{A}/network?limit=1&offset=0", headers=h("viewer")).json()
    assert "not a quantified impact" in j["note"] and j["single_source"]["limit"] == 1
    assert len(j["single_source"]["items"]) <= 1 and "Structural" in j["single_source"]["note"]
    assert secured.get(f"{A}/network?limit=1&offset=99999", headers=h("viewer")).json()["single_source"]["items"] == []


def test_alerts_and_activity_roles(secured, session):
    run(session, "RR-ACT", [alt("A", 1.0, 1.0)])
    session.rollback()
    v = secured.get(f"{A}/activity", headers=h("viewer")).json()
    r = secured.get(f"{A}/activity", headers=h("reviewer")).json()
    assert v["audit_included"] is False and r["audit_included"] is True
    assert all(i["kind"] != "audit" for i in v["items"])
    assert any(i["kind"] == "resilience" for i in v["items"])
    al = secured.get(f"{A}/alerts", headers=h("viewer")).json()
    assert al["items"] == [] and "persisted" in al["priority"].lower()


def test_cache_is_scoped_by_role_and_filters_and_flagged(cached):
    a = cached.get(f"{A}/overview", headers=h("viewer")).json()
    b = cached.get(f"{A}/overview", headers=h("viewer")).json()
    c = cached.get(f"{A}/overview", headers=h("editor")).json()
    d = cached.get(f"{A}/overview?region=Nowhere", headers=h("viewer")).json()
    assert a["meta"]["cached"] is False and b["meta"]["cached"] is True
    assert c["meta"]["cached"] is False and d["meta"]["cached"] is False
    assert a["metrics"] == b["metrics"]


def test_export_needs_editor_is_filtered_audited_and_formula_safe(secured, client):
    w = world(client)
    assert secured.get(f"{A}/export?area=metrics", headers=h("viewer")).status_code == 403
    with Session(secured.app.state.engine) as s:
        s.execute(text("update suppliers set name='=HYPERLINK(\"http://x\")' where id=(select min(id) from suppliers)"))
        s.commit()
    r = secured.get(f"{A}/export?area=supplier_risk&format=csv", headers=h("editor"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][0].startswith("# ATLAS analytics export") and "registry" in rows[0][0]
    header, body = rows[1], rows[2:]
    assert "name" in header and all(not c.startswith(("=", "+", "@")) for row in body for c in row)
    assert any(row[header.index("name")].startswith("'=") for row in body)
    j = secured.get(f"{A}/export?area=metrics&format=json&region=Germany", headers=h("editor")).json()
    assert (
        j["meta"]["applied_filters"]["region"] == "Germany"
        and j["meta"]["rows"] == len(j["rows"])
        and j["rows"][0]["calculation"]
    )
    assert secured.get(f"{A}/export?area=bogus", headers=h("editor")).status_code == 422
    with Session(secured.app.state.engine) as s:
        acts = [a.action for a in s.scalars(select(AuditEvent))]
    assert acts.count("analytics.exported") == 2 and w


def test_export_row_cap_is_enforced(settings, engine, client):
    with TestClient(
        create_app(
            make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, analytics_export_max_rows=10), engine
        )
    ) as c:
        world(client)
        j = c.get(f"{A}/export?area=metrics&format=json", headers=h("editor")).json()
        assert j["meta"]["row_cap"] == 10 and len(j["rows"]) == 10


def test_analytics_reads_never_change_domain_data(secured, client):
    world(client)
    tables = (
        "suppliers",
        "facilities",
        "supply_relationships",
        "risk_assessments",
        "stockout_analyses",
        "resilience_runs",
        "decision_cases",
    )

    def snap():
        with Session(secured.app.state.engine) as s:
            return {t: s.execute(text(f"select count(*) from {t}")).scalar() for t in tables}

    before = snap()
    for ep in (
        "overview",
        "suppliers",
        "inventory",
        "logistics",
        "network",
        "disruption",
        "trends",
        "alerts",
        "activity",
    ):
        assert secured.get(f"{A}/{ep}", headers=h("reviewer")).status_code == 200
    assert snap() == before


def test_dashboard_stays_fast_with_a_thousand_suppliers(secured):
    with Session(secured.app.state.engine) as s:
        for i in range(1000):
            s.execute(
                text(
                    "insert into suppliers (supplier_code,name,country_or_region,category,status,provenance,name_normalized,version,created_at,updated_at) values (:c,:n,:r,'Cat','ACTIVE','SYNTHETIC_DEMO',:n,1,'2026-01-01','2026-01-01')"
                ),
                {"c": f"B{i}", "n": f"Bench {i}", "r": f"R{i % 20}"},
            )
        s.commit()
    t = time.perf_counter()
    r = secured.get(f"{A}/overview", headers=h("viewer"))
    sup = secured.get(f"{A}/suppliers?limit=20", headers=h("viewer")).json()
    dt = time.perf_counter() - t
    assert (
        r.status_code == 200
        and sup["table"]["total"] == 1000
        and len(sup["table"]["items"]) == 20
        and len(sup["breakdowns"]["by_region"]) == 20
    )
    assert dt < 10, dt
    assert SimStatus.COMPLETED and ResilienceRun
