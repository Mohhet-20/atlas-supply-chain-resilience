"""Registry, lifecycle governance, evaluation runs, explanations, monitoring and cards over real synthetic lead-time runs."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.modelops import cards, evaluate, explain, monitor, registry
from app.modelops.config import EvalConfig, Limits
from app.models.enums import SimStatus
from app.models.modelops import (
    ModelDiagnostic,
    ModelEvaluation,
    ModelExplanation,
    ModelLifecycleEvent,
    ModelState,
    ModelVersion,
)
from tests.modelops_helpers import issue_assessments, leadtime_env, sync, version_of


@pytest.fixture()
def env(client):
    cm, lead, risk = leadtime_env(client)
    return client, cm, lead, risk


def _s(client):
    return Session(client.app.state.engine)


def test_sync_registers_each_finished_run_once(env):
    client, _cm, lead, risk = env
    first = sync(client)
    assert first["registered"] >= 3  # lead time, risk, rule-based methodology
    again = sync(client)
    assert again["registered"] == 0
    with _s(client) as s:
        rows = list(s.scalars(select(ModelVersion)))
        assert {r.source_ref for r in rows} >= {lead.run_uid, risk.run_uid, "rm-1"}
        for r in rows:
            assert r.lifecycle_state is ModelState.DEVELOPMENT and registry.REF_RE.match(r.artifact_ref)
            assert (
                r.dependency_versions["python"]
                and r.artifact_checksum
                and r.intended_use
                and r.prohibited_use
                and r.limitations
            )
        assert s.scalar(select(ModelLifecycleEvent).where(ModelLifecycleEvent.to_state == "DEVELOPMENT")) is not None


def test_artifact_ref_rejects_paths_and_code():
    for bad in (
        "/etc/passwd",
        "../x",
        "http://x/y",
        "leadtime_run:../../a",
        "pickle:abc",
        "leadtime_run:" + "a" * 80,
        "leadtime_run:a b",
    ):
        with pytest.raises(DomainValidationError):
            registry.validate_ref(bad)
    assert registry.validate_ref("leadtime_run:LT-ABC123")


def test_versions_are_immutable_and_never_deleted(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        mv.target = "tampered"
        with pytest.raises(ValueError):
            s.flush()
        s.rollback()
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        s.delete(mv)
        with pytest.raises(ValueError):
            s.flush()
        s.rollback()
        h = s.scalar(select(ModelLifecycleEvent))
        h.note = "x"
        with pytest.raises(ValueError):
            s.flush()


def test_unique_model_key_and_version(env):
    client, *_ = env
    sync(client)
    with _s(client) as s:
        a = s.scalars(select(ModelVersion)).first()
        dup = ModelVersion(**{c.name: getattr(a, c.name) for c in ModelVersion.__table__.columns if c.name != "id"})
        dup.model_uid = "MV-DUP0000000000001"
        dup.source_ref = "other"
        s.add(dup)
        with pytest.raises(Exception):  # noqa: B017, PT011
            s.flush()


def test_approval_needs_valid_evaluation_rationale_and_role(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        with pytest.raises(InvalidTransitionError):  # cannot skip EVALUATED
            registry.transition(s, mv, "APPROVED", "rev", "reviewer", "looks fine to me, approving now")
        ev = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        assert ev.status is SimStatus.COMPLETED and ev.result_status == "completed"
        s.refresh(mv)
        assert mv.lifecycle_state is ModelState.EVALUATED
        with pytest.raises(InvalidTransitionError):
            registry.transition(s, mv, "APPROVED", "ed", "editor", "this is a long enough rationale")
        with pytest.raises(DomainValidationError):
            registry.transition(s, mv, "APPROVED", "rev", "reviewer", "ok")
        registry.transition(
            s, mv, "APPROVED", "rev", "reviewer", "Reviewed backtest and limitations; fit for planning use."
        )
        s.commit()
        hist = registry.history(s, mv)
        assert [h.to_state for h in hist] == ["DEVELOPMENT", "EVALUATED", "APPROVED"]
        assert hist[-1].evidence["evaluation"] == ev.eval_uid and hist[-1].actor == "rev"
        with pytest.raises(InvalidTransitionError):
            registry.transition(s, mv, "ARCHIVED", "rev", "reviewer", "archive me please now")
        registry.transition(s, mv, "DEPRECATED", "rev", "reviewer", "superseded by a newer run")
        with pytest.raises(InvalidTransitionError):
            registry.transition(s, mv, "ARCHIVED", "rev", "reviewer", "archive after deprecation")
        registry.transition(s, mv, "ARCHIVED", "adm", "admin", "retired")


def test_failed_validity_blocks_approval(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        # an evaluation row is frozen once terminal, so simulate an invalid one by inserting a new terminal row
        bad = ModelEvaluation(eval_uid="EV-BADBADBADBADBAD1", model_version_id=mv.id, kind="RECORDED_BACKTEST", status=SimStatus.COMPLETED, result_status="completed", requested_by="t", config={}, config_hash="x", algorithm_version="mo-1", validity={"overall": "invalid"})  # fmt: skip
        s.add(bad)
        s.flush()
        assert registry.latest_valid_evaluation(s, mv).eval_uid == bad.eval_uid
        with pytest.raises(InvalidTransitionError):
            registry.transition(s, mv, "APPROVED", "rev", "reviewer", "a sufficiently long rationale")
        assert ev.eval_uid != bad.eval_uid


def test_recorded_backtest_content_for_lead_time(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        assert ev.metrics["mae"]["status"] == "ok" and ev.metrics["bias"]["note"]
        assert ev.baselines["entries"] and ev.baselines["note"]
        assert ev.validity["overall"] in ("valid", "valid_with_unchecked")
        assert {c["check"] for c in ev.validity["checks"]} >= {"window_order", "fit_boundaries"}
        assert ev.reproducibility["config_hash"] == ev.config_hash and ev.reproducibility["evidence_hash"]
        assert ev.software["python"] and ev.algorithm_version == "mo-1" and ev.limitations
        # re-running yields the same evidence hash (reproducible) in a new immutable run
        ev2 = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        assert ev2.id != ev.id and ev2.reproducibility["evidence_hash"] == ev.reproducibility["evidence_hash"]


def test_risk_model_scores_are_not_reported_as_probabilities(env):
    client, _cm, _lead, risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == risk.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        assert ev.result_status == "completed"
        calibrated = risk.artifact["score_kind"] == "calibrated_probability"
        assert (ev.metrics["brier"]["status"] == "ok") == calibrated
        if not calibrated:
            assert "score" in ev.metrics["brier"]["note"]
        assert ev.calibration["statement"] and ev.calibration["decision"] in (
            "calibrated_probability",
            "uncalibrated_score",
            None,
        )
        assert ev.metrics["pr_auc"]["status"] in ("ok", "undefined")


def test_rule_based_evaluation_is_reported_unsupported(env):
    client, *_ = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == "rm-1"))
        ev = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        assert ev.result_status == "unsupported" and ev.metrics is None
        assert any("outcome labels" in w for w in ev.warnings)
        s.refresh(mv)
        assert mv.lifecycle_state is ModelState.DEVELOPMENT  # unsupported evidence is not evaluation
        with pytest.raises(InvalidTransitionError):
            registry.transition(s, mv, "APPROVED", "rev", "reviewer", "approve without evidence please")


def test_prospective_with_no_outcomes_is_insufficient_not_zero(env):
    client, *_cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(kind="PROSPECTIVE"), "tester")
        assert ev.result_status == "insufficient_data" and ev.metrics is None
        s.refresh(mv)
        assert mv.lifecycle_state is ModelState.DEVELOPMENT


def test_prospective_with_issued_assessments(env):
    client, cm, lead, _risk = env
    issue_assessments(client, cm, lead, n=6)
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(kind="PROSPECTIVE", segment_by=["supplier", "mode"]), "tester")
        assert ev.status is SimStatus.COMPLETED
        if ev.result_status == "completed":
            assert ev.sample["with_outcome"] >= 1 and ev.baselines["note"].startswith("not available")
            assert ev.segments["unsupported_fields"]["fields"] == ["mode"]
            assert any("indicative" in w for w in ev.warnings)
        else:
            assert ev.result_status == "insufficient_data"


def test_evaluation_idempotency_limits_cancel_and_freeze(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        a, created = evaluate.create(s, mv, EvalConfig(), "u1", Limits(), idempotency_key="k-1")
        s.commit()
        b, created2 = evaluate.create(s, mv, EvalConfig(), "u1", Limits(), idempotency_key="k-1")
        assert created and not created2 and a.id == b.id
        with pytest.raises(ConflictError):
            evaluate.create(s, mv, EvalConfig(kind="PROSPECTIVE"), "u1", Limits(), idempotency_key="k-1")
        with pytest.raises(ConflictError):  # a different actor cannot replay the key
            evaluate.create(s, mv, EvalConfig(), "u2", Limits(), idempotency_key="k-1")
        with pytest.raises(ConflictError):  # active-evaluation limit
            evaluate.create(s, mv, EvalConfig(ece_bins=5), "u1", Limits(max_active_evaluations=1))
        aid = a.id
        evaluate.cancel(s, a)
        s.commit()
        evaluate.execute(client.app.state.session_factory, aid, Limits())  # cancelled stays cancelled
        s.refresh(a)
        assert a.status is SimStatus.CANCELLED
        with pytest.raises(InvalidTransitionError):
            evaluate.cancel(s, a)
        done = evaluate.run_sync(s, mv, EvalConfig(ece_bins=7), "u1")
        done.metrics = {"tampered": True}
        with pytest.raises(ValueError):
            s.flush()
        s.rollback()


def test_budget_exceeded_fails_visibly(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        ev, _ = evaluate.create(s, mv, EvalConfig(), "u", Limits())
        eid = ev.id
        s.commit()
        evaluate.execute(client.app.state.session_factory, eid, Limits(time_budget_seconds=-1))
        s.refresh(ev)
        assert ev.status is SimStatus.FAILED and "budget" in ev.error


def test_eval_config_rejects_unknown_and_organisation_fields():
    for bad in (
        {"organization_id": 3},
        {"tenant": "x"},
        {"min_segment_n": 1},
        {"kind": "FUTURE"},
        {"segment_by": ["ssn"]},
    ):
        with pytest.raises(Exception):  # noqa: B017, PT011
            EvalConfig(**bad)


def test_global_explanation_traceable_cached(env):
    client, _cm, lead, risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        g = explain.global_explanation(s, mv, "stored_global", "tester")
        assert g["status"] == "ok" and g["model_version"] == mv.model_uid and g["statement"] and g["cached"] is False
        assert g["kind"] in ("learned_linear", "transparent_lookup")
        again = explain.global_explanation(s, mv, "stored_global", "tester")
        assert again["cached"] is True
        assert len(list(s.scalars(select(ModelExplanation)))) == 1
        for m in ("shap", "lime", "counterfactual", "causal"):
            u = explain.global_explanation(s, mv, m, "tester")
            assert u["status"] == "unsupported" and u["reason"] and "supported_methods" in u
        with pytest.raises(DomainValidationError):
            explain.global_explanation(s, mv, "made_up", "tester")
        rm = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == "rm-1"))
        w = explain.global_explanation(s, rm, "stored_global", "tester")
        assert w["kind"] == "transparent_weights" and abs(sum(x["weight"] for x in w["weights"]) - 1) < 1e-9


def test_local_explanation_requires_assessment_from_this_model(env):
    client, cm, lead, risk = env
    uids = issue_assessments(client, cm, lead, n=2)
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        other = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == risk.run_uid))
        e = explain.local_explanation(s, mv, uids[0], "stored_local", "tester")
        assert e["status"] == "ok" and e["subject"] == uids[0] and "factors" in e and e["model_version"] == mv.model_uid
        with pytest.raises(NotFoundError):
            explain.local_explanation(s, other, uids[0], "stored_local", "tester")
        with pytest.raises(NotFoundError):
            explain.local_explanation(s, mv, "DA-DOESNOTEXIST", "stored_local", "tester")
        with pytest.raises(DomainValidationError):
            explain.local_explanation(s, mv, "../../etc", "stored_local", "tester")
        assert explain.local_explanation(s, mv, uids[0], "shap", "tester")["status"] == "unsupported"


def test_monitoring_is_observation_only(env):
    client, cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        state = mv.lifecycle_state
        out = monitor.run_check(s, mv, "tester")
        s.commit()
        kinds = {d["kind"] for d in out["diagnostics"]}
        assert {"COVARIATE_DRIFT", "PREDICTION_DRIFT", "PERFORMANCE"} <= kinds
        perf = next(d for d in out["diagnostics"] if d["kind"] == "PERFORMANCE")
        assert perf["status"] == "insufficient_data" and "not evidence of deterioration" in perf["explanation"]
        assert "does not show" in out["statement"]
        s.refresh(mv)
        assert mv.lifecycle_state is state  # drift never changes governance state
        assert s.scalar(select(ModelDiagnostic)) is not None
        d = s.scalar(select(ModelDiagnostic))
        d.status = "ok"
        with pytest.raises(ValueError):
            s.flush()


def test_model_card_is_generated_from_records_and_shows_gaps(env):
    client, _cm, lead, _risk = env
    sync(client)
    with _s(client) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        c0 = cards.build(s, mv)
        assert (
            c0["evaluation"]["recorded_backtest"] == "not recorded"
            and "RECORDED_BACKTEST" in c0["evaluation"]["missing"]
        )
        evaluate.run_sync(s, mv, EvalConfig(), "tester")
        c1 = cards.build(s, mv)
        assert c1["evaluation"]["recorded_backtest"]["metrics"] and c1["evaluation"]["prospective"] == "not recorded"
        assert c1["governance"]["state"] == "EVALUATED" and len(c1["governance"]["history"]) == 2
        assert (
            c1["intended_use"]
            and c1["prohibited_use"]
            and c1["statement"]
            and c1["reproducibility"]["artifact_checksum"]
        )
        assert "path" not in str(c1["reproducibility"]).lower().replace("pathway", "")


def test_version_lookup(env):
    client, _cm, lead, _risk = env
    sync(client)
    mv = version_of(client, lead.run_uid)
    with _s(client) as s:
        assert registry.get(s, mv.model_uid).id == mv.id
        with pytest.raises(NotFoundError):
            registry.get(s, "MV-NOPE")


def test_prospective_evaluations_with_matured_outcomes(env):
    client, cm, lead, risk = env
    from tests.modelops_helpers import mature_assessments

    uids = mature_assessments(client, cm, lead, risk)
    assert len(uids) >= 20, "synthetic open orders must be assessable"
    sync(client)
    with _s(client) as s:
        lv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == lead.run_uid))
        ev = evaluate.run_sync(s, lv, EvalConfig(kind="PROSPECTIVE", segment_by=["supplier", "lane"]), "tester")
        assert ev.result_status == "completed", (ev.warnings, ev.sample)
        assert ev.sample["with_outcome"] >= 20 and ev.metrics["mae"]["status"] == "ok" and ev.metrics["bias"]["note"]
        assert ev.baselines["note"].startswith("not available")
        assert ev.segments["unsupported_fields"]["fields"] == ["lane"] and "suppressed" in ev.segments["supplier"]
        assert ev.intervals is None or ev.intervals["coverage"]["status"] in ("ok", "insufficient_data")
        rv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == risk.run_uid))
        ev2 = evaluate.run_sync(s, rv, EvalConfig(kind="PROSPECTIVE"), "tester")
        assert ev2.result_status == "completed", (ev2.warnings, ev2.sample)
        probabilistic = risk.artifact["score_kind"] == "calibrated_probability"
        assert (ev2.metrics["brier"]["status"] == "ok") == probabilistic
        assert ev2.metrics["prevalence"]["status"] == "ok" and ev2.sample["with_outcome"] >= 20
        if not probabilistic:
            assert (
                ev2.calibration["status"] == "not_applicable"
                and "not a calibrated probability" in ev2.calibration["note"]
            )
        # monitoring now has labelled prospective evidence, but 30+ matured outcomes are still required before any verdict
        out = monitor.run_check(s, lv, "tester")
        perf = next(d for d in out["diagnostics"] if d["kind"] == "PERFORMANCE")
        assert perf["status"] in ("ok", "warning", "insufficient_data")
