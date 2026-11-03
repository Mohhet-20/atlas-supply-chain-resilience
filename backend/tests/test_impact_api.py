"""Impact analysis HTTP contract: auth, validation, lifecycle, idempotency, results, comparison, immutability, safe errors."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.impact import config as cfg
from app.impact import runs as iruns
from app.main import create_app
from app.models.enums import SimStatus
from app.models.impact import ImpactAnalysis, ImpactFinding
from app.models.scenario import SimulationImpact, SimulationRun
from app.scenarios import runs as sruns
from tests.graph_helpers import node, ok
from tests.impact_helpers import IM, TABLE, analyze, h, linear, make_run, new_scenario, realistic, sup_event
from tests.registry_helpers import make_settings


@pytest.fixture
def secured(settings, engine):
    s = make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False)
    with TestClient(create_app(s, engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    s = make_settings(
        settings,
        write_api_enabled=False,
        environment="production",
        api_tokens=TABLE,
        cors_allowed_origins=["https://x.example"],
        risk_auto_reassess=False,
    )
    with TestClient(create_app(s, engine)) as c:
        yield c


def start(c, run_id, role="editor", **kw):
    return c.post(f"{IM}/analyses", json={"run_id": run_id} | kw, headers=h(role))


@pytest.fixture
def world(client):
    w = realistic(client)
    return w, make_run(client, [sup_event(w["S1"]), sup_event(w["S2"])])


def test_auth_matrix(client, secured, world):
    _w, run = world
    assert secured.get(f"{IM}/analyses").status_code == 401
    assert secured.get(f"{IM}/limits").status_code == 401
    assert start(secured, run, role="viewer").status_code == 403
    assert secured.post(f"{IM}/analyses", json={"run_id": run}).status_code == 401
    assert secured.get(f"{IM}/analyses", headers=h("viewer")).status_code == 200
    assert secured.get(f"{IM}/limits", headers=h("viewer")).json()["client_overridable"] is False


def test_write_gate(readonly, world):
    _w, run = world
    r = start(readonly, run)
    assert r.status_code == 403 and r.json()["error"]["code"] == "write_api_disabled"


def test_create_runs_and_returns_status_then_results(secured, world):
    _w, run = world
    r = start(secured, run)
    assert r.status_code == 202
    aid = r.json()["id"]
    d = secured.get(f"{IM}/analyses/{aid}", headers=h("viewer")).json()
    assert (
        d["status"] == "COMPLETED"
        and d["algorithm_version"] == "ci-1"
        and d["graph_basis"] == "SAME_AS_RUN"
        and d["complete"] is True
    )
    assert (
        d["reproducibility"]["config_hash"] == d["config_hash"] and d["config"]["reference_date"]
    )  # resolved and stored
    assert d["summary"]["unique_entities"] == 7 and "does not mean it is safe" in d["summary"]["scope"]
    assert "not a forecast" in d["disclaimer"].lower()


@pytest.mark.parametrize(
    "body",
    [
        {"run_id": 1, "organization_id": 7},
        {"run_id": 1, "config": {"max_paths_per_entity": 11}},
        {"run_id": 1, "config": {"stale_after_days": 1}},
        {"run_id": 1, "config": {"command": "rm -rf /"}},
        {"run_id": 0},
        {"run_id": "abc"},
        {},
    ],
)
def test_rejects_invalid_or_foreign_fields(secured, world, body):
    assert secured.post(f"{IM}/analyses", json=body, headers=h("editor")).status_code == 422


def test_unknown_and_ineligible_runs(client, secured):
    assert start(secured, 999).status_code == 404
    w = linear(client)
    sid = new_scenario(client, [sup_event(w["a"])])
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        from app.models.scenario import Scenario

        run, _c = sruns.create_run(s, s.get(Scenario, sid), "tester", sruns.RunLimits())
        rid = run.id
    r = start(secured, rid)  # QUEUED run
    assert r.status_code == 422 and "COMPLETED or PARTIAL" in r.json()["error"]["message"]


def test_idempotent_retry(secured, world):
    w, run = world
    key = {"Idempotency-Key": "abcdef123456"}
    r1 = secured.post(f"{IM}/analyses", json={"run_id": run}, headers=h("editor") | key)
    r2 = secured.post(f"{IM}/analyses", json={"run_id": run}, headers=h("editor") | key)
    assert r1.status_code == r2.status_code == 202 and r1.json()["id"] == r2.json()["id"]
    assert r2.headers.get("Idempotent-Replayed") == "true" and "Idempotent-Replayed" not in r1.headers
    other = make_run(secured.app.state and client_of(secured), [sup_event(w["S3"])])
    assert secured.post(f"{IM}/analyses", json={"run_id": other}, headers=h("editor") | key).status_code == 409
    n = secured.get(f"{IM}/analyses", headers=h("viewer")).json()["total"]
    assert n == 1  # no duplicate was created


def client_of(c):
    class _C:
        app = c.app

    return _C()


def test_rerun_creates_new_analysis_and_keeps_old(secured, world):
    _w, run = world
    a1 = start(secured, run).json()["id"]
    a2 = start(secured, run).json()["id"]
    assert a1 != a2
    items = secured.get(f"{IM}/analyses", params={"run_id": run}, headers=h("viewer")).json()
    assert (
        items["total"] == 2 and secured.get(f"{IM}/analyses/{a1}", headers=h("viewer")).json()["status"] == "COMPLETED"
    )


def test_findings_filters_pagination_sorting(secured, world):
    _w, run = world
    aid = start(secured, run).json()["id"]
    g = lambda **p: secured.get(f"{IM}/analyses/{aid}/findings", params=p, headers=h("viewer"))  # noqa: E731
    allf = g(limit=200).json()
    assert allf["total"] == 7 and allf["complete"] is True
    assert {i["entity_name"] for i in g(entity_type="PRODUCT").json()["items"]} == {"Product P1", "Product P2"}
    assert {i["entity_name"] for i in g(classification="INITIATING").json()["items"]} == {"Supplier S1", "Supplier S2"}
    assert {i["entity_name"] for i in g(classification="DIRECT").json()["items"]} == {"Facility F1", "Facility F2"}
    assert g(classification="PROPAGATED").json()["total"] == 3
    assert g(min_depth=3, max_depth=3).json()["items"][0]["entity_name"] == "Product P1"
    assert g(impact_state="ASSUMPTION_DEPENDENT").json()["total"] >= 1
    assert g(q="p1").json()["total"] == 1
    first = g(limit=3, offset=0).json()
    nxt = g(limit=3, offset=3).json()
    assert len(first["items"]) == 3 and not ({i["id"] for i in first["items"]} & {i["id"] for i in nxt["items"]})
    depths = [i["min_depth"] for i in g(limit=200, sort="depth").json()["items"]]
    assert depths == sorted(depths)
    uns = g(dimension="capacity_shortfall", dimension_status="UNAVAILABLE").json()
    assert uns["total"] >= 1 and all(i["dimension_status"]["capacity_shortfall"] == "UNAVAILABLE" for i in uns["items"])
    ev = g(event_uid=allf["items"][0]["event_uids"][0] if allf["items"][0]["event_uids"] else "x").json()
    assert ev["total"] >= 1
    for bad in (
        {"classification": "NOPE"},
        {"limit": 0},
        {"limit": 500},
        {"offset": -1},
        {"min_depth": 9},
        {"sort": "x"},
        {"entity_type": "WIDGET"},
        {"dimension_status": "QUANTIFIED"},
        {"dimension": "a;b"},
    ):
        assert g(**bad).status_code == 422, bad


def test_finding_detail_paths_calculations_evidence(secured, world):
    _w, run = world
    aid = start(secured, run).json()["id"]
    lst = secured.get(f"{IM}/analyses/{aid}/findings", params={"q": "Facility F3"}, headers=h("viewer")).json()[
        "items"
    ][0]
    d = secured.get(f"{IM}/analyses/{aid}/findings/{lst['entity_type']}/{lst['entity_id']}", headers=h("viewer")).json()
    assert d["classification"] == "PROPAGATED" and d["interaction"] == "OVERLAPPING" and len(d["paths"]) == 3
    assert (
        d["combined"]["status"] == "ASSUMPTION_DEPENDENT"
        and len(d["per_event"]) == 2
        and d["assumptions"]
        and d["evidence"]
    )
    assert d["analysis"]["algorithm_version"] == "ci-1" and "Potential exposure" in d["disclaimer"]
    assert {"supply_shortfall", "capacity_shortfall", "evidence_completeness"} <= set(d["dimensions"])
    assert d["confidence_basis"]["note"] and d["coverage_detail"]["needed_inputs"] >= 1
    p = secured.get(
        f"{IM}/analyses/{aid}/findings/{lst['entity_type']}/{lst['entity_id']}/paths", headers=h("viewer")
    ).json()
    assert (
        p["path_count"] == 3
        and p["paths_complete"] is True
        and all(s["status"] == "CONFIRMED" for x in p["paths"] for s in x["steps"])
    )
    calc = secured.get(f"{IM}/analyses/{aid}/calculations", headers=h("viewer")).json()
    assert calc["total"] >= 1 and all(i["assumptions"] for i in calc["items"]) and "no financial values" in calc["note"]
    ev = secured.get(f"{IM}/analyses/{aid}/evidence", params={"role": "PROPAGATION"}, headers=h("viewer")).json()
    assert ev["total"] >= 6 and all(i["record"]["provenance"] for i in ev["items"])
    one = secured.get(
        f"{IM}/analyses/{aid}/evidence", params={"edge_ref": ev["items"][0]["edge_ref"]}, headers=h("viewer")
    ).json()
    assert one["total"] == 1
    miss = secured.get(f"{IM}/analyses/{aid}/findings/SUPPLIER/{_w['S4']['id']}", headers=h("viewer"))
    assert miss.status_code == 404 and "does not mean it is safe" in miss.json()["error"]["message"]


def test_events_summary_criticality_alternatives_graph(secured, world):
    _w, run = world
    aid = start(secured, run, config={"include_proposed_alternatives": True}).json()["id"]
    g = lambda path, **p: secured.get(f"{IM}/analyses/{aid}/{path}", params=p, headers=h("viewer"))  # noqa: E731
    ev = g("events").json()
    assert (
        ev["total"] == 2
        and all("UNSPECIFIED" not in e["timing"] or e["duration_days"] is None for e in ev["items"])
        and "not inferred" in ev["items"][0]["timing"]
    )
    sm = g("summary").json()
    assert (
        sm["summary"]["facilities_exposed"] == 3
        and sm["coverage"]["mean_input_coverage"] is not None
        and sm["uncertainty"]
    )
    cr = g("criticality", bottleneck_only="true").json()
    assert (
        cr["total"] == 1
        and cr["items"][0]["entity_name"] == "Facility F1"
        and "requires validation" in cr["items"][0]["label"]
    )
    assert "separate from operational severity" in cr["note"]
    al = g("alternatives").json()
    assert (
        al["total"] >= 2
        and "Structurally possible only" in al["notice"]
        and all(i["operationally_validated"] is False for i in al["items"])
    )
    assert g("alternatives", path_state="PROPOSED").json()["total"] >= 1
    gr = g("graph", max_nodes=3).json()
    assert gr["shown_nodes"] == 3 and gr["truncated"] is True and gr["total_nodes"] == 7
    full = g("graph").json()
    assert len(full["edges"]) >= 6 and not full["truncated"]


def test_results_unavailable_until_finished_and_for_failed(client, secured, world, monkeypatch):
    _w, run = world
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        a, _c = iruns.create_analysis(
            s, s.get(SimulationRun, run), cfg.ImpactConfig(), "tester", iruns.AnalysisLimits()
        )
        aid = a.id
    for path in ("findings", "summary", "criticality", "alternatives", "evidence", "events", "graph", "calculations"):
        r = secured.get(f"{IM}/analyses/{aid}/{path}", headers=h("viewer"))
        assert r.status_code == 422 and "QUEUED" in r.json()["error"]["message"], path
    import app.impact.runs as mod

    def boom(*a, **k):
        raise RuntimeError("secret /srv/path password=hunter2")

    monkeypatch.setattr(mod, "run_analysis", boom)
    iruns.execute_analysis(factory, aid, iruns.AnalysisLimits())
    d = secured.get(f"{IM}/analyses/{aid}", headers=h("viewer")).json()
    assert d["status"] == "FAILED" and d["error"] == "unexpected RuntimeError"
    assert "hunter2" not in str(d) and "/srv" not in str(d)
    r = secured.get(f"{IM}/analyses/{aid}/findings", headers=h("viewer"))
    assert r.status_code == 422 and "no results were stored" in r.json()["error"]["message"]
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(ImpactFinding).where(ImpactFinding.analysis_id == aid)) == 0


def test_cancel_and_concurrency(client, secured, world):
    _w, run = world
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        a, _c = iruns.create_analysis(
            s, s.get(SimulationRun, run), cfg.ImpactConfig(), "editor-user", iruns.AnalysisLimits()
        )
        aid = a.id
    with factory() as s, pytest.raises(ConflictError) as ei:  # one active analysis per run
        iruns.create_analysis(s, s.get(SimulationRun, run), cfg.ImpactConfig(), "tester", iruns.AnalysisLimits())
    assert "already queued or running" in str(ei.value)
    c = secured.post(f"{IM}/analyses/{aid}/cancel", headers=h("editor"))
    assert c.status_code == 200 and c.json()["status"] == "CANCELLED"
    assert secured.post(f"{IM}/analyses/{aid}/cancel", headers=h("editor")).status_code == 409
    iruns.execute_analysis(factory, aid, iruns.AnalysisLimits())  # a cancelled analysis is never executed
    assert secured.get(f"{IM}/analyses/{aid}", headers=h("viewer")).json()["status"] == "CANCELLED"
    assert secured.post(f"{IM}/analyses/{aid}/cancel", headers=h("viewer")).status_code == 403
    # someone else's analysis: another editor is refused, a reviewer is allowed
    with factory() as s, s.begin():
        s.get(ImpactAnalysis, aid).status = SimStatus.QUEUED
        s.get(ImpactAnalysis, aid).requested_by = "someone-else"
    assert secured.post(f"{IM}/analyses/{aid}/cancel", headers=h("editor")).status_code == 403
    assert secured.post(f"{IM}/analyses/{aid}/cancel", headers=h("reviewer")).status_code == 200


def test_active_cap_and_stale_lock(client, world):
    _w, run = world
    factory = client.app.state.session_factory
    from datetime import timedelta

    from app.models.types import utcnow

    with factory() as s, s.begin():
        r = s.get(SimulationRun, run)
        a, _c = iruns.create_analysis(s, r, cfg.ImpactConfig(), "tester", iruns.AnalysisLimits(max_active=1))
        with pytest.raises(Exception) as ei:
            iruns.create_analysis(s, make_other(s, r), cfg.ImpactConfig(), "tester", iruns.AnalysisLimits(max_active=1))
        assert "limit 1" in str(ei.value)
        a.queued_at = utcnow() - timedelta(hours=2)  # owner died
    with factory() as s, s.begin():
        b, created = iruns.create_analysis(
            s,
            make_other(s, s.get(SimulationRun, run)),
            cfg.ImpactConfig(),
            "tester",
            iruns.AnalysisLimits(max_active=1),
        )
        assert (
            created
            and s.get(ImpactAnalysis, a.id).status is SimStatus.FAILED
            and s.get(ImpactAnalysis, a.id).error == "abandoned: no heartbeat"
        )


def make_other(s, run):

    # a second eligible run id is not needed for the cap check: reuse by cloning the row
    clone = SimulationRun(
        **{c.name: getattr(run, c.name) for c in SimulationRun.__table__.columns if c.name not in ("id", "run_uid")},
        run_uid=f"SR-X{run.id}{id(s) % 10000}",
    )
    clone.idempotency_key = None
    s.add(clone)
    s.flush()
    return clone


def test_results_independent_of_live_graph_changes(client, secured, world):
    w, run = world
    aid = start(secured, run).json()["id"]
    before = secured.get(f"{IM}/analyses/{aid}/findings", params={"limit": 200}, headers=h("viewer")).json()
    ok(client, "TRANSFERS_TO", w["F3"], node(client, "facilities", "Brand New"), supply_share_pct=100.0)
    after = secured.get(f"{IM}/analyses/{aid}/findings", params={"limit": 200}, headers=h("viewer")).json()
    assert before == after
    fresh = start(secured, run).json()["id"]
    d = secured.get(f"{IM}/analyses/{fresh}", headers=h("viewer")).json()
    assert d["graph_basis"] == "CHANGED_SINCE_RUN" and d["summary"]["unique_entities"] == 8


def test_compare_compatible_and_incompatible(client, secured, world):
    w, run = world
    a1 = start(secured, run).json()["id"]
    ok(client, "TRANSFERS_TO", w["F3"], node(client, "facilities", "Brand New"), supply_share_pct=100.0)
    a2 = start(secured, run).json()["id"]
    r = secured.get(f"{IM}/analyses/compare", params={"ids": [a1, a2]}, headers=h("viewer"))
    assert r.status_code == 200
    c = r.json()
    assert c["comparable"] is True and {x["code"] for x in c["warnings"]} >= {"NETWORK_INPUTS_DIFFER"}
    d = c["comparisons"][0]
    assert (
        d["newly_exposed"]["total"] == 1
        and d["newly_exposed"]["items"][0]["entity"].startswith("FACILITY:")
        and d["newly_exposed"]["items"][0]["analysis_id"] == a2
    )
    assert (
        d["no_longer_exposed"]["total"] == 0 and d["persistently_exposed"] == 7 and d["counts"]["unique_entities"] == 1
    )
    assert "cannot be separated" in d["interpretation"]
    with Session(client.app.state.engine) as s, s.begin():
        s.get(ImpactAnalysis, a2).algorithm_version = "ci-9"
    bad = secured.get(f"{IM}/analyses/compare", params={"ids": [a1, a2]}, headers=h("viewer")).json()
    assert (
        bad["comparable"] is False
        and bad["not_comparable_reasons"][0]["code"] == "ALGORITHM_DIFFERS"
        and bad["comparisons"] == []
    )
    assert bad["metric_comparability"]["counts_and_sets"] is False
    for q in ({"ids": [a1]}, {"ids": [a1, a1]}, {"ids": [a1, 999]}, {"ids": [a1, 0]}, {"ids": [1, 2, 3, 4, 5, 6]}):
        assert secured.get(f"{IM}/analyses/compare", params=q, headers=h("viewer")).status_code in (404, 422), q


def test_compare_rejects_unfinished(client, secured, world):
    _w, run = world
    a1 = start(secured, run).json()["id"]
    factory = client.app.state.session_factory
    with factory() as s, s.begin():
        a, _c = iruns.create_analysis(
            s, s.get(SimulationRun, run), cfg.ImpactConfig(), "tester", iruns.AnalysisLimits()
        )
        qid = a.id
    r = secured.get(f"{IM}/analyses/compare", params={"ids": [a1, qid]}, headers=h("viewer"))
    assert r.status_code == 422 and "only COMPLETED or PARTIAL" in r.json()["error"]["message"]


def test_analysis_does_not_touch_simulation_or_scenario_results(client, secured, world):
    _w, run = world
    with Session(client.app.state.engine) as s:
        before = (
            s.scalar(select(func.count()).select_from(SimulationImpact)),
            s.get(SimulationRun, run).summary,
            s.get(SimulationRun, run).status,
        )
    start(secured, run)
    with Session(client.app.state.engine) as s:
        after = (
            s.scalar(select(func.count()).select_from(SimulationImpact)),
            s.get(SimulationRun, run).summary,
            s.get(SimulationRun, run).status,
        )
    assert before == after


def test_no_organisation_leakage_in_payloads(secured, world):
    _w, run = world
    aid = start(secured, run).json()["id"]
    text = secured.get(f"{IM}/analyses/{aid}/findings/FACILITY/{_w['F3']['id']}", headers=h("viewer")).text
    for token in ("organization", "tenant", "traceback", "Traceback", "/home/", "sqlite"):
        assert token not in text
    assert isinstance(analyze, object)
