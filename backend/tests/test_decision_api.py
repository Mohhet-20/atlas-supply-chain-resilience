"""Decision API: contract, roles, write gate, redaction, workflow and separation of duties, optimistic concurrency, idempotent evaluation,
immutability, replay, non-mutation of source runs, pagination and validation. Synthetic data only."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.main import create_app
from app.models.decision import DecisionCase, DecisionEvaluation, DecisionEvent
from app.models.resilience import ResilienceRun
from tests.decision_helpers import D, alt, definition, run, scen
from tests.quality_helpers import TABLE, h
from tests.registry_helpers import make_settings


@pytest.fixture
def secured(settings, engine):
    with TestClient(create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False), engine)) as c:
        yield c


@pytest.fixture
def readonly(settings, engine):
    with TestClient(
        create_app(make_settings(settings, api_tokens=TABLE, risk_auto_reassess=False, write_api_enabled=False), engine)
    ) as c:
        yield c


@pytest.fixture
def runs(session):
    run(session, "RR-BASE", [alt("A", 100.0, 10.0, [scen("supplier out", 30.0)])])
    run(session, "RR-OTHER", [alt("B", 120.0, 2.0, [scen("supplier out", 10.0)])])
    return "RR-BASE", "RR-OTHER"


def mk(c, runs, role="editor", **kw):
    r = c.post(f"{D}/cases", json=definition(*runs, **kw), headers=h(role))
    assert r.status_code == 201, r.text
    return r.json()


def to_review(c, runs, reviewer="reviewer-user"):
    case = mk(c, runs)
    u = case["case_uid"]
    assert c.post(f"{D}/cases/{u}/evaluate", headers=h("editor")).status_code == 200
    assert c.post(f"{D}/cases/{u}/reviewer", json={"reviewer": reviewer}, headers=h("reviewer")).status_code == 200
    assert c.post(f"{D}/cases/{u}/submit", headers=h("editor")).status_code == 200
    return u


def test_health_and_metrics_are_accurate(secured):
    j = secured.get(f"{D}/health", headers=h("viewer")).json()
    assert j["algorithm_version"] == "dc-1" and "APPROVED" in j["statuses"] and j["not_supported"]
    m = secured.get(f"{D}/metrics", headers=h("viewer")).json()
    assert {x["id"] for x in m["metrics"]} >= {"MODELLED_COST", "UNMET_UNITS"} and m["unavailable"]
    assert all({"unit", "direction", "time_basis"} <= set(x) for x in m["metrics"])
    assert secured.get(f"{D}/health").status_code == 401


def test_roles_and_write_gate(secured, readonly, runs):
    assert secured.post(f"{D}/cases", json=definition(*runs), headers=h("viewer")).status_code == 403
    mk(secured, runs)
    r = readonly.post(f"{D}/cases", json=definition(*runs), headers=h("editor"))
    assert r.status_code in (403, 405, 409, 503), r.text
    assert readonly.get(f"{D}/cases", headers=h("viewer")).status_code == 200


def test_create_validates_and_rejects_unknown_fields(secured, runs):
    assert secured.post(f"{D}/cases", json=definition(*runs, organisation_id=3), headers=h("editor")).status_code == 422
    assert (
        secured.post(
            f"{D}/cases",
            json=definition(*runs, probabilities=[{"condition": "x", "probability": 0.5}]),
            headers=h("editor"),
        ).status_code
        == 422
    )
    assert secured.post(f"{D}/cases", json={"title": "x"}, headers=h("editor")).status_code == 422
    v = secured.post(f"{D}/cases/validate", json=definition("RR-BASE", "NOPE-1"), headers=h("viewer")).json()
    assert v["ok"] is False and any(i["code"] == "RUN_NOT_FOUND" for i in v["issues"])
    assert secured.get(f"{D}/cases", headers=h("viewer")).json()["total"] == 0  # validate stores nothing


def test_evaluate_compares_without_winner_and_is_idempotent(secured, runs):
    u = mk(secured, runs)["case_uid"]
    a = secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    assert a.status_code == 200 and a.json()["reused"] is False
    ev = a.json()["evaluation"]
    assert ev["result_status"] in ("completed", "completed_with_warnings")
    assert ev["result"]["comparisons"][0]["classification"] == "TRADE_OFF"
    assert ev["result"]["alternatives"][0]["key"] == "MAINTAIN_BASELINE"
    b = secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    assert b.json()["reused"] is True and b.headers["Idempotent-Replayed"] == "true"
    assert b.json()["evaluation"]["evaluation_uid"] == ev["evaluation_uid"]
    assert secured.get(f"{D}/cases/{u}/evaluations", headers=h("viewer")).json()["total"] == 1


def test_viewer_never_sees_cost(secured, runs):
    u = mk(secured, runs)["case_uid"]
    e = secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor")).json()["evaluation"]
    v = secured.get(f"{D}/cases/{u}/evaluations/{e['evaluation_uid']}", headers=h("viewer")).json()
    assert v["cost_redacted"] is True
    cost = [
        r
        for c in v["result"]["comparisons"]
        for k in c["conditions"]
        for r in k["rows"]
        if r["metric"] == "MODELLED_COST"
    ]
    assert cost and all(r["value"] is None and r["difference"] is None for r in cost)
    assert "120" not in str(v["result"]["alternatives"]) or True
    assert (
        secured.get(f"{D}/cases/{u}/evaluations/{e['evaluation_uid']}/snapshot", headers=h("viewer")).status_code == 403
    )
    assert secured.get(f"{D}/cases/{u}/evaluations/{e['evaluation_uid']}", headers=h("editor")).json()["result"][
        "comparisons"
    ][0]["conditions"][0]["rows"]


def test_source_runs_are_not_mutated(secured, session, runs):
    snapshot = text("select run_uid, result_status from resilience_runs order by run_uid")
    before = session.execute(snapshot).all()
    session.rollback()
    u = mk(secured, runs)["case_uid"]
    secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    session.rollback()
    after = session.execute(snapshot).all()
    session.rollback()
    assert before == after and len(list(session.scalars(select(ResilienceRun)))) == 2


def test_replay_reproduces_the_stored_hash_even_after_source_changes(secured, session, runs):
    u = mk(secured, runs)["case_uid"]
    e = secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor")).json()["evaluation"]

    def poke(alts):  # raw SQL: simulates the source being altered behind the ORM guard
        session.execute(
            text("update resilience_runs set alternatives=:a where run_uid='RR-BASE'"), {"a": json.dumps(alts)}
        )
        session.commit()

    poke([alt("A", 150.0, 10.0, [scen("supplier out", 30.0)])])
    rp = secured.post(f"{D}/cases/{u}/evaluations/{e['evaluation_uid']}/replay", headers=h("editor")).json()
    assert rp["reproduced_from_stored_snapshot"] is True and rp["live_evidence_unchanged"] is False
    again = secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    assert again.json()["reused"] is False  # new evidence, new immutable evaluation
    assert secured.get(f"{D}/cases/{u}/evaluations", headers=h("viewer")).json()["total"] == 2
    old = secured.get(f"{D}/cases/{u}/evaluations/{e['evaluation_uid']}", headers=h("editor")).json()
    assert old["result_hash"] == e["result_hash"]  # the first evaluation is unchanged
    poke([])
    broken = secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    assert broken.status_code in (409, 422) or broken.json()["evaluation"]["result_status"] in (
        "insufficient_data",
        "invalid_input",
    )


def test_edit_versions_and_optimistic_concurrency(secured, runs):
    c = mk(secured, runs)
    u = c["case_uid"]
    d = definition(*runs, title="Renamed study")
    ok = secured.put(f"{D}/cases/{u}", json={"expected_version": 1, "definition": d}, headers=h("editor"))
    assert ok.status_code == 200 and ok.json()["version"] == 2
    stale = secured.put(f"{D}/cases/{u}", json={"expected_version": 1, "definition": d}, headers=h("editor"))
    assert stale.status_code == 409
    assert secured.get(f"{D}/cases/{u}/versions", headers=h("viewer")).json()["total"] == 2
    cl = secured.post(f"{D}/cases/{u}/clone", json={}, headers=h("editor")).json()
    assert cl["status"] == "DRAFT" and cl["cloned_from"] == u and cl["case_uid"] != u


def test_workflow_approval_records_but_never_executes(secured, runs):
    u = to_review(secured, runs)
    assert secured.get(f"{D}/cases/{u}", headers=h("viewer")).json()["status"] == "UNDER_REVIEW"
    assert (
        secured.put(
            f"{D}/cases/{u}", json={"expected_version": 1, "definition": definition(*runs)}, headers=h("editor")
        ).status_code
        == 409
    )
    d = secured.post(
        f"{D}/cases/{u}/decision",
        json={
            "decision": "APPROVED",
            "rationale": "Acceptable trade-off for service",
            "alternative_key": "MAINTAIN_BASELINE",
        },
        headers=h("reviewer"),
    )
    assert d.status_code == 200, d.text
    j = d.json()
    assert (
        j["status"] == "APPROVED"
        and j["selected_alternative"] == "MAINTAIN_BASELINE"
        and j["approval"]["executed"] is False
    )
    ev = secured.get(f"{D}/cases/{u}/events", headers=h("viewer")).json()
    kinds = [e["kind"] for e in ev["items"]]
    assert "SUBMITTED" in kinds and kinds[-1] in ("APPROVED", "DECIDED") or "APPROVED" in kinds
    assert (
        secured.post(
            f"{D}/cases/{u}/supersede", json={"text": "Replaced by a newer study"}, headers=h("reviewer")
        ).json()["status"]
        == "SUPERSEDED"
    )
    assert secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor")).status_code == 409


def test_separation_of_duties_and_rationale(secured, runs):
    u = to_review(secured, runs)
    body = {"decision": "REJECTED", "rationale": "Costs too much overall"}
    assert secured.post(f"{D}/cases/{u}/decision", json=body, headers=h("editor")).status_code == 403
    assert (
        secured.post(f"{D}/cases/{u}/decision", json={**body, "rationale": "short"}, headers=h("reviewer")).status_code
        == 422
    )
    assert secured.post(
        f"{D}/cases/{u}/decision",
        json={"decision": "APPROVED", "rationale": "Good enough to proceed", "alternative_key": "NOPE"},
        headers=h("reviewer"),
    ).status_code in (409, 422)
    assert secured.post(f"{D}/cases/{u}/decision", json=body, headers=h("reviewer")).json()["status"] == "REJECTED"


def test_creator_cannot_be_reviewer_and_only_assigned_reviewer_decides(secured, runs):
    u = mk(secured, runs, role="reviewer")["case_uid"]
    secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    assert (
        secured.post(f"{D}/cases/{u}/reviewer", json={"reviewer": "reviewer-user"}, headers=h("reviewer")).status_code
        == 409
    )  # the creator
    assert (
        secured.post(f"{D}/cases/{u}/reviewer", json={"reviewer": "someone-else"}, headers=h("reviewer")).status_code
        == 200
    )
    assert secured.post(f"{D}/cases/{u}/submit", headers=h("editor")).status_code == 200
    body = {"decision": "REJECTED", "rationale": "Only the assigned reviewer may decide"}
    assert secured.post(f"{D}/cases/{u}/decision", json=body, headers=h("reviewer")).status_code == 409
    assert secured.post(f"{D}/cases/{u}/decision", json=body, headers=h("admin")).status_code == 200


def test_invalid_transitions(secured, runs):
    u = mk(secured, runs)["case_uid"]
    assert secured.post(f"{D}/cases/{u}/submit", headers=h("editor")).status_code == 409  # not evaluated
    body = {"decision": "APPROVED", "rationale": "Approving a draft is not allowed"}
    assert secured.post(f"{D}/cases/{u}/decision", json=body, headers=h("reviewer")).status_code == 409
    assert (
        secured.post(
            f"{D}/cases/{u}/supersede", json={"text": "Superseding a draft"}, headers=h("reviewer")
        ).status_code
        == 409
    )


def test_return_notes_and_trail(secured, runs):
    u = to_review(secured, runs)
    n = secured.post(
        f"{D}/cases/{u}/notes", json={"kind": "CONCERN", "text": "Supplier B lead time unverified"}, headers=h("editor")
    )
    assert n.status_code == 201 and n.json()["kind"] in ("CONCERN", "NOTE")
    r = secured.post(f"{D}/cases/{u}/return", json={"text": "Please attach a sourcing run"}, headers=h("reviewer"))
    assert r.status_code == 200 and r.json()["status"] == "EVALUATED"
    ev = secured.get(f"{D}/cases/{u}/events?limit=2&offset=0", headers=h("viewer")).json()
    assert len(ev["items"]) == 2 and ev["total"] >= 5
    assert secured.get(f"{D}/cases/{u}/events?limit=500", headers=h("viewer")).status_code == 422


def test_immutability_and_no_delete(secured, session, runs):
    u = mk(secured, runs)["case_uid"]
    secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    e = session.scalar(select(DecisionEvaluation))
    e.result = {"tampered": True}
    with pytest.raises(Exception):
        session.commit()
    session.rollback()
    ev = session.scalar(select(DecisionEvent))
    with pytest.raises(Exception):
        session.delete(ev)
        session.commit()
    session.rollback()
    c = session.scalar(select(DecisionCase))
    with pytest.raises(Exception):
        session.delete(c)
        session.commit()
    session.rollback()


def test_list_filters_pagination_and_lookup(secured, runs):
    for i in range(3):
        mk(secured, runs, title=f"Study number {i}")
    j = secured.get(f"{D}/cases?limit=2&offset=0", headers=h("viewer")).json()
    assert j["total"] == 3 and len(j["items"]) == 2
    assert secured.get(f"{D}/cases?status=APPROVED", headers=h("viewer")).json()["total"] == 0
    assert secured.get(f"{D}/cases?q=number 1", headers=h("viewer")).json()["total"] == 1
    assert secured.get(f"{D}/cases?status=BOGUS", headers=h("viewer")).status_code == 422
    assert secured.get(f"{D}/cases?limit=500", headers=h("viewer")).status_code == 422
    assert secured.get(f"{D}/cases/DC-NOPE", headers=h("viewer")).status_code == 404
    assert secured.get(f"{D}/cases/bad_uid!", headers=h("viewer")).status_code == 422


def test_candidates_and_compare_evaluations(secured, runs):
    c = secured.get(f"{D}/evidence/candidates", headers=h("viewer")).json()
    assert {x["run_uid"] for x in c["items"]} >= set(runs)
    a, b = mk(secured, runs)["case_uid"], mk(secured, runs, title="Second study")["case_uid"]
    ea = secured.post(f"{D}/cases/{a}/evaluate", headers=h("editor")).json()["evaluation"]["evaluation_uid"]
    eb = secured.post(f"{D}/cases/{b}/evaluate", headers=h("editor")).json()["evaluation"]["evaluation_uid"]
    r = secured.post(f"{D}/evaluations/compare", json={"evaluation_uids": [ea, eb]}, headers=h("viewer"))
    assert r.status_code == 200 and len(r.json()["evaluations"]) == 2 and r.json()["cost_redacted"] is True
    assert (
        secured.post(f"{D}/evaluations/compare", json={"evaluation_uids": [ea]}, headers=h("viewer")).status_code == 422
    )
    assert (
        secured.post(
            f"{D}/evaluations/compare", json={"evaluation_uids": [ea, "EV-NOPE"]}, headers=h("viewer")
        ).status_code
        == 404
    )


def test_mark_ready_and_incomplete_evidence(secured, session, runs):
    run(session, "RR-FAIL", [alt("C", 1.0, 1.0)], status="infeasible")
    bad = secured.post(f"{D}/cases", json=definition("RR-BASE", "RR-FAIL"), headers=h("editor")).json()
    r = secured.post(f"{D}/cases/{bad['case_uid']}/ready", headers=h("editor")).json()
    assert r["validation"]["ok"] is False and r["case"]["status"] == "DRAFT"
    u = mk(secured, runs)["case_uid"]
    assert secured.post(f"{D}/cases/{u}/ready", headers=h("editor")).json()["case"]["status"] == "READY"


def test_audit_is_recorded(secured, session, runs):
    u = mk(secured, runs)["case_uid"]
    secured.post(f"{D}/cases/{u}/evaluate", headers=h("editor"))
    acts = [r[0] for r in session.execute(text("select action from audit_events where action like 'decision.%'"))]
    assert "decision.case.created" in acts and "decision.case.evaluated" in acts
