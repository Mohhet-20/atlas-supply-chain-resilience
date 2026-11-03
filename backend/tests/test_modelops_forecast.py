"""Forecast models in the unified registry: recorded backtests with same-fold baselines, prospective actuals, monitoring."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecast import lifecycle
from app.forecast.config import ForecastConfig
from app.modelops import cards, evaluate, explain, monitor, registry
from app.modelops.config import EvalConfig
from app.models.forecast import ForecastRun
from app.models.modelops import ModelVersion
from tests.forecast_helpers import TODAY, forecast, synthetic_world


def _setup(client):
    w = synthetic_world(client)
    run = forecast(client, w, "p1", "f1", ForecastConfig(horizon=14, n_folds=5))
    with Session(client.app.state.engine) as s:
        out = registry.sync(s, "tester")
        s.commit()
    return run, out


def test_forecast_run_registered_with_documented_metadata(client):
    run, out = _setup(client)
    assert out["registered"] >= 1
    with Session(client.app.state.engine) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == run.run_uid))
        assert (
            mv.task.value == "FORECASTING"
            and mv.domain == "DEMAND"
            and mv.model_key == f"forecast/{run.selected_model}"
        )
        assert mv.artifact_ref == f"forecast_run:{run.run_uid}" and mv.data_snapshot_hash == run.data_snapshot_hash
        assert mv.operating_conditions["horizon"] == 14 and mv.limitations and mv.prohibited_use


def test_recorded_backtest_compares_baselines_on_same_folds(client):
    run, _ = _setup(client)
    with Session(client.app.state.engine) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == run.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(), "tester")
        assert ev.result_status == "completed" and ev.metrics["mae"]["status"] == "ok"
        assert ev.baselines["note"].startswith("Baselines were scored on the same")
        assert ev.folds and all("mae" in f for f in ev.folds)
        assert {c["check"] for c in ev.validity["checks"]} >= {"fold_order", "fit_boundaries"}
        assert ev.segments["horizon"] and "individual records" in ev.segments["note"]
        fixed = (run.validation or {}).get("fold_train_lengths")
        assert ev.split["fold_train_lengths"] == fixed
        card = cards.build(s, mv)
        assert (
            card["training"]["data_snapshot_hash"] == run.data_snapshot_hash
            and card["evaluation"]["recorded_backtest"]["metrics"]
        )


def test_prospective_waits_for_final_actuals(client):
    run, _ = _setup(client)
    with Session(client.app.state.engine) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == run.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(kind="PROSPECTIVE"), "tester")
        assert (
            ev.result_status == "insufficient_data"
            and ev.metrics is None
            and any("No forecast-versus-actual" in w for w in ev.warnings)
        )


def test_prospective_scores_final_actuals_without_a_baseline(client):
    run, _ = _setup(client)
    with Session(client.app.state.engine) as s:
        r = s.get(ForecastRun, run.id)
        later = TODAY + timedelta(days=400)
        lifecycle.evaluate_run(s, r, "tester", 7, now=datetime.combine(later, time(), tzinfo=timezone.utc))
        s.commit()
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == run.run_uid))
        ev = evaluate.run_sync(s, mv, EvalConfig(kind="PROSPECTIVE"), "tester")
        assert ev.status.value == "COMPLETED"
        assert ev.result_status in ("completed", "insufficient_data")
        if ev.result_status == "completed":
            assert ev.baselines["note"].startswith("not available") and ev.sample["final_periods"] >= 1
            assert any("indicative" in w for w in ev.warnings)


def test_forecast_explanations_are_parameters_not_attributions(client):
    run, _ = _setup(client)
    with Session(client.app.state.engine) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == run.run_uid))
        g = explain.global_explanation(s, mv, "stored_global", "tester")
        assert (
            g["status"] == "ok"
            and g["kind"] == "fitted_parameters"
            and any("do not explain why" in x for x in g["limitations"])
        )
        loc = explain.local_explanation(s, mv, "anything-1", "stored_local", "tester")
        assert loc["status"] == "unsupported" and "decomposed" in loc["reason"]


def test_forecast_monitoring_reports_data_signals_only(client):
    run, _ = _setup(client)
    with Session(client.app.state.engine) as s:
        mv = s.scalar(select(ModelVersion).where(ModelVersion.source_ref == run.run_uid))
        out = monitor.run_check(s, mv, "tester")
        kinds = {d["kind"] for d in out["diagnostics"]}
        assert {"TARGET_DRIFT", "PERFORMANCE"} <= kinds
        perf = next(d for d in out["diagnostics"] if d["kind"] == "PERFORMANCE")
        assert perf["status"] == "insufficient_data"
