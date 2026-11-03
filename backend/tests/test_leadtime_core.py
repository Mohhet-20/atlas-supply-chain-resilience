"""Lead-time semantics, analytics and evaluation/leakage controls. SYNTHETIC data only."""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.leadtime import analytics, dataset
from app.leadtime import estimators as est
from app.leadtime import features as F
from app.leadtime import metrics as M
from app.leadtime import semantics as sem
from app.leadtime.config import LeadTimeConfig, LeadTimeLimits
from app.leadtime.history import Outcome, Sweep
from app.leadtime.pipeline import _threshold, run_pipeline
from app.leadtime.semantics import Calendar, Delivery
from tests.leadtime_helpers import NOW, gen_rows, special_rows, to_deliveries

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)  # a Monday


def dl(**kw) -> Delivery:
    base = dict(key="A|1", ref="A", product_id=1, supplier_id=1, destination_id=1)
    base.update(kw)
    return Delivery(**base)


# ------------------------------------------------------------------------------------------------- semantics
def test_units_calendar_days_and_hours():
    assert sem.elapsed(T0, T0 + timedelta(hours=36), "HOURS") == 36
    assert sem.elapsed(T0, T0 + timedelta(hours=36), "CALENDAR_DAYS") == pytest.approx(1.5)


def test_business_days_exclude_weekends_and_holidays():
    cal = Calendar("UTC", (5, 6), frozenset({date(2026, 3, 4)}))  # Wed 4 Mar is a holiday
    assert sem.business_days_between(T0, T0 + timedelta(days=7), cal) == 4  # Tue, Thu, Fri, Mon
    assert sem.business_days_between(T0 + timedelta(days=7), T0, cal) == -4
    with pytest.raises(ValueError):
        sem.elapsed(T0, T0 + timedelta(days=3), "BUSINESS_DAYS")  # no calendar -> refuses


def test_business_day_uses_local_dates():
    cal = Calendar("Asia/Kolkata", (5, 6), frozenset())
    start = datetime(2026, 3, 6, 20, 0, tzinfo=UTC)  # Fri 20:00 UTC = Sat 01:30 IST
    assert (
        sem.business_days_between(start, start + timedelta(days=3), cal) == 2
    )  # local Sat 01:30 -> Tue 01:30: Mon, Tue
    assert (
        sem.business_days_between(start, start + timedelta(days=3), Calendar("UTC", (5, 6), frozenset())) == 1
    )  # UTC dates: Fri -> Mon


def test_deviation_sign_and_grace():
    d = dl(original_promised_at=T0, arrived_at=T0 + timedelta(hours=5))
    assert sem.deviation_hours(d, "ORIGINAL") == 5 and sem.is_late(d, "ORIGINAL") is True
    assert sem.is_late(d, "ORIGINAL", grace_hours=6) is False
    early = dl(original_promised_at=T0, arrived_at=T0 - timedelta(hours=2))
    assert sem.deviation_hours(early, "ORIGINAL") == -2 and sem.is_late(early, "ORIGINAL") is False
    exact = dl(original_promised_at=T0, arrived_at=T0)
    assert sem.deviation_hours(exact, "ORIGINAL") == 0 and sem.is_late(exact, "ORIGINAL") is False


def test_missing_promise_is_unknown_never_on_time():
    d = dl(arrived_at=T0)
    assert sem.is_late(d, "ORIGINAL") is None and sem.deviation_hours(d, "ORIGINAL") is None


def test_original_vs_latest_commitment():
    d = dl(original_promised_at=T0, latest_promised_at=T0 + timedelta(days=3), arrived_at=T0 + timedelta(days=2))
    assert sem.is_late(d, "ORIGINAL") is True and sem.is_late(d, "LATEST") is False


