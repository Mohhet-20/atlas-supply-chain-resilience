"""Forecast run lifecycle, persistence, immutability, evaluation and monitoring (synthetic data only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, DomainValidationError, InvalidTransitionError, NotFoundError
from app.forecast import lifecycle, runs
from app.forecast.config import ForecastConfig
from app.models.enums import SimStatus
from app.models.forecast import ForecastAlert, ForecastRun, ForecastValue
from tests.forecast_helpers import (
    TODAY,
    code,
    d,
    forecast,
    load_demand,
    pid,
    series_rows,
    stable,
    synthetic_world,
    values_of,
)


def _fresh(client, rid):
    with Session(client.app.state.engine) as s:
        r = s.get(ForecastRun, rid)
        s.expunge(r)
        return r


def test_run_completes_and_persists_everything(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p1", "f1")
    assert r.status in (SimStatus.COMPLETED, SimStatus.PARTIAL) and r.result_status in (
        "completed",
        "completed_with_fallback",
    ), (r.error, r.warnings)
    assert r.origin == TODAY and r.unit == "EA" and r.provenance == "MODEL_ESTIMATE"
    vals = values_of(client, r.id)
    assert len(vals) == 14 and vals[0].period_start == TODAY and vals[-1].period_end == TODAY + timedelta(days=14)
    assert all(v.point >= 0 and v.point_raw is not None for v in vals)
    assert r.selected_model and r.model_version and r.data_snapshot_hash and r.config_hash
    assert r.reproducibility["algorithm_version"] == "fc-1" and r.reproducibility["numeric_libraries"].startswith(
        "none"
    )
    assert r.validation["leakage_controls"] and r.evaluation and r.selection["rationale"]
    assert r.patterns["labels"] and r.diagnostics["n"] > 100
    # no number is invented: the training interval ends exactly at the origin
    assert r.training_interval["end_exclusive"] == TODAY.isoformat()


def test_same_data_same_config_is_reproducible(client):
    w = synthetic_world(client)
    a, b = forecast(client, w, "p2", "f1"), forecast(client, w, "p2", "f1")
    assert a.id != b.id and a.data_snapshot_hash == b.data_snapshot_hash and a.selected_model == b.selected_model
    assert [v.point for v in values_of(client, a.id)] == [v.point for v in values_of(client, b.id)]


def test_terminal_runs_values_and_alerts_are_immutable(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p1", "f1")
    with Session(client.app.state.engine) as s:
        row = s.get(ForecastRun, r.id)
        row.selected_model = "naive"
        with pytest.raises(ValueError, match="immutable"):
            s.flush()
        s.rollback()
        row = s.get(ForecastRun, r.id)
        s.delete(row)
        with pytest.raises(ValueError, match="cannot be deleted"):
            s.flush()
        s.rollback()
        v = s.scalars(select(ForecastValue).where(ForecastValue.run_id == r.id)).first()
        v.point = 1.0
        with pytest.raises(ValueError, match="append-only"):
            s.flush()


def test_idempotency_and_conflict(client):
    w = synthetic_world(client)
    f = client.app.state.session_factory
    lim = runs.RunLimits()
    conf = ForecastConfig(horizon=7)
    with f() as s, s.begin():
        a, created = runs.create_run(
            s, pid(client, w, "p1"), pid(client, w, "f1"), conf, "u", lim, idempotency_key="k1"
        )
        aid = a.id
    assert created
    with f() as s, s.begin():
        b, created2 = runs.create_run(
            s, pid(client, w, "p1"), pid(client, w, "f1"), conf, "u", lim, idempotency_key="k1"
        )
        assert b.id == aid and not created2
    with f() as s, pytest.raises(ConflictError):
        runs.create_run(
            s, pid(client, w, "p1"), pid(client, w, "f1"), ForecastConfig(horizon=8), "u", lim, idempotency_key="k1"
        )
    with f() as s, pytest.raises(ConflictError):
        runs.create_run(s, pid(client, w, "p1"), pid(client, w, "f1"), conf, "someone-else", lim, idempotency_key="k1")


def test_validation_errors_are_domain_errors(client):
    w = synthetic_world(client)
    f = client.app.state.session_factory
    lim = runs.RunLimits()
    with f() as s, pytest.raises(NotFoundError):
        runs.create_run(s, 99999, pid(client, w, "f1"), ForecastConfig(), "u", lim)
    with f() as s, pytest.raises(DomainValidationError, match="limit"):
        runs.create_run(s, pid(client, w, "p1"), pid(client, w, "f1"), ForecastConfig(horizon=200), "u", lim)
    # a pair with no demand observations has nothing to forecast
    from tests.graph_helpers import node

    p9 = node(client, "products", "Zinc Plate", unit_of_measure="EA")
    with f() as s, pytest.raises(DomainValidationError, match="nothing to forecast"):
        runs.create_run(s, int(p9["id"]), pid(client, w, "f1"), ForecastConfig(), "u", lim)


def test_cancel_queued_running_and_finished(client):
    w = synthetic_world(client)
    f = client.app.state.session_factory
    lim = runs.RunLimits()
    with f() as s, s.begin():
        a, _ = runs.create_run(s, pid(client, w, "p1"), pid(client, w, "f1"), ForecastConfig(horizon=7), "u", lim)
        runs.cancel_run(s, a)
        assert a.status is SimStatus.CANCELLED
    with f() as s, s.begin():
        b, _ = runs.create_run(s, pid(client, w, "p1"), pid(client, w, "f1"), ForecastConfig(horizon=7), "u", lim)
        b.cancel_requested = True  # a cancel requested while the run is active is honoured at the next checkpoint
        bid = b.id
    runs.execute_run(f, bid, lim)
    assert _fresh(client, bid).status is SimStatus.CANCELLED and not values_of(client, bid)
    done = forecast(client, w, "p1", "f1")
    with f() as s, pytest.raises(InvalidTransitionError):
        runs.cancel_run(s, s.get(ForecastRun, done.id))


def test_active_limit_and_budget(client):
    w = synthetic_world(client)
    f = client.app.state.session_factory
    with f() as s, s.begin():
        runs.create_run(
            s, pid(client, w, "p1"), pid(client, w, "f1"), ForecastConfig(horizon=7), "u", runs.RunLimits(max_active=1)
        )
    with f() as s, pytest.raises(ConflictError, match="already queued"):
        runs.create_run(
            s, pid(client, w, "p1"), pid(client, w, "f1"), ForecastConfig(horizon=7), "u", runs.RunLimits(max_active=1)
        )
    r = forecast(client, w, "p2", "f1", limits=runs.RunLimits(time_budget_seconds=1e-9))
    assert r.status is SimStatus.FAILED and r.error == "compute budget exceeded" and not values_of(client, r.id)


def test_insufficient_history_declines_to_forecast(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p3", "f2")
    assert r.status is SimStatus.COMPLETED and r.result_status == "insufficient_data"
    assert not values_of(client, r.id) and any("No forecast was produced" in x for x in r.warnings)


def test_missing_period_is_imputed_recorded_and_not_zero(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p3", "f1")
    imps = r.preparation["imputations"]
    assert len(imps) == 3 and all(i["method"] == "interpolate_linear" and i["value"] > 10 for i in imps)
    assert r.preparation["counts"]["missing_periods"] == 3
    assert any("imputed" in x for x in r.warnings)


def test_intermittent_series_is_labelled_and_compared_with_baselines(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p2", "f2")
    assert "intermittent" in r.patterns["labels"] and "croston_sba" in r.candidates["evaluated"]
    assert r.selection["status"] in ("selected", "no_reliable_winner")
    assert "naive" in r.evaluation  # the baseline is always evaluated on the same folds


def test_forecast_vs_actual_evaluation_never_rewrites_the_forecast(client):
    w = synthetic_world(client)
    # forecast issued 20 days ago from data that existed then; later actuals already exist in the demand table
    origin = TODAY - timedelta(days=20)
    r = forecast(client, w, "p1", "f1", ForecastConfig(horizon=14, n_folds=5, origin=origin))
    assert r.origin == origin
    before = [(v.period_start, v.point, v.point_raw) for v in values_of(client, r.id)]
    f = client.app.state.session_factory
    with f() as s, s.begin():
        ev, created = lifecycle.evaluate_run(s, s.get(ForecastRun, r.id), "tester", 7)
        assert created and ev.status == "evaluated"
        ms = ev.matches
        # forecast days 1..13 ended >= 7 days ago -> final; the last day ended 6 days ago -> preliminary
        assert [m["status"] for m in ms].count("final") == 13 and ms[-1]["status"] == "preliminary"
        fin = [m for m in ms if m["status"] == "final"]
        mae = sum(abs(m["forecast"] - m["actual"]) for m in fin) / len(fin)
        assert ev.metrics["final"]["mae"]["value"] == pytest.approx(mae)
        assert ev.metrics["final"]["bias"]["value"] == pytest.approx(
            sum(m["forecast"] - m["actual"] for m in fin) / len(fin)
        )
        assert ev.metrics["bias_convention"].startswith("forecast")
        eid = ev.id
    with f() as s, s.begin():
        again, created2 = lifecycle.evaluate_run(s, s.get(ForecastRun, r.id), "tester", 7)
        assert again.id == eid and not created2  # same actuals -> same snapshot, nothing duplicated
    assert [(v.period_start, v.point, v.point_raw) for v in values_of(client, r.id)] == before


def test_no_actuals_yet_is_reported_not_scored(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p1", "f1")  # origin today: no actuals exist yet
    with client.app.state.session_factory() as s, s.begin():
        ev, _ = lifecycle.evaluate_run(s, s.get(ForecastRun, r.id), "tester", 7)
        assert ev.status == "no_actuals" and ev.metrics is None and ev.n_matched == 0


def test_monitoring_flags_stale_forecast_once_and_never_retrains(client):
    w = synthetic_world(client)
    r = forecast(client, w, "p1", "f1", ForecastConfig(horizon=7, n_folds=5, origin=TODAY - timedelta(days=30)))
    f = client.app.state.session_factory
    with f() as s, s.begin():
        found = lifecycle.monitor(s, "tester", 7)
        assert [a.kind for a in found].count("stale_forecast") == 1
    with f() as s, s.begin():
        assert lifecycle.monitor(s, "tester", 7) == []  # de-duplicated by fingerprint
        assert s.scalar(select(ForecastRun.id).where(ForecastRun.id != r.id)) is None  # nothing was retrained
        assert s.scalars(select(ForecastAlert).where(ForecastAlert.run_id == r.id)).first().evidence["horizon_end"]


def test_monitoring_sustained_bias_needs_minimum_samples(client):
    w = synthetic_world(client)
    # P3@F2 has only 8 days; build a fresh series whose level jumps 60% after the forecast origin
    from tests.graph_helpers import node

    p = node(client, "products", "Level Shift Item", unit_of_measure="EA")
    w["p9"] = p
    base = stable(100, 9, 50.0)
    shifted = [v * 1.6 for v in stable(30, 10, 50.0)]
    load_demand(client, series_rows(w, "p9", "f1", base + shifted))
    origin = TODAY - timedelta(days=30)
    r = forecast(client, w, "p9", "f1", ForecastConfig(horizon=21, n_folds=5, origin=origin))
    assert r.status in (SimStatus.COMPLETED, SimStatus.PARTIAL), r.warnings
    f = client.app.state.session_factory
    with f() as s, s.begin():
        found = lifecycle.monitor(s, "tester", 7)
        assert "sustained_bias" in [a.kind for a in found]
        al = next(a for a in found if a.kind == "sustained_bias")
        assert al.evidence["n"] >= lifecycle.MIN_EVAL_N and al.evidence["relative_bias"] < -0.2  # under-forecast
    # a recent forecast with too few final periods raises no bias alert
    r2 = forecast(client, w, "p9", "f1", ForecastConfig(horizon=7, n_folds=5, origin=TODAY - timedelta(days=9)))
    with f() as s, s.begin():
        ev, _ = lifecycle.evaluate_run(s, s.get(ForecastRun, r2.id), "t", 7)
        assert ev.n_final < lifecycle.MIN_EVAL_N


def test_clock_arg_is_utc_aware():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert lifecycle._utc_date(now).isoformat() == "2026-01-01"
    assert d(0) == TODAY.isoformat() and code
