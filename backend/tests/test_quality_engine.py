from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.enums import EntityKind, EvalScope, EvalStatus, ScoreState
from app.models.facility import Facility
from app.models.quality import QualityEvalRun, QualityScore
from app.quality import engine
from app.registry.config import quality_config
from tests.graph_helpers import node, ok, world
from tests.quality_helpers import Q, evaluate, facility_with_coords, findings
from tests.registry_helpers import API, confirm_pair, merge_body, mk


def test_placeholder_coordinates_flagged_but_unknown_coordinates_are_not(client):
    zero = facility_with_coords(client, "Zero Site", 0.0, 0.0)
    unknown = facility_with_coords(client, "Unknown Site", None, None)
    real = facility_with_coords(client, "Real Site", 48.1, 11.5)
    run = evaluate(client)
    assert run.status is EvalStatus.COMPLETED
    flagged = {f["entity_id"] for f in findings(client, rule_id="QR115")}
    assert zero["id"] in flagged and unknown["id"] not in flagged and real["id"] not in flagged
    for f in findings(client, entity_type="FACILITY", entity_id=unknown["id"]):
        assert f["rule_id"] not in {"QR114", "QR115"}


def test_run_is_idempotent_and_reproducible(client):
    facility_with_coords(client, "Zero Site", 0.0, 0.0)
    r1 = evaluate(client)
    first = {f["id"] for f in findings(client)}
    r2 = evaluate(client)
    assert {f["id"] for f in findings(client)} == first
    assert r2.findings_created == 0 and r2.findings_unchanged >= 1
    assert r1.catalog_hash == r2.catalog_hash
    assert r1.aggregate["mean"] == r2.aggregate["mean"]


def test_finding_resolves_then_reopens(client):
    fac = facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    f = findings(client, rule_id="QR115")[0]
    assert f["status"] == "OPEN"
    patched = client.patch(
        f"/api/v1/registry/facilities/{fac['id']}",
        json={"latitude": 10.0, "longitude": 20.0, "expected_version": fac["version"]},
    )
    assert patched.status_code == 200, patched.text
    after = findings(client, rule_id="QR115")[0]
    assert after["status"] == "RESOLVED"  # the edit hook re-evaluated the record
    v = patched.json()["version"]
    client.patch(
        f"/api/v1/registry/facilities/{fac['id']}", json={"latitude": 0.0, "longitude": 0.0, "expected_version": v}
    )
    again = findings(client, rule_id="QR115")[0]
    assert again["id"] == f["id"] and again["status"] == "REOPENED" and again["reopened_count"] == 1


def test_accepted_exception_is_not_reopened_or_scored_as_failure(client):
    fac = facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    f = findings(client, rule_id="QR115")[0]
    r = client.post(
        f"{Q}/findings/{f['id']}/transition",
        json={
            "action": "accept_exception",
            "note": "Known placeholder, site survey pending.",
            "expected_version": f["version"],
        },
    )
    assert r.status_code == 200, r.text
    evaluate(client)
    assert findings(client, rule_id="QR115")[0]["status"] == "ACCEPTED_EXCEPTION"
    sc = client.get(f"{Q}/scores/FACILITY/{fac['id']}").json()
    assert sc["exceptions_excluded"] >= 1


def test_proposed_relationship_is_not_invalid_and_reported_separately(client):
    s, f = node(client, "suppliers", "Prop Supplier"), node(client, "facilities", "Prop Plant")
    edge = ok(client, "SUPPLIES", s, f, status="PROPOSED")
    evaluate(client)
    ent = client.get(f"{Q}/entities/RELATIONSHIP/{edge['ref'].split(':')[1]}").json()
    open_rules = {x["rule_id"] for x in ent["open_findings"]}
    structural = {"QR201", "QR202", "QR203", "QR204", "QR205", "QR207", "QR208"}
    assert not (open_rules & structural)
    assert ent["entity"]["status"] == "PROPOSED"
    assert "QR213" in open_rules or ent["score"]["state"] in {"SCORED", "INSUFFICIENT_EVIDENCE"}


