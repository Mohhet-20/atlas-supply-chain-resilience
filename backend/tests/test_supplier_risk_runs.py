"""Assessment runs, history, supersession, idempotency, failure reporting, alerts, hooks."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ConflictError
from app.models.enums import RiskRunScope
from app.models.risk import RiskAlert, RiskAssessment, RiskEvidence, RiskFactor, RiskRun
from app.risk import runs
from tests.graph_helpers import G, node, ok
from tests.supplier_risk_helpers import R, assess_all, cfg_of, current, two_supplier_site


@pytest.fixture(autouse=True)
def manual_assessment(client):
    """Tests drive assessment runs explicitly; automatic reassessment is exercised in the hook tests below."""
    client.app.state.settings.risk_auto_reassess = False


def n_assessments(client, sid: int | None = None) -> int:
    with Session(client.app.state.engine) as s:
        q = select(func.count()).select_from(RiskAssessment)
        if sid:
            q = q.where(RiskAssessment.supplier_id == sid)
        return s.scalar(q)


def test_network_run_assesses_every_active_supplier_and_records_counts(client, seeded):
    run = assess_all(client)
    assert run.status.value == "COMPLETED" and run.examined == 6 and run.failed == 0 and run.skipped == 0
    assert run.assessed + run.partial + run.insufficient == 6 and run.created == 6
    assert run.methodology_version == "rm-1" and len(run.methodology_hash) == 64
    assert run.graph_snapshot["supply_relationships"] > 0


def test_rerun_with_unchanged_data_creates_no_new_history(client, seeded):
    assess_all(client)
    before = n_assessments(client)
    second = assess_all(client)
    assert second.created == 0 and second.unchanged == 6 and n_assessments(client) == before
    with Session(client.app.state.engine) as s:
        confirmed = s.scalars(
            select(RiskAssessment.last_confirmed_run_id).where(RiskAssessment.is_current.is_(True))
        ).all()
    assert set(confirmed) == {second.id}  # confirmation is recorded without rewriting the assessment


def test_changed_inputs_supersede_the_previous_assessment_and_keep_history(client):
    a, b, _f = two_supplier_site(client, crit="HIGH")
    assess_all(client)
    first = current(client, a["id"])["assessment"]
    sole = node(client, "suppliers", "Fir Parts")
    other = node(client, "facilities", "Gum Plant")
    ok(client, "SUPPLIES", sole, other, criticality="CRITICAL")
    ok(client, "SUPPLIES", a, other, criticality="CRITICAL")  # a now shares a new node; the structure around a changed
    assess_all(client)
    after = current(client, a["id"])["assessment"]
    assert after["id"] != first["id"] and after["supersedes_id"] == first["id"]
    hist = client.get(f"{R}/suppliers/{a['id']}/assessments").json()
    assert hist["total"] == 2 and [h["is_current"] for h in hist["items"]] == [True, False]
    assert hist["items"][0]["score_change"] is not None
    assert hist["items"][0]["change_comparable"] is True
    assert b["id"] != a["id"]


def test_exactly_one_current_assessment_per_supplier_is_enforced_by_the_database(client, seeded):
    assess_all(client)
    with Session(client.app.state.engine) as s:
        row = s.scalars(select(RiskAssessment).where(RiskAssessment.is_current.is_(True))).first()
        clone = RiskAssessment(
            assessment_uid="RA-DUP", supplier_id=row.supplier_id, run_id=row.run_id, computed_at=row.computed_at, status=row.status,
            methodology_version=row.methodology_version, methodology_hash=row.methodology_hash, risk_score=row.risk_score, classification=row.classification,
            evidence_coverage=row.evidence_coverage, effective_weight=row.effective_weight, reliability=row.reliability, reliability_band=row.reliability_band,
            scored_factors=row.scored_factors, contributions=[], missing=[], actions=[], data_basis={}, fingerprint="x", requires_review=False,
            review_state="UNREVIEWED", is_current=True, last_confirmed_at=row.computed_at,
        )  # fmt: skip
        s.add(clone)
        with pytest.raises(IntegrityError):
            s.flush()


def test_scores_are_never_stored_for_insufficient_evidence(client):
    lone = node(client, "suppliers", "Lonely Supplier")
    assess_all(client)
    a = current(client, lone["id"])["assessment"]
    assert a["status"] == "INSUFFICIENT_EVIDENCE" and a["risk_score"] is None and a["classification"] is None
    with Session(client.app.state.engine) as s:
        row = s.get(RiskAssessment, a["id"])
        row.risk_score = 10.0
        with pytest.raises(IntegrityError):
            s.flush()


def test_factors_and_evidence_are_persisted_without_duplicates(client, seeded):
    assess_all(client)
    with Session(client.app.state.engine) as s:
        dup = s.execute(
            select(RiskEvidence.factor_id, RiskEvidence.evidence_type, RiskEvidence.ref, func.count())
            .group_by(RiskEvidence.factor_id, RiskEvidence.evidence_type, RiskEvidence.ref)
            .having(func.count() > 1)
        ).all()
        per_assessment = s.execute(
            select(RiskFactor.assessment_id, func.count()).group_by(RiskFactor.assessment_id)
        ).all()
    assert dup == []
    assert {n for _a, n in per_assessment} == {10}  # every category is always recorded, available or not


# ------------------------------------------------------------------------------------------------ run mechanics


def test_idempotent_replay_returns_the_same_run(client):
    two_supplier_site(client)
    body = {"scope": "NETWORK"}
    first = client.post(f"{R}/runs", json=body, headers={"Idempotency-Key": "key-0001-abcdef"})
    again = client.post(f"{R}/runs", json=body, headers={"Idempotency-Key": "key-0001-abcdef"})
    assert first.status_code == again.status_code == 202
    assert first.json()["id"] == again.json()["id"] and again.headers["Idempotent-Replayed"] == "true"
    other_scope = client.post(
        f"{R}/runs", json={"scope": "SUPPLIER", "supplier_id": 1}, headers={"Idempotency-Key": "key-0001-abcdef"}
    )
    assert other_scope.status_code == 409


def test_only_one_active_run_per_scope(client):
    two_supplier_site(client)
    s = client.app.state.session_factory()
    try:
        runs.create_run(s, RiskRunScope.NETWORK, None, "t")
        s.commit()
        with pytest.raises(ConflictError):
            runs.create_run(s, RiskRunScope.NETWORK, None, "t")
        s.rollback()
        # a different scope is independent
        runs.create_run(s, RiskRunScope.SUPPLIER, {"supplier_id": 1}, "t")
        s.commit()
    finally:
        s.close()


def test_a_dead_runs_lock_is_released_and_reported(client):
    two_supplier_site(client)
    s = client.app.state.session_factory()
    try:
        old, _ = runs.create_run(s, RiskRunScope.NETWORK, None, "t")
        s.commit()
        limits = runs.Limits(stale_lock_seconds=0)
        new, created = runs.create_run(s, RiskRunScope.NETWORK, None, "t", limits=limits)
        s.commit()
        assert created and new.id != old.id
        s.refresh(old)
        assert old.status.value == "FAILED" and "abandoned" in old.error
    finally:
        s.close()


def test_supplier_cap_truncates_and_is_reported_not_hidden(client):
    for i in range(5):
        node(client, "suppliers", f"Capped Supplier {i}")
    run = assess_all(client, limits=runs.Limits(max_suppliers=3))
    assert run.examined == 3 and run.skipped == 2 and run.truncated and "supplier_limit" in run.truncation_reasons
    assert run.status.value == "PARTIAL"


def test_time_budget_stops_cleanly_and_counts_the_rest_as_skipped(client):
    for i in range(4):
        node(client, "suppliers", f"Slow Supplier {i}")
    run = assess_all(client, limits=runs.Limits(time_budget_seconds=-1))
    assert run.truncated and "time_budget" in run.truncation_reasons
    assert run.examined + run.skipped == 4 and run.status.value == "PARTIAL"


def test_one_failing_supplier_does_not_hide_the_others(client, monkeypatch):
    a, b, _f = two_supplier_site(client)
    real = runs.assess

    def flaky(sf, net, today, now, cfg, reach=None):
        if sf.info.id == a["id"]:
            raise RuntimeError("synthetic failure")
        return real(sf, net, today, now, cfg, reach)

    monkeypatch.setattr(runs, "assess", flaky)
    run = assess_all(client)
    assert (
        run.status.value == "PARTIAL"
        and run.failed == 1
        and run.failures == [{"supplier_id": a["id"], "error": "RuntimeError"}]
    )
    assert current(client, b["id"])["assessed"] is True and current(client, a["id"])["assessed"] is False


def test_every_supplier_failing_marks_the_run_failed(client, monkeypatch):
    two_supplier_site(client)

    def boom(*_a, **_k):
        raise ValueError("nope")

    monkeypatch.setattr(runs, "assess", boom)
    run = assess_all(client)
    assert run.status.value == "FAILED" and run.failed == run.examined > 0 and n_assessments(client) == 0


def test_a_failed_store_rolls_back_only_that_supplier(client, monkeypatch):
    a, b, _f = two_supplier_site(client)
    real = runs.service.store

    def store(session, run, sid, *args, **kw):
        if sid == a["id"]:
            session.add(
                RiskAlert(supplier_id=sid, alert_type="BOGUS")
            )  # invalid row forces a flush error inside the savepoint
            session.flush()
        return real(session, run, sid, *args, **kw)

    monkeypatch.setattr(runs.service, "store", store)
    run = assess_all(client)
    assert run.failed == 1 and n_assessments(client, a["id"]) == 0 and n_assessments(client, b["id"]) == 1


def test_merged_supplier_cannot_be_assessed(client):
    from app.core.errors import EntityMergedError

    a, b, _f = two_supplier_site(client)
    with Session(client.app.state.engine) as s, s.begin():
        from app.models.supplier import Supplier
        from app.models.types import utcnow

        row = s.get(Supplier, a["id"])
        row.merged_into_id, row.merged_at = b["id"], utcnow()
    s = client.app.state.session_factory()
    try:
        with pytest.raises(EntityMergedError):
            runs.create_run(s, RiskRunScope.SUPPLIER, {"supplier_id": a["id"]}, "t")
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------ alerts


def make_sole_critical(client):
    """A supplier assessed first with a shared node, then becoming the only confirmed supplier of a critical node."""
    a = node(client, "suppliers", "Alert Subject")
    b = node(client, "suppliers", "Alert Peer")
    c = node(client, "suppliers", "Alert Third")
    f1 = node(client, "facilities", "Alert Plant")
    f2 = node(client, "facilities", "Alert Depot")
    ok(client, "SUPPLIES", a, f1, criticality="CRITICAL")
    peer = ok(client, "SUPPLIES", b, f1, criticality="CRITICAL")
    ok(client, "SUPPLIES", a, f2, criticality="LOW")
    ok(client, "SUPPLIES", c, f2, criticality="LOW")
    return a, b, peer


def test_new_single_source_on_a_critical_node_raises_one_deduplicated_alert(client):
    a, _b, peer = make_sole_critical(client)
    assess_all(client)
    assert client.get(f"{R}/alerts").json()["total"] == 0  # first assessment has nothing to compare with
    r = client.post(
        f"{G}/relationships/{peer['ref']}/status",
        json={"status": "INACTIVE", "reason": "no longer supplies", "expected_version": peer["version"]},
    )
    assert r.status_code == 200, r.text
    assess_all(client)
    alerts = client.get(f"{R}/alerts", params={"supplier_id": a["id"]}).json()
    kinds = [x["alert_type"] for x in alerts["items"]]
    assert "NEW_SINGLE_SOURCE" in kinds
    first = next(x for x in alerts["items"] if x["alert_type"] == "NEW_SINGLE_SOURCE")
    assert "no email" in first["delivery"] and first["status"] == "OPEN" and first["occurrences"] == 1
    assess_all(client)  # unchanged data: no duplicate, no extra occurrence
    again = client.get(f"{R}/alerts", params={"supplier_id": a["id"], "alert_type": "NEW_SINGLE_SOURCE"}).json()
    assert again["total"] == 1 and again["items"][0]["id"] == first["id"]
    with Session(client.app.state.engine) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(RiskAlert)
                .where(RiskAlert.supplier_id == a["id"], RiskAlert.alert_type == "NEW_SINGLE_SOURCE")
            )
            == 1
        )


def test_alert_transition_requires_the_current_version_and_records_who(client):
    a, _b, peer = make_sole_critical(client)
    assess_all(client)
    client.post(
        f"{G}/relationships/{peer['ref']}/status",
        json={"status": "INACTIVE", "reason": "gone", "expected_version": peer["version"]},
    )
    assess_all(client)
    al = client.get(f"{R}/alerts", params={"supplier_id": a["id"], "alert_type": "NEW_SINGLE_SOURCE"}).json()["items"][
        0
    ]
    stale = client.post(f"{R}/alerts/{al['id']}/transition", json={"to": "ACKNOWLEDGED", "version": al["version"] + 5})
    assert stale.status_code == 409
    ok_ = client.post(
        f"{R}/alerts/{al['id']}/transition",
        json={"to": "RESOLVED", "version": al["version"], "note": "contract signed"},
    )
    assert (
        ok_.status_code == 200
        and ok_.json()["status"] == "RESOLVED"
        and ok_.json()["resolved_by"]
        and ok_.json()["resolution_note"] == "contract signed"
    )
    active = client.get(f"{R}/alerts", params={"alert_type": "NEW_SINGLE_SOURCE"}).json()
    assert active["total"] == 0  # the default view lists active alerts only
    assert (
        client.get(f"{R}/alerts", params={"status": "RESOLVED", "alert_type": "NEW_SINGLE_SOURCE"}).json()["total"] == 1
    )


# ------------------------------------------------------------------------------------------------ hooks


def test_relationship_writes_reassess_the_affected_suppliers_automatically(client):
    client.app.state.settings.risk_auto_reassess = True
    a, b, f = two_supplier_site(client)
    extra = node(client, "suppliers", "Late Joiner")
    ok(client, "SUPPLIES", extra, f)
    assert (
        current(client, a["id"])["assessed"]
        and current(client, b["id"])["assessed"]
        and current(client, extra["id"])["assessed"]
    )
    with Session(client.app.state.engine) as s:
        assert s.scalar(select(func.count()).select_from(RiskRun).where(RiskRun.trigger == "AUTO")) >= 1


def test_automatic_reassessment_can_be_disabled(client):
    two_supplier_site(client)
    assert n_assessments(client) == 0


def test_a_failing_hook_never_fails_the_graph_write(client, monkeypatch):
    from app.risk import hooks

    client.app.state.settings.risk_auto_reassess = True

    def boom(*_a, **_k):
        raise RuntimeError("hook")

    monkeypatch.setattr(hooks.runs, "assess_now", boom)
    a, _b, _f = two_supplier_site(client)  # edges were created (ok() asserts 201) despite the hook failing
    assert a["id"]


def test_supplier_edit_reassesses_the_supplier_and_the_registry_write_still_succeeds(client):
    client.app.state.settings.risk_auto_reassess = True
    a, _b, _f = two_supplier_site(client)
    with Session(client.app.state.engine) as s:
        before = s.scalar(select(func.count()).select_from(RiskRun).where(RiskRun.trigger == "AUTO"))
    ver = client.get(f"/api/v1/registry/suppliers/{a['id']}").json()["version"]
    r = client.patch(
        f"/api/v1/registry/suppliers/{a['id']}", json={"expected_version": ver, "country_or_region": "Vietnam"}
    )
    assert r.status_code == 200, r.text
    with Session(client.app.state.engine) as s:
        after = s.scalar(select(func.count()).select_from(RiskRun).where(RiskRun.trigger == "AUTO"))
    assert after > before
    assert cfg_of(client).version == "rm-1"
