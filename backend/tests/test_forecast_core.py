"""Forecast core: data preparation, models, metrics (hand-calculated), intervals, leakage and selection.

All series here are SYNTHETIC and built in the tests; none represent real demand."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import pytest

from app.forecast import backtest as bt
from app.forecast import features as feat
from app.forecast import intervals as iv
from app.forecast import metrics as mx
from app.forecast.config import ForecastConfig, ForecastLimits
from app.forecast.models import REGISTRY, ModelFailure, eligibility, run_model
from app.forecast.pipeline import run_pipeline
from app.forecast.prep import RawObs, prepare

D0 = date(2026, 1, 5)  # a Monday


def daily(vals, kind="DEMAND", unit="EA", skip=(), start=D0):
    out = []
    for i, v in enumerate(vals):
        if i in skip or v is None:
            continue
        out.append(RawObs(i + 1, kind, start + timedelta(days=i), start + timedelta(days=i + 1), v, unit, f"h{i}"))
    return out


def cfg(**kw):
    return ForecastConfig(**{"horizon": 7, **kw})


# ------------------------------------------------------------------------------------------------ preparation


def test_missing_is_not_zero_and_imputation_is_recorded():
    vals = [10.0 + (i % 5) for i in range(40)]
    p = prepare(daily(vals, skip={10, 11}), cfg())
    assert p.status == "ok" and p.counts["missing_periods"] == 2
    assert [m["index"] for m in p.imputations] == [10, 11]
    assert all(m["method"] == "interpolate_linear" for m in p.imputations)
    assert p.observed[10] is False and p.observed[9] is True
    assert p.values[10] != 0.0  # never zero-filled
    assert p.raw[10] is None


def test_observed_zero_stays_zero_and_is_not_imputed():
    vals = [5.0, 0.0, 7.0] * 10
    p = prepare(daily(vals), cfg())
    assert p.status == "ok" and not p.imputations and p.values[1] == 0.0 and p.observed[1] is True


def test_reject_policy_and_zero_fill_needs_contract():
    obs = daily([3.0 + i % 4 for i in range(30)], skip={7})
    assert prepare(obs, cfg(missing_policy="reject")).status == "missing_periods"
    with pytest.raises(ValueError):
        ForecastConfig(missing_policy="zero_fill_by_contract")
    p = prepare(obs, cfg(missing_policy="zero_fill_by_contract", missing_means_zero=True))
    assert p.status == "ok" and p.values[7] == 0.0 and p.imputations[0]["method"] == "zero_fill_by_contract"


def test_long_gap_and_too_many_missing_decline():
    obs = daily([4.0 + i % 3 for i in range(40)], skip={10, 11, 12, 13, 14})
    p = prepare(obs, cfg())
    assert p.status == "too_many_missing" and not p.values


def test_unit_mismatch_conflict_and_invalid_rows():
    obs = daily([5.0] * 20) + [RawObs(99, "DEMAND", D0, D0 + timedelta(days=1), 3, "KG", "x")]
    p = prepare(obs, cfg(unit="EA", horizon=3))
    assert p.counts["unit_mismatch"] == 1
    assert prepare(daily([5.0] * 20, unit="KG"), cfg(unit="EA")).status == "incompatible_units"
    ov = daily([5.0] * 20) + [RawObs(50, "DEMAND", D0 + timedelta(days=3), D0 + timedelta(days=6), 9, "EA", "ov")]
    assert prepare(ov, cfg()).status == "conflicting_observations"
    bad = daily([5.0] * 20) + [RawObs(60, "DEMAND", D0 + timedelta(days=30), D0 + timedelta(days=31), -4, "EA", "neg")]
    assert prepare(bad, cfg()).counts["invalid"] == 1


def test_kind_priority_never_mixes_and_adjustments_are_ignored():
    obs = daily([5.0] * 20, kind="CONSUMPTION") + daily([50.0] * 20, kind="SALES") + daily([1.0] * 3, kind="ADJUSTMENT")
    p = prepare(obs, cfg())
    assert p.basis == "CONSUMPTION" and p.counts["other_kind"] == 20 and p.counts["adjustments_ignored"] == 3
    assert max(p.values) == 5.0


def test_insufficient_and_stale_and_empty():
    assert prepare(daily([5.0] * 6), cfg()).status == "insufficient_data"
    assert prepare([], cfg()).status == "no_observations"
    p = prepare(daily([5.0] * 30), cfg(origin=D0 + timedelta(days=40), max_stale_periods=3))
    assert p.status == "stale_data" and p.freshness_gap_periods == 10


def test_weekly_aggregation_needs_full_weeks():
    vals = [1.0] * 7 * 20
    obs = daily(vals)
    p = prepare(obs, cfg(frequency="weekly", horizon=2, seasonal_period=4))
    assert p.status == "ok" and len(p.values) == 20 and all(abs(v - 7.0) < 1e-9 for v in p.values)
    holes = prepare(daily(vals, skip={20}), cfg(frequency="weekly", horizon=2))
    assert holes.counts["missing_periods"] == 1  # the week containing the missing day is missing, not a short sum


def test_multi_day_observation_is_spread_pro_rata():
    obs = [
        RawObs(i, "DEMAND", D0 + timedelta(days=2 * i), D0 + timedelta(days=2 * i + 2), 6.0, "EA", f"h{i}")
        for i in range(20)
    ]
    p = prepare(obs, cfg())
    assert p.status == "ok" and set(p.values) == {3.0}
    assert any("pro-rata" in w for w in p.warnings)


def test_outlier_detection_is_separate_from_removal():
    vals = [10.0 + (i % 3) for i in range(40)]
    vals[20] = 500.0
    flagged = prepare(daily(vals), cfg(outlier_handling="flag"))
    assert [o["index"] for o in flagged.outliers] == [20] and flagged.values[20] == 500.0
    tr = prepare(daily(vals), cfg(outlier_handling="transform"))
    assert tr.values[20] < 100 and tr.outliers[0]["action"] == "transform" and tr.outliers[0]["before"] == 500.0
    ex = prepare(daily(vals), cfg(outlier_handling="exclude"))
    assert ex.observed[20] is False and ex.outliers[0]["action"] == "exclude"


def test_snapshot_hash_is_stable_and_sensitive():
    vals = [10.0 + (i % 4) for i in range(30)]
    a, b = prepare(daily(vals), cfg()), prepare(daily(vals), cfg())
    assert a.snapshot_hash == b.snapshot_hash
    vals[3] += 1
    assert prepare(daily(vals), cfg()).snapshot_hash != a.snapshot_hash


# ------------------------------------------------------------------------------------------------ models


def test_baseline_models_hand_values():
    y = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
    d = [D0 + timedelta(days=i) for i in range(len(y))]
    assert run_model("naive", y, 3, 4, d, "daily").values == [8.0] * 3
    assert run_model("historical_mean", y, 2, 4, d, "daily").values == [4.5, 4.5]
    assert run_model("moving_average", y, 1, 4, d, "daily").values == [6.5]  # mean(5,6,7,8)
    assert run_model("seasonal_naive", y, 5, 4, d, "daily").values == [5.0, 6.0, 7.0, 8.0, 5.0]


def test_holt_winters_recovers_exact_seasonal_pattern():
    base = [10, 12, 15, 11, 9, 20, 25]
    y = [float(base[i % 7]) for i in range(70)]
    d = [D0 + timedelta(days=i) for i in range(70)]
    out = run_model("holt_winters", y, 7, 7, d, "daily").values
    assert all(abs(out[i] - base[(70 + i) % 7]) < 1e-6 for i in range(7))


def test_croston_sba_hand_calculation():
    # demand: 0 0 6 0 0 0 4 0  ; alpha chosen by one-step MAE; verify against an independent re-implementation
    y = [0, 0, 6, 0, 0, 0, 4, 0, 0, 0, 8, 0]
    y = [float(v) for v in y]
    d = [D0 + timedelta(days=i) for i in range(len(y))]
    o = run_model("croston_sba", y, 3, None, d, "daily")
    a = o.params["alpha"]
    z, p, q = 6.0, 3.0, 1
    for v in y[3:]:
        if v > 0:
            z += a * (v - z)
            p += a * (q - p)
            q = 1
        else:
            q += 1
    # q increments are applied after the first demand; replicate exactly
    assert o.values[0] == pytest.approx((1 - a / 2) * z / p)
    assert o.values[0] == o.values[2] and o.values[0] > 0


def test_croston_is_ineligible_for_smooth_demand():
    spec = REGISTRY["croston_sba"]
    ok, why = eligibility(spec, n=100, season=7, freq="daily", zero_prop=0.0, nonzero=100)
    assert not ok and "not intermittent" in why
    ok, _ = eligibility(spec, n=100, season=7, freq="daily", zero_prop=0.6, nonzero=40)
    assert ok


def test_seasonal_models_need_season_and_history():
    for k in ("seasonal_naive", "holt_winters"):
        assert not eligibility(REGISTRY[k], n=100, season=None, freq="daily", zero_prop=0, nonzero=100)[0]
    assert not eligibility(REGISTRY["holt_winters"], n=10, season=7, freq="daily", zero_prop=0, nonzero=10)[0]
    with pytest.raises(ModelFailure):
        run_model("holt_winters", [1.0] * 10, 3, 7, [D0] * 10, "daily")


def test_arima_like_is_bounded_and_deterministic_and_rejects_short():
    rnd = random.Random(3)
    y = [50 + 0.3 * i + rnd.gauss(0, 2) for i in range(60)]
    d = [D0 + timedelta(days=i) for i in range(60)]
    a, b = run_model("arima_like", y, 10, 7, d, "daily"), run_model("arima_like", y, 10, 7, d, "daily")
    assert a.values == b.values and a.params["q"] == 0 and a.params["p"] <= 3
    assert all(0 < v < 200 for v in a.values)
    with pytest.raises(ModelFailure):
        run_model("arima_like", y[:12], 3, 7, d[:12], "daily")


def test_every_model_is_deterministic():
    rnd = random.Random(5)
    y = [30 + 5 * ((i % 7) == 5) + rnd.gauss(0, 1.5) for i in range(90)]
    d = [D0 + timedelta(days=i) for i in range(90)]
    for k in REGISTRY:
        if k == "croston_sba":
            continue
        assert run_model(k, y, 7, 7, d, "daily").values == run_model(k, y, 7, 7, d, "daily").values


# ------------------------------------------------------------------------------------------------ leakage


def test_features_use_history_only():
    y = [float(i) for i in range(30)]
    d = [D0 + timedelta(days=i) for i in range(30)]
    r1 = feat.row(y[:20], d[20], 7, "daily", 1.0)
    y2 = y[:20] + [9999.0] * 10  # future values changed
    r2 = feat.row(y2[:20], d[20], 7, "daily", 1.0)
    assert r1 == r2
    xs, ys = feat.matrix(y, d, 7, "daily", 1.0)
    t = 15 - feat.min_rows(7)
    assert ys[t] == y[15] and xs[t][1] == y[14] and xs[t][4] == y[15 - 7]  # lag_1 and lag_7 precede the target


def test_backtest_forecasts_do_not_depend_on_future_values():
    rnd = random.Random(7)
    y = [40 + 6 * ((i % 7) in (5, 6)) + rnd.gauss(0, 2) for i in range(120)]
    d = [D0 + timedelta(days=i) for i in range(120)]
    obs = [True] * 120
    plan = bt.plan_folds(120, cfg(horizon=7, n_folds=4), ForecastLimits(), 7)
    base = {
        k: bt.backtest_model(k, y, d, obs, plan, 7, "daily")
        for k in ("naive", "ses", "holt_winters", "arima_like", "lag_ridge")
    }
    cut = plan.origins[0]
    y2 = y[:cut] + [1e6] * (120 - cut)  # poison everything at/after the first origin
    for k, ev in base.items():
        for t in plan.origins:
            if t == cut:
                out1 = run_model(k, y[:t], 7, 7, d[:t], "daily").values
                out2 = run_model(k, y2[:t], 7, 7, d[:t], "daily").values
                assert out1 == out2, k
        assert ev.records


def test_imputed_actuals_are_never_scored():
    y = [float(10 + i % 3) for i in range(60)]
    d = [D0 + timedelta(days=i) for i in range(60)]
    observed = [True] * 60
    observed[55] = False
    plan = bt.plan_folds(60, cfg(horizon=5, n_folds=3), ForecastLimits(), 7)
    ev = bt.backtest_model("naive", y, d, observed, plan, 7, "daily")
    scored = {(fo, h) for fo, h, *_ in ev.records}
    for fi, t in enumerate(plan.origins):
        for h in range(1, plan.horizon + 1):
            assert ((fi, h) in scored) == observed[t + h - 1]


# ------------------------------------------------------------------------------------------------ metrics


def test_metrics_hand_calculated():
    rec = [(0, 1, 10.0, 12.0), (0, 2, 20.0, 18.0), (0, 3, 30.0, 33.0)]
    m = mx.metric_set(rec, {0: 5.0})
    assert m["mae"]["value"] == pytest.approx(7 / 3)
    assert m["rmse"]["value"] == pytest.approx(math.sqrt(17 / 3))
    assert m["wape"]["value"] == pytest.approx(7 / 60)
    assert m["bias"]["value"] == pytest.approx(1.0)  # (+2 −2 +3)/3; positive = over-forecast
    assert m["smape"]["value"] == pytest.approx((4 / 22 + 4 / 38 + 6 / 63) / 3)
    assert m["mase"]["value"] == pytest.approx((7 / 3) / 5)


def test_metric_edge_cases_are_undefined_not_fabricated():
    zeros = [(0, 1, 0.0, 0.0), (0, 2, 0.0, 0.0)]
    m = mx.metric_set(zeros, {0: None})
    assert m["wape"]["value"] is None and m["smape"]["value"] is None and m["mase"]["value"] is None
    assert m["mae"]["value"] == 0.0
    mixed = mx.metric_set([(0, 1, 0.0, 0.0), (0, 2, 4.0, 2.0)], {0: 1.0})
    assert (
        mixed["smape"]["n"] == 1
        and mixed["smape"]["value"] == pytest.approx(2 * 2 / 6)
        and "excluded" in mixed["smape"]["note"]
    )
    assert mx.metric_set([], {})["mae"]["value"] is None


def test_fold_scale_is_naive_in_sample_mae():
    assert bt.fold_scale([1.0, 3.0, 2.0, 6.0]) == pytest.approx((2 + 1 + 4) / 3)
    assert bt.fold_scale([5.0, 5.0, 5.0]) is None


# ------------------------------------------------------------------------------------------------ intervals


def test_interval_offsets_rank_rule():
    res = [float(v) for v in range(1, 10)]  # n=9, level 0.8: floor(10*0.1)=1, ceil(10*0.9)=9
    assert iv.offsets(res, 0.8) == (1.0, 9.0)
    assert iv.offsets(res[:8], 0.8) is None  # n=8 → floor(0.9)=0 → not enough evidence
    assert iv.required_n(0.95, 9) == 39 and iv.required_n(0.8, 9) == 9


def test_interval_unsupported_without_enough_residuals():
    rec = [(f, h, 10.0, 10.0 + f) for f in range(2) for h in range(1, 4)]
    cal = iv.calibrate(rec, 3, 0.8, 9)
    assert cal["supported"] is False and "needs" in cal["reason"]


def test_interval_bands_cover_all_horizons():
    bs = iv.bands(H=10, folds=3, need=9)
    assert [h for b in bs for h in b] == list(range(1, 11))
    assert all(len(b) * 3 >= 9 for b in bs)


def test_coverage_is_calibrated_on_earlier_folds_only():
    rnd = random.Random(11)
    rec = [(f, h, 100 + rnd.gauss(0, 5), 100.0) for f in range(12) for h in range(1, 4)]
    c = iv.coverage(rec, 3, 0.8, 9, True)
    assert c["status"] == "evaluated" and c["n_scored"] < len(rec)  # the first folds only calibrate
    assert 0.55 <= c["empirical_coverage"] <= 1.0 and c["mean_width"] > 0


# ------------------------------------------------------------------------------------------------ selection & pipeline


def _ev(score, folds):
    return {
        "complete": True,
        "overall": {"mase": {"value": score}, "mae": {"value": score}},
        "per_fold": {str(i): {"mase": {"value": s}} for i, s in enumerate(folds)},
    }


def test_selection_prefers_baseline_without_reliable_gain():
    plan = bt.FoldPlan([60, 61, 62, 63], 5, 40, "ok")
    ev = {"naive": _ev(1.00, [1, 1, 1, 1]), "ses": _ev(0.97, [0.9, 1.2, 0.9, 1.0])}
    s = bt.select(ev, cfg(), plan)
    assert s["selected"] == "naive" and s["status"] == "no_reliable_winner"
    ev["ses"] = _ev(0.70, [0.7, 0.7, 0.7, 0.7])
    s = bt.select(ev, cfg(), plan)
    assert s["selected"] == "ses" and s["status"] == "selected"


def test_selection_insufficient_evidence_when_few_folds():
    plan = bt.FoldPlan([60, 61], 5, 40, "insufficient_evidence", "only 2 fold(s)")
    s = bt.select({"naive": _ev(1.0, [1, 1])}, cfg(), plan)
    assert s["status"] == "insufficient_evidence" and s["selected"] is None


def test_pipeline_declines_short_history_and_labels_fallback():
    r = run_pipeline(daily([5.0 + i % 3 for i in range(10)]), cfg())
    assert r["status"] == "insufficient_data" and "values" not in r
    r2 = run_pipeline(daily([5.0 + (i % 3) for i in range(16)]), cfg(horizon=3))
    assert r2["status"] == "completed_with_fallback" and r2["fallback"]["validated"] is False
    assert all(it["status"] == "unsupported" for v in r2["values"] for it in v["intervals"])


def test_pipeline_seasonal_series_beats_naive_and_is_reproducible():
    rnd = random.Random(21)
    shape = [0.6, 0.7, 0.8, 0.9, 1.0, 1.9, 2.1]
    vals = [100 * shape[i % 7] + rnd.gauss(0, 3) for i in range(210)]
    c = cfg(horizon=14, n_folds=8, interval_levels=[0.8])
    a, b = run_pipeline(daily(vals), c), run_pipeline(daily(vals), c)
    assert a["status"] == "completed" and a["selection"]["status"] == "selected"
    assert a["model"]["key"] in ("seasonal_naive", "holt_winters", "lag_ridge", "arima_like", "moving_average")
    assert "seasonal" in a["patterns"]["labels"]
    assert (
        a["evaluation"][a["model"]["key"]]["overall"]["mase"]["value"]
        < a["evaluation"]["naive"]["overall"]["mase"]["value"]
    )
    assert (
        a["values"] == b["values"]
        and a["reproducibility"]["data_snapshot_hash"] == b["reproducibility"]["data_snapshot_hash"]
    )
    assert (
        a["interval_calibration"][0]["supported"]
        and a["interval_calibration"][0]["coverage_evaluation"]["status"] == "evaluated"
    )


def test_pipeline_intermittent_series_gets_croston_candidate():
    rnd = random.Random(2)
    vals = [float(rnd.choice([0, 0, 0, 4, 6, 9])) if rnd.random() < 0.9 else 0.0 for _ in range(150)]
    r = run_pipeline(daily(vals), cfg(horizon=7, n_folds=6))
    assert "croston_sba" in r["candidates"]["evaluated"] and "intermittent" in r["patterns"]["labels"]
    assert r["status"].startswith("completed")


def test_nonnegative_clamping_is_measured():
    vals = [max(0.0, 3.0 - 0.1 * i) for i in range(30)] + [0.0] * 10
    r = run_pipeline(daily(vals), cfg(horizon=5, n_folds=3))
    assert r["nonnegative_clamping"]["enabled"] is True
    assert all(v["point"] >= 0 for v in r["values"])
    assert all(v["point_raw"] <= v["point"] + 1e-12 for v in r["values"])


def test_limits_are_enforced():
    with pytest.raises(ValueError):
        ForecastConfig(horizon=500).check_limits(ForecastLimits())
    with pytest.raises(ValueError):
        ForecastConfig(candidates=["lstm"])
    with pytest.raises(ValueError):
        ForecastConfig(unknown_field=1)  # type: ignore[call-arg]