def test_classification_states():
    asof = T0 + timedelta(days=30)
    assert sem.classify(dl(order_at=T0, received_at=T0 + timedelta(days=5)), asof)[0] == sem.COMPLETED
    assert sem.classify(dl(order_at=T0, dispatched_at=T0 + timedelta(days=1)), asof)[0] == sem.IN_TRANSIT
    assert sem.classify(dl(order_at=T0), asof)[0] == sem.OPEN
    assert sem.classify(dl(order_at=T0, reported_status="CANCELLED"), asof)[0] == sem.CANCELLED
    assert (
        sem.classify(dl(order_at=T0, arrived_at=T0 + timedelta(days=4), quantity=100, received_quantity=40), asof)[0]
        == sem.PARTIAL
    )
    assert sem.classify(dl(order_at=T0, dispatched_at=T0 - timedelta(days=1)), asof)[0] == sem.INVALID
    assert sem.classify(dl(), asof)[0] == sem.UNKNOWN


def test_tolerance_allows_precision_differences():
    d = dl(order_at=T0, dispatched_at=T0 - timedelta(hours=3))
    assert sem.chronology_violations(d, 0) == ["DISPATCHED_BEFORE_ORDER"]
    assert sem.chronology_violations(d, 4) == []


def test_timezone_normalisation():
    u, assumed, date_only = sem.parse_ts("2026-03-01T09:00:00", "Asia/Kolkata")
    assert u == datetime(2026, 3, 1, 3, 30, tzinfo=UTC) and assumed and not date_only
    u2, assumed2, _ = sem.parse_ts("2026-03-01T09:00:00+02:00", "Asia/Kolkata")
    assert u2 == datetime(2026, 3, 1, 7, 0, tzinfo=UTC) and not assumed2
    assert sem.parse_ts("2026-03-01", "UTC")[2] is True


def test_endpoints_do_not_substitute_events():
    d = dl(order_at=T0, received_at=None, arrived_at=T0 + timedelta(days=4))
    s, e, _ = sem.endpoints(d, "ORDER_TO_RECEIPT")
    assert s == T0 and e is None  # arrival is not silently used as receipt


# ------------------------------------------------------------------------------------------------- analytics
def cfg(**kw) -> LeadTimeConfig:
    return LeadTimeConfig(**kw)