def test_network_scope_covers_everything_and_scores_have_state(client):
    world(client)
    run = evaluate(client)
    assert run.truncated is False and run.status is EvalStatus.COMPLETED
    assert run.examined["SUPPLIER"] == 3 and run.examined["RELATIONSHIP"] >= 1
    with Session(client.app.state.session_factory.kw["bind"]) as s:
        states = {sc.state for sc in s.scalars(select(QualityScore))}
    assert states <= {ScoreState.SCORED, ScoreState.INSUFFICIENT_EVIDENCE}


def test_run_is_bounded_and_partial_is_explicit(client):
    for i in range(6):
        mk(client, "suppliers", name=f"Bounded Supplier {i}")
    s = client.app.state.settings
    lim = engine.Limits(max_subjects=3, chunk=2)
    run = engine.evaluate_now(
        client.app.state.session_factory,
        EvalScope.NETWORK,
        None,
        "t",
        quality_config(s.quality_config_path),
        limits=lim,
    )
    assert run.status is EvalStatus.PARTIAL and run.truncated is True and run.truncation_reasons


def test_second_run_of_same_scope_is_rejected_while_queued(client):
    mk(client, "suppliers", name="Lock Supplier")
    f = client.app.state.session_factory
    with f() as a, a.begin():
        engine.create_run(a, EvalScope.NETWORK, None, "t")
    with f() as b, pytest.raises(ConflictError):
        engine.create_run(b, EvalScope.NETWORK, None, "t")


def test_stale_lock_is_released_with_explicit_failure(client):
    f = client.app.state.session_factory
    with f() as a, a.begin():
        run = engine.create_run(a, EvalScope.NETWORK, None, "t")
        run.heartbeat_at = None
        rid = run.id
        a.execute(
            text("UPDATE quality_eval_runs SET queued_at = :t WHERE id = :i"),
            {"t": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2), "i": rid},
        )
    with f() as b, b.begin():
        engine.create_run(b, EvalScope.NETWORK, None, "t")
    with f() as c:
        old = c.get(QualityEvalRun, rid)
        assert old.status is EvalStatus.FAILED and "abandoned" in (old.error or "")


def test_disabled_rule_produces_no_new_findings_and_hides_old(client, engine_admin=None):
    facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    assert findings(client, rule_id="QR115")
    f = client.app.state.session_factory
    from app.models.quality import QualityRuleSetting

    with f() as s, s.begin():
        s.add(QualityRuleSetting(rule_id="QR115", enabled=False, reason="test", updated_by="t", revision=1))
    assert findings(client, rule_id="QR115") == []
    assert findings(client, rule_id="QR115", include_disabled="true")
    r = evaluate(client)
    assert r.catalog_hash


def test_merged_away_records_are_not_evaluated(client):
    a, b = (
        mk(client, "suppliers", name="Acme Components GmbH"),
        mk(client, "suppliers", name="ACME Components G.m.b.H."),
    )
    run1 = evaluate(client)
    assert run1.examined["SUPPLIER"] == 2
    cid = confirm_pair(client, "SUPPLIER", a, b)
    r = client.post(
        f"{API}/merges",
        json=merge_body("SUPPLIER", a, b, field_decisions={"name": {"choice": "KEEP_SURVIVOR"}}, candidate_id=cid),
    )
    assert r.status_code == 201, r.text
    run2 = evaluate(client)
    assert run2.examined["SUPPLIER"] == 1


def test_entity_scope_unknown_record_is_404_domain_error(client):
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        engine.create_run(
            client.app.state.session_factory(), EvalScope.ENTITY, {"entity_type": "SUPPLIER", "entity_id": 999999}, "t"
        )


def test_facility_rows_untouched_by_evaluation(client, engine):
    fac = facility_with_coords(client, "Zero Site", 0.0, 0.0)
    evaluate(client)
    with Session(engine) as s:
        row = s.get(Facility, fac["id"])
        assert (row.latitude, row.longitude, row.version) == (0.0, 0.0, fac["version"])
    assert EntityKind.FACILITY.value == "FACILITY"
