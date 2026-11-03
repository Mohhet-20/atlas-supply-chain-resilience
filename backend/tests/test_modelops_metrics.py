"""Metric correctness against hand-computed values, undefined handling, drift and leakage checks. Synthetic data only."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app.modelops import drift as D
from app.modelops import metrics as M
from app.modelops import validity as V

T0 = datetime(2026, 1, 1)


def test_regression_hand_computed():
    r = M.regression([12, 8, 10], [10, 10, 10])
    assert r["mae"]["value"] == pytest.approx(4 / 3)
    assert r["rmse"]["value"] == pytest.approx(math.sqrt(8 / 3))
    assert r["bias"]["value"] == pytest.approx(0.0)
    assert r["wape"]["value"] == pytest.approx(4 / 30)
    assert r["median_ae"]["value"] == 2
    assert r["mase"]["status"] == "undefined"  # no scale supplied: never a silent number
    assert M.regression([12, 8, 10], [10, 10, 10], scale=2.0)["mase"]["value"] == pytest.approx(2 / 3)


def test_regression_edge_cases():
    assert M.regression([], [])["mae"]["status"] == "insufficient_data"
    z = M.regression([1, 2], [0, 0])
    assert z["wape"]["status"] == "undefined" and z["mae"]["value"] == 1.5
    r = M.regression([1, None, float("nan"), 3], [1, 2, 3, 5])
    assert r["counts"] == {"input": 4, "used": 2, "excluded_missing_or_non_finite": 2}
    assert r["mae"]["value"] == 1.0
    with pytest.raises(ValueError):
        M.regression([1], [1, 2])


def test_classification_hand_computed_and_ties():
    s = [0.9, 0.8, 0.3, 0.2]
    y = [1, 0, 1, 0]
    c = M.classification(s, y, 0.5, probabilistic=True)
    assert c["confusion"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert c["roc_auc"]["value"] == pytest.approx(0.75)
    assert c["brier"]["value"] == pytest.approx((0.01 + 0.64 + 0.49 + 0.04) / 4)
    assert c["log_loss"]["value"] == pytest.approx(-(math.log(0.9) + math.log(0.2) + math.log(0.3) + math.log(0.8)) / 4)
    assert M.classification([0.5] * 4, y, 0.5, probabilistic=True)["roc_auc"]["value"] == pytest.approx(0.5)


def test_classification_undefined_cases():
    c = M.classification([0.2, 0.4, 0.6], [0, 0, 0], 0.5, probabilistic=True)
    assert c["roc_auc"]["status"] == "undefined" and c["pr_auc"]["status"] == "undefined" and c["single_class"]
    assert c["per_class"]["positive"]["recall"]["status"] == "undefined"
    never = M.classification([0.1, 0.2, 0.3, 0.4], [1, 0, 1, 0], 0.9, probabilistic=False)
    assert never["per_class"]["positive"]["precision"]["status"] == "undefined"  # nothing predicted positive
    assert never["per_class"]["positive"]["f1"]["status"] == "undefined"
    assert M.classification([], [], 0.5, probabilistic=True)["status"] == "insufficient_data"


def test_scores_are_not_scored_as_probabilities():
    c = M.classification([1.5, -2.0, 3.0, 0.1], [1, 0, 1, 0], 0.5, probabilistic=False)
    assert c["brier"]["status"] == "undefined" and "score" in c["brier"]["note"]
    bad = M.classification([1.5, -2.0, 3.0, 0.1], [1, 0, 1, 0], 0.5, probabilistic=True)
    assert bad["brier"]["status"] == "undefined" and bad["log_loss"]["status"] == "undefined"


def test_log_loss_clips_extremes():
    c = M.classification([1.0, 0.0], [0, 1], 0.5, probabilistic=True)
    assert math.isfinite(c["log_loss"]["value"]) and c["log_loss"]["value"] > 30


def test_reliability_needs_enough_data():
    r = M.reliability([0.5] * 20, [0, 1] * 10, min_n=100)
    assert r["status"] == "insufficient_data" and r["ece"]["status"] == "insufficient_data"
    probs = [i / 199 for i in range(200)]
    perfect = [1 if i % 200 >= 100 else 0 for i in range(200)]
    r2 = M.reliability(probs, perfect, min_n=100)
    assert r2["status"] == "ok" and r2["ece"]["value"] is not None and "equal-frequency" in r2["method"]


def test_interval_coverage():
    c = M.interval_coverage([0, 0, 0, 0], [10, 10, 10, 10], [5, 5, 5, 20], 0.8)
    assert c["coverage"]["value"] == 0.75 and c["inside"] == 3 and c["width"]["value"] == 10
    assert M.interval_coverage([], [], [], 0.8)["status"] == "insufficient_data"


def test_segment_suppression_hides_small_groups():
    keys = ["A"] * 25 + ["B"] * 3
    r = M.segment_metrics(keys, [1.0] * 28, [2.0] * 28, min_n=20)
    assert [s["segment"] for s in r["segments"]] == ["A"]
    assert r["suppressed"] == {"segments": 1, "records": 3, "reason": r["suppressed"]["reason"]}
    assert "B" not in str(r["segments"])


def test_psi_detects_shift_and_handles_small_samples():
    ref = [float(i % 10) for i in range(200)]
    same = D.psi_numeric(ref, ref)
    shifted = D.psi_numeric(ref, [x + 6 for x in ref])
    assert same["status"] == "ok" and same["value"] == pytest.approx(0, abs=1e-9)
    assert shifted["status"] == "alert" and shifted["value"] > 0.25
    assert D.psi_numeric(ref[:5], ref)["status"] == "insufficient_data"
    assert D.psi_numeric([3.0] * 50, [3.0] * 50)["status"] == "ok"


def test_categorical_drift_flags_unseen_categories():
    r = D.psi_categorical(["a", "b"] * 30, ["a", "b"] * 15 + ["z"] * 2 + ["a"] * 28)
    assert "z" in r["unseen_categories"] and r["status"] in ("warning", "alert")


def test_missingness_and_ks():
    assert D.missingness_change(0, 100, 30, 100)["status"] == "alert"
    assert D.missingness_change(1, 10, 5, 10)["status"] == "insufficient_data"
    a = [float(i) for i in range(100)]
    assert D.ks_statistic(a, a)["value"] == 0.0
    assert D.ks_statistic(a, [x + 50 for x in a])["status"] == "warning"


# ----------------------------------------------------------------------------------------------------- leakage checks
def test_contaminated_temporal_split_is_detected():
    train = [T0 + timedelta(days=i) for i in range(10)]
    clean = [T0 + timedelta(days=20 + i) for i in range(5)]
    leaky = [T0 + timedelta(days=5 + i) for i in range(5)]  # overlaps the training period
    assert V.temporal_order(train, clean)["status"] == "pass"
    bad = V.temporal_order(train, leaky)
    assert bad["status"] == "fail" and bad["overlap"] > 0
    assert V.temporal_order([], clean)["status"] == "not_checked"


def test_grouped_entity_leakage_is_detected():
    assert V.group_overlap(["s1", "s2"], ["s3"], "supplier")["status"] == "pass"
    bad = V.group_overlap(["s1", "s2"], ["s2", "s3"], "supplier")
    assert bad["status"] == "fail" and bad["overlap"] == 1


def test_duplicate_rows_across_splits_detected():
    assert V.duplicate_overlap([1, 2, 3], [3, 4])["status"] == "fail"
    assert V.duplicate_overlap([1, 2], [3, 4])["status"] == "pass"


def test_target_in_features_by_name_and_value():
    y = [float(i) for i in range(30)]
    assert V.target_in_features(["age", "delay_days"], "delay_days")["status"] == "fail"
    leaked = V.target_in_features(
        ["a", "b"], "y", {"a": [v * 2 + 1 for v in y], "b": [(i * 7) % 5 for i in range(30)]}, y
    )
    assert leaked["status"] == "fail" and leaked["features"] == ["a"]
    assert V.target_in_features(["a"], "y", {"a": [(i * 7) % 5 for i in range(30)]}, y)["status"] == "pass"


def test_fit_boundaries_reject_final_holdout_fitting():
    ok = V.fit_boundary({"scaler": "train", "threshold": "validation", "calibration": "validation"})
    assert ok["status"] == "pass"
    bad = V.fit_boundary({"scaler": "train", "threshold": "test", "calibration": "full"})
    assert bad["status"] == "fail" and bad["steps"] == ["calibration", "threshold"]
    assert V.fit_boundary(None)["status"] == "not_checked"


def test_window_order_and_overall():
    a, b, c = T0, T0 + timedelta(days=10), T0 + timedelta(days=20)
    assert V.window_order(a, b, c)["status"] == "pass"
    assert V.window_order(c, b, a)["status"] == "fail"
    assert V.overall([{"status": "pass"}, {"status": "fail"}]) == "invalid"
    assert V.overall([{"status": "pass"}, {"status": "not_checked"}]) == "valid_with_unchecked"
    assert V.overall([{"status": "pass"}]) == "valid"