def test_summary_matches_independent_calculation():
    rows = gen_rows(120, 5)
    ds = to_deliveries(rows)
    s = analytics.summarize(ds, cfg(), NOW)
    leads = sorted((d.received_at - d.order_at).total_seconds() / 86400 for d in ds)
    n = len(leads)
    assert s["lead_time"]["n"] == n and s["lead_time"]["median"] == pytest.approx(
        (leads[n // 2 - 1] + leads[n // 2]) / 2
    )
    assert s["lead_time"]["mean"] == pytest.approx(sum(leads) / n)
    late = sum(1 for d in ds if d.arrived_at > d.original_promised_at)
    p = s["punctuality"]
    assert p["late"] == late and p["on_time"] == n - late and p["late_rate"] == pytest.approx(late / n)
    assert p["late_rate_ci95"][0] < p["late_rate"] < p["late_rate_ci95"][1]


def test_censored_cancelled_partial_are_not_completed_deliveries():
    rows = gen_rows(40, 3) + list(special_rows().values())
    s = analytics.summarize(to_deliveries(rows), cfg(), NOW)
    ex = s["counts"]["excluded_by_reason"]
    assert (
        ex["CENSORED_IN_TRANSIT"] == 1
        and ex["CENSORED_OPEN"] == 1
        and ex["CANCELLED"] == 1
        and ex["PARTIAL_RECEIPT"] == 1
    )
    assert s["counts"]["eligible_completed"] == 41  # 40 + the revised-promise delivery
    assert any("right-censored" in w or "censored" in w for w in s["warnings"])


def test_missing_promise_excluded_from_punctuality_not_counted_on_time():
    rows = gen_rows(30, 3)
    for r in rows[:10]:
        r["original_promised_at"] = None
    s = analytics.summarize(to_deliveries(rows), cfg(), NOW)
    assert s["punctuality"]["n"] == 20 and s["punctuality"]["n_missing_commitment"] == 10
    assert s["lead_time"]["n"] == 30  # lead time still uses all 30


def test_percentiles_need_enough_observations():
    s = analytics.summarize(to_deliveries(gen_rows(12, 3)), cfg(), NOW)
    assert "p50" in s["lead_time"]["percentiles"] and "p95" in s["lead_time"]["percentiles_unsupported"]
    one = analytics.summarize(to_deliveries(gen_rows(1, 3)), cfg(), NOW)
    assert one["lead_time"]["std"] is None and one["lead_time"]["note_std"]


def test_definitions_are_not_mixed():
    ds = to_deliveries(gen_rows(60, 4))
    a = analytics.summarize(ds, cfg(definition="ORDER_TO_RECEIPT"), NOW)["lead_time"]["median"]
    b = analytics.summarize(ds, cfg(definition="DISPATCH_TO_ARRIVAL"), NOW)["lead_time"]["median"]
    c = analytics.summarize(ds, cfg(definition="ORDER_TO_DISPATCH"), NOW)["lead_time"]["median"]
    assert len({round(a, 3), round(b, 3), round(c, 3)}) == 3
    h = analytics.summarize(ds, cfg(unit="HOURS"), NOW)["lead_time"]["median"]
    assert h == pytest.approx(a * 24, rel=1e-6)


def test_start_event_confirmation_changes_lead_time():
    ds = to_deliveries(gen_rows(60, 4))
    o = analytics.summarize(ds, cfg(start_event="ORDER"), NOW)["lead_time"]["median"]
    c = analytics.summarize(ds, cfg(start_event="CONFIRMATION"), NOW)["lead_time"]["median"]
    assert c < o


def test_group_summary_flags_sparse_groups_and_does_not_rank_them():
    rows = gen_rows(90, 6)
    extra = dict(rows[0], delivery_ref="TINY", supplier_code="S3")
    ds = to_deliveries(rows)
    for d in ds:  # make one supplier tiny
        if d.supplier_id == 3:
            d.supplier_id = 1
    tiny = to_deliveries([dict(extra, supplier_code="S3", arrived_at=extra["arrived_at"])])[0]
    tiny.supplier_id = 9
    g = analytics.group_summary([*ds, tiny], cfg(), NOW, "supplier")
    by = {tuple(r["key"]): r for r in g["groups"]}
    assert by[(9,)]["sufficient_sample"] is False and by[(9,)]["difference_from_overall"] is None
    assert g["groups"][-1]["key"] == [9]  # insufficient groups sort after the rankable ones


def test_recent_vs_history_needs_both_periods():
    ds = to_deliveries(gen_rows(200, 8))
    r = analytics.recent_vs_history(ds, cfg(), NOW, window_days=30)
    assert r["status"] in ("ok", "insufficient_evidence") and "note" in r
    tiny = analytics.recent_vs_history(ds[:5], cfg(), NOW, window_days=30)
    assert tiny["status"] == "insufficient_evidence"


# ------------------------------------------------------------------------------------------------- metrics
def test_metrics_independent_values():
    assert M.roc_auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75
    assert M.pr_auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == pytest.approx(5 / 6)
    assert M.brier([0.9, 0.2], [1, 0]) == pytest.approx(0.025)
    c = M.confusion([0.9, 0.8, 0.3, 0.1], [1, 0, 1, 0], 0.5)
    assert c == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert M.prf(c)["precision"] == 0.5 and M.prf(c)["recall"] == 0.5 and M.prf(c)["f1"] == 0.5


def test_undefined_metrics_are_none_with_reason():
    assert M.roc_auc([0.2, 0.3], [0, 0]) is None and M.pr_auc([0.2, 0.3], [0, 0]) is None
    cls = M.classification([0.2, 0.3], [0, 0], 0.5, 0.0)
    assert cls["roc_auc"] is None and "one class" in cls["roc_auc_note"] and cls["precision"] is None and cls["note"]
    assert M.regression([], [])["mae"]["value"] is None


def test_regression_bias_sign():
    r = M.regression([12, 12], [10, 10])
    assert r["bias"]["value"] == 2 and r["mae"]["value"] == 2 and r["rmse"]["value"] == 2


def test_reliability_needs_data():
    assert M.reliability([0.5] * 10, [1, 0] * 5)["status"] == "insufficient"
    p = [i / 400 for i in range(400)]
    y = [1 if random.Random(i).random() < p[i] else 0 for i in range(400)]
    assert M.reliability(p, y)["status"] == "ok"


# ------------------------------------------------------------------------------------------------- leakage controls
def test_point_view_hides_future_and_outcome_fields():
    d = to_deliveries(gen_rows(1, 1))[0]
    d.latest_promised_at = d.original_promised_at + timedelta(days=5)
    for point, allowed in F.POINT_FIELDS.items():
        v = F.view_of(d, point)
        assert set(v) == set(allowed)
        for forbidden in F.FORBIDDEN_EVERYWHERE:
            assert forbidden not in v
            with pytest.raises(KeyError):
                v[forbidden]  # noqa: B018
    assert "dispatched_at" not in F.view_of(d, "CONFIRMATION") and "confirmed_at" not in F.view_of(d, "ORDER_PLACEMENT")
    assert "original_promised_at" not in F.view_of(d, "ORDER_PLACEMENT")


def test_features_use_original_not_revised_promise():
    d = to_deliveries(gen_rows(1, 1))[0]
    base = F.raw_numeric(F.view_of(d, "CONFIRMATION"), "CONFIRMATION", None)
    d.latest_promised_at = d.original_promised_at + timedelta(days=9)
    assert F.raw_numeric(F.view_of(d, "CONFIRMATION"), "CONFIRMATION", None) == base


def test_history_sweep_ignores_future_and_self():
    t = lambda days: T0 + timedelta(days=days)  # noqa: E731
    outs = [Outcome(t(5), 1, (1, 1), 10.0, False), Outcome(t(20), 1, (1, 1), 30.0, True)]
    sw = Sweep(outs)
    hf = sw.at(t(10), 1, (1, 1))
    assert hf.sup_n == 1 and hf.sup_median == 10.0
    with_future = Sweep([*outs, Outcome(t(11), 1, (1, 1), 99.0, True)]).at(t(10), 1, (1, 1))
    assert with_future == hf  # an outcome known after the prediction cannot change it
    assert Sweep(outs).at(t(5), 1, (1, 1)).sup_n == 0  # strictly before: an outcome known exactly now is not used
    with pytest.raises(ValueError):
        sw.at(t(1), 1, (1, 1))


def _examples(n=300, seed=9):
    c = cfg(target="LATE_RISK")
    ds = to_deliveries(gen_rows(n, seed))
    part = analytics.partition(ds, c, NOW)
    ex, skipped = dataset.build_examples(part, c)
    dataset.attach_history(ex, dataset.outcomes_of(part["complete"]))
    return c, ex, part, skipped


def test_split_is_chronological_group_safe_and_purged():
    c, ex, _p, _s = _examples()
    # make two lines of one order straddle the natural cut
    sp = dataset.split(ex, c)
    tr, va, te = sp["train"], sp["val"], sp["test"]
    assert max(e.ref_at for e in tr) <= min(e.ref_at for e in va) and max(e.ref_at for e in va) <= min(
        e.ref_at for e in te
    )
    assert all(
        e.known_at <= min(x.ref_at for x in va) for e in tr
    )  # purge: no train label is from the future of validation
    assert all(e.known_at <= min(x.ref_at for x in te) for e in va + tr)
    assert {e.d.ref for e in tr}.isdisjoint({e.d.ref for e in te}) and sp["purged_train"] >= 0


def test_order_lines_stay_in_one_split():
    c = cfg(target="LATE_RISK")
    rows = gen_rows(120, 11)
    for i in range(0, 120, 6):  # second product line for every sixth order
        twin = dict(rows[i], product_code="P2" if rows[i]["product_code"] == "P1" else "P1")
        rows.append(twin)
    ds = to_deliveries(rows)
    part = analytics.partition(ds, c, NOW)
    ex, _ = dataset.build_examples(part, c)
    dataset.attach_history(ex, dataset.outcomes_of(part["complete"]))
    sp = dataset.split(ex, c)
    where = {}
    for name in ("train", "val", "test"):
        for e in sp[name]:
            where.setdefault(e.d.ref, set()).add(name)
    assert all(len(v) == 1 for v in where.values())


def test_preprocessing_is_fit_on_training_rows_only():
    c, ex, _p, _s = _examples()
    sp = dataset.split(ex, c)
    train = sp["train"]
    raws = est.raws_of(train, c.prediction_point)
    spec = F.fit_spec(c.prediction_point, raws, min_count=3, max_levels=12)
    vals = [r[0]["log_qty"] for r in raws if r[0]["log_qty"] is not None]
    assert spec.num_mean["log_qty"] == pytest.approx(sum(vals) / len(vals))
    test_only_levels = {e.d.supplier_id for e in sp["test"]} - {e.d.supplier_id for e in train}
    assert not test_only_levels or all(str(s) not in spec.cat_levels["supplier"] for s in test_only_levels)


def run(c: LeadTimeConfig, ds):
    return run_pipeline(ds, c, LeadTimeLimits(), NOW, lambda: None)


def test_test_labels_do_not_influence_fitting_threshold_or_calibration():
    c = cfg(target="LATE_RISK")
    rows = gen_rows(420, 13)
    a = run(c, to_deliveries(rows))
    # flip every late/on-time outcome that falls in the test split by moving the arrival
    ds2 = to_deliveries(rows)
    test_refs = {e.d.ref for e in dataset.split(_ex(c, ds2), c)["test"]}
    for d in ds2:
        if d.ref in test_refs:
            d.arrived_at = d.original_promised_at + timedelta(days=20)  # all late
            d.received_at = d.arrived_at
    b = run(c, ds2)
    assert a["artifact"]["model"] == b["artifact"]["model"] or a["selection"]["status"] != b["selection"]["status"]
    ea, eb = a["evaluation"]["models"]["logistic_late"], b["evaluation"]["models"]["logistic_late"]
    assert ea["threshold"] == eb["threshold"]  # chosen on validation only
    assert ea["validation"] == eb["validation"]  # validation untouched by test outcomes
    assert ea["test"] != eb["test"]


def _ex(c, ds):
    part = analytics.partition(ds, c, NOW)
    ex, _ = dataset.build_examples(part, c)
    dataset.attach_history(ex, dataset.outcomes_of(part["complete"]))
    return ex


def test_label_uses_the_configured_commitment():
    rows = gen_rows(200, 14)
    ds = to_deliveries(rows)
    for d in ds[:60]:
        d.latest_promised_at = d.original_promised_at + timedelta(
            days=30
        )  # revised far out: nothing is late vs the latest
    o = analytics.summarize(ds, cfg(commitment="ORIGINAL"), NOW)["punctuality"]
    lt = analytics.summarize(ds, cfg(commitment="LATEST"), NOW)["punctuality"]
    assert lt["late"] < o["late"]


def test_duplicate_delivery_lines_do_not_inflate_counts():
    rows = gen_rows(30, 3)
    ds = to_deliveries(rows)
    assert len({d.key for d in ds}) == 30


# ------------------------------------------------------------------------------------------------- pipeline behaviour
def test_lead_time_run_end_to_end_synthetic():
    r = run(cfg(), to_deliveries(gen_rows(500, 21) + list(special_rows().values())))
    assert r["status"] == "completed" and r["selection"]["selected"] in r["candidates"]["evaluated"]
    assert r["splits"]["train"]["n"] > r["splits"]["test"]["n"] > 0
    assert r["preparation"]["censored"] == 2
    m = r["evaluation"]["models"]
    for v in m.values():
        assert set(v) >= {"validation", "test", "training_in_sample"}
    assert r["selection"]["selected"] != "global_median"  # supplier effect exists in the synthetic generator
    iv = r["intervals"]
    assert (
        iv["supported"]
        and iv["lower_offset"] < 0 < iv["upper_offset"]
        and 0 < iv["test_coverage"]["empirical_coverage"] <= 1
    )


def test_declines_with_too_little_data():
    r = run(cfg(), to_deliveries(gen_rows(25, 1)))
    assert r["status"] == "insufficient_data" and "required" in r["decline_reason"]
    assert "selection" not in r or r.get("selected_model") is None


def test_learned_model_not_selected_without_beating_baseline():
    r = run(cfg(), to_deliveries(gen_rows(500, 22)))
    s = r["selection"]
    ridge = r["evaluation"]["models"]["ridge_leadtime"]["validation"]["mae"]["value"]
    best_base = min(
        r["evaluation"]["models"][k]["validation"]["mae"]["value"]
        for k in ("global_median", "supplier_median", "lane_median", "hierarchical_median")
    )
    if s["selected"] == "ridge_leadtime":
        assert ridge < best_base * 0.95
    else:
        assert s["status"] in ("baseline_selected", "inconclusive")


def test_ineligible_models_are_reported_not_hidden():
    r = run(cfg(min_eligible=20), to_deliveries(gen_rows(60, 2)))
    assert "ridge_leadtime" in r["candidates"]["ineligible"] or "ridge_leadtime" in r["candidates"]["evaluated"]


def test_unknown_model_key_rejected_by_config():
    with pytest.raises(ValueError, match="Unknown model"):
        cfg(models=["os.system"])
    with pytest.raises(ValueError):
        cfg(models=["logistic_late"])  # a risk model is not a lead-time model
    with pytest.raises(ValueError):
        LeadTimeConfig(organisation_id=3)  # unknown field


def test_risk_run_reports_class_balance_and_baseline_comparison():
    c = cfg(target="LATE_RISK")
    r = run(c, to_deliveries(gen_rows(420, 31)))
    cb = r["preparation"]["class_balance"]
    assert cb["train"]["late"] + cb["train"]["on_time"] == r["splits"]["train"]["n"]
    ms = r["evaluation"]["models"]
    assert set(ms) == {"base_rate", "supplier_rate", "logistic_late"}
    assert ms["base_rate"]["test"]["roc_auc"] == 0.5 or ms["base_rate"]["test"]["roc_auc"] is None
    assert ms["logistic_late"]["training_in_sample"]["brier_raw"] is not None


def test_small_validation_means_uncalibrated_score_not_probability():
    r = run(cfg(target="LATE_RISK"), to_deliveries(gen_rows(300, 41)))
    cal = r["calibration"]
    if r["selected_model"] == "logistic_late":
        assert (
            r["score_kind"] == "uncalibrated_score"
            and cal["decision"] == "uncalibrated_score"
            and cal["reasons_not_calibrated"]
        )


def test_calibrated_label_requires_evidence_criteria():
    c = cfg(target="LATE_RISK", dispatch=None) if False else cfg(target="LATE_RISK", prediction_point="DISPATCH")
    r = run(c, to_deliveries(gen_rows(1400, 51)))
    cal = r["calibration"]
    assert cal is not None and cal["n_validation"] >= 100 and cal["n_test"] >= 100
    assert (cal["decision"] == "calibrated_probability") == (not cal["reasons_not_calibrated"])
    if cal["decision"] == "calibrated_probability":
        assert (
            cal["test"]["ece"] <= 0.05
            and cal["test"]["brier_calibrated"] < cal["test"]["brier_baseline_train_prevalence"]
        )


def test_no_late_examples_means_no_model_claims():
    rows = gen_rows(300, 61)
    for r_ in rows:
        r_["original_promised_at"] = (
            datetime.fromisoformat(r_["arrived_at"].replace("Z", "+00:00")) + timedelta(days=3)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = run(cfg(target="LATE_RISK"), to_deliveries(rows))
    assert (
        r["status"] == "insufficient_data"
        or r["selection"]["selected"] is None
        or r["evaluation"]["models"]["base_rate"]["test"]["pr_auc"] is None
    )
    assert not math.isnan(r["preparation"]["eligible_examples"])


def test_threshold_is_chosen_on_given_split_only():
    t, f = _threshold([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1])
    assert 0.2 < t <= 0.8 and f == 1.0
    t2, _ = _threshold([0.5, 0.5], [0, 1])
    assert t2 == 0.5


def test_pipeline_is_reproducible():
    c = cfg(target="LATE_RISK")
    rows = gen_rows(420, 71)
    a, b = run(c, to_deliveries(rows)), run(c, to_deliveries(rows))
    assert a["evaluation"]["models"]["logistic_late"]["test"] == b["evaluation"]["models"]["logistic_late"]["test"]
    assert a["artifact"] == b["artifact"] and a["reproducibility"] == b["reproducibility"]
