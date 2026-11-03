"""Read persisted domain evidence and re-express it in the common evaluation schema. Nothing here fits or changes a model."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.forecast.config import BASELINE_KEYS
from app.leadtime.assess import outcome_of
from app.modelops import config as C
from app.modelops import metrics as M
from app.modelops import validity as V
from app.modelops.registry import checksum
from app.models.delivery import DeliveryAssessment, LeadTimeRun
from app.models.forecast import ForecastEvaluation, ForecastRun
from app.models.modelops import ModelVersion
from app.models.types import utcnow


def _val(d: Any, *path: str) -> Any:
    for p in path:
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d


def _mv(d: dict[str, Any] | None) -> dict[str, Any]:
    """Re-wrap a stored ``{value, n, note}`` metric in the common status schema."""
    if not isinstance(d, dict) or "value" not in d:
        return M.m(None, 0, "not stored by the training run")
    return M.m(d.get("value"), d.get("n") or 0, d.get("note"))


def result(status: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"result_status": status, "dataset_ref": None, "split": None, "sample": None, "metrics": None, "baselines": None, "folds": None, "segments": None, "calibration": None, "intervals": None, "checks": [], "warnings": [], "limitations": []}  # fmt: skip
    base.update(kw)
    return base


# ------------------------------------------------------------------------------------------------ recorded backtests
def forecast_backtest(session: Session, mv: ModelVersion, cfg: C.EvalConfig) -> dict[str, Any]:
    r = session.get(ForecastRun, mv.source_id)
    ev = r.evaluation or {}
    sel = r.selected_model
    if r is None or sel not in ev:
        return result("insufficient_data", warnings=["The run stored no backtest evidence for the selected model."])
    one = ev[sel]
    keys = ("mae", "rmse", "wape", "mase", "bias", "smape", "mape")
    metrics = {k: _mv(one["overall"].get(k)) for k in keys if k in one["overall"]}
    base = {k: {"metrics": {m: _mv(v["overall"].get(m)) for m in ("mae", "rmse", "mase", "bias") if m in v["overall"]}, "complete": v.get("complete"), "same_folds_as_model": True} for k, v in ev.items() if k in BASELINE_KEYS and k != sel}  # fmt: skip
    lowest = None
    for k, b in base.items():
        a = b["metrics"].get("mae", {}).get("value")
        if a is not None and (lowest is None or a < lowest[1]):
            lowest = (k, a)
    mine = metrics.get("mae", {}).get("value")
    verdict = None
    if lowest and mine is not None:
        verdict = {"best_baseline": lowest[0], "baseline_mae": lowest[1], "model_mae": mine, "relative_improvement": (lowest[1] - mine) / lowest[1] if lowest[1] else None, "beats_best_baseline": mine < lowest[1] if sel not in BASELINE_KEYS else None}  # fmt: skip
    folds = [{"fold": f, **{m: _mv(v.get(m)) for m in ("mae", "rmse", "mase", "bias") if m in v}} for f, v in sorted(one.get("per_fold", {}).items(), key=lambda kv: int(kv[0]))]  # fmt: skip
    horizons = [{"segment": f"h{h}", **{m: _mv(v.get(m)) for m in ("mae", "bias") if m in v}} for h, v in sorted(one.get("per_horizon", {}).items(), key=lambda kv: int(kv[0]))]  # fmt: skip
    vd = r.validation or {}
    lens = vd.get("fold_train_lengths") or []
    checks = [
        V._c(
            "fold_order",
            "pass" if lens == sorted(lens) and len(set(lens)) == len(lens) else "fail",
            "expanding window: each fold trains on a strictly longer, earlier history than the one before"
            if lens
            else "no fold information",
        ),  # fmt: skip
        V.fit_boundary(
            {"model selection": "backtest folds", "per-fold fitting": "training window of that fold"}
            if vd.get("leakage_controls")
            else None
        ),
        V._c(
            "declared_controls",
            "pass" if vd.get("leakage_controls") else "not_checked",
            "controls declared by the training run (not independently re-executed): "
            + "; ".join(vd.get("leakage_controls") or [])
            if vd.get("leakage_controls")
            else "the run declares no leakage controls",
        ),  # fmt: skip
    ]
    ic = r.interval_calibration
    if isinstance(ic, dict):
        intervals = {
            "source": "training run",
            "calibration": ic.get("method") or ic.get("status"),
            "coverage_evaluation": ic.get("coverage_evaluation"),
        }
    elif isinstance(ic, list) and ic:
        intervals = {"source": "training run", "levels": ic}
    else:
        intervals = None
    warn = list(r.warnings or [])
    if not isinstance(folds, list) or len(folds) < 3:
        warn.append(f"Only {len(folds)} backtest fold(s): a single window of history says little about stability.")
    return result(
        "completed", metrics=metrics, baselines={"entries": base, "verdict": verdict, "note": "Baselines were scored on the same rolling-origin folds as the model."}, folds=folds,
        segments={"horizon": horizons, "min_segment_n": cfg.min_segment_n, "note": "Per-horizon results aggregate every fold; no individual records are exposed."}, intervals=intervals,
        split={"scheme": vd.get("scheme"), "folds": len(folds), "fold_train_lengths": lens, "evaluation_horizon": vd.get("evaluation_horizon"), "min_train_periods": vd.get("min_train_periods")},  # fmt: skip
        sample={"folds": len(folds), "series_periods": r.preparation and r.preparation.get("n_periods")}, dataset_ref={"forecast_run": r.run_uid, "data_snapshot_hash": r.data_snapshot_hash},
        checks=checks, warnings=warn, limitations=["Evidence comes from this series' own past; it does not show performance on other products or facilities.", C.STATEMENTS["recorded"]],  # fmt: skip
    )  # fmt: skip


def leadtime_backtest(session: Session, mv: ModelVersion, cfg: C.EvalConfig) -> dict[str, Any]:
    r = session.get(LeadTimeRun, mv.source_id)
    models = _val(r.evaluation, "models") or {}
    sel = r.selected_model
    if sel not in models:
        return result("insufficient_data", warnings=["The run stored no evaluation for the selected model."])
    risk = r.target == "LATE_RISK"
    e = models[sel]
    test, val = e.get("test") or {}, e.get("validation") or {}
    sp = r.splits or {}
    from datetime import datetime

    def _t(x: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(x) if x else None
        except ValueError:
            return None

    checks = [
        V.window_order(
            _t(_val(sp, "train", "last_reference")),
            _t(_val(sp, "validation", "last_reference")),
            _t(_val(sp, "test", "first_reference")),
        ),
        V.fit_boundary(
            {
                "model choice": "validation",
                "threshold": "validation",
                "calibration map": "validation",
                "scaling and encodings": "train",
            }
            if r.leakage_controls
            else None
        ),
        V._c(
            "declared_controls",
            "pass" if r.leakage_controls else "not_checked",
            ("controls declared by the training run: " + "; ".join(r.leakage_controls))
            if r.leakage_controls
            else "none declared",
        ),  # fmt: skip
    ]
    prev = _val(r.artifact, "train_prevalence")
    base = {}
    for k, v in models.items():
        if k == sel:
            continue
        t = v.get("test") or {}
        base[k] = {"metrics": ({m: _mv(t.get(m)) for m in ("mae", "rmse", "bias")} if not risk else {"pr_auc": M.m(t.get("pr_auc"), t.get("n") or 0), "roc_auc": M.m(t.get("roc_auc"), t.get("n") or 0), "brier": M.m(t.get("brier_raw"), t.get("n") or 0)}), "same_test_split": True}  # fmt: skip
    if risk:
        n = test.get("n") or 0
        score_kind = _val(r.artifact, "score_kind")
        probabilistic = score_kind == "calibrated_probability"
        metrics = {
            "pr_auc": M.m(test.get("pr_auc"), n, "no-skill level equals prevalence"), "roc_auc": M.m(test.get("roc_auc"), n, test.get("roc_auc_note")),
            "precision": M.m(test.get("precision"), n), "recall": M.m(test.get("recall"), n), "f1": M.m(test.get("f1"), n),
            "brier": M.m(test.get("brier_raw") if probabilistic else None, n, None if probabilistic else "not reported: output is a score, not a probability"),
            "prevalence": M.m(test.get("prevalence"), n),
        }  # fmt: skip
        if probabilistic and test.get("brier_baseline_train_prevalence") is not None:
            metrics["brier_baseline_constant_prevalence"] = M.m(test["brier_baseline_train_prevalence"], n, f"constant prediction = training prevalence {prev:.4f}" if prev is not None else None)  # fmt: skip
        base["constant_prevalence"] = {"metrics": {"brier": M.m(test.get("brier_baseline_train_prevalence"), n)}, "same_test_split": True} if test.get("brier_baseline_train_prevalence") is not None else {"note": "not available"}  # fmt: skip
        cal = r.calibration or {}
        tcal = cal.get("test") or {}
        calibration = {"decision": cal.get("decision"), "score_kind": score_kind, "reasons_not_calibrated": cal.get("reasons_not_calibrated"), "fitted_on": cal.get("fitted_on"), "reliability_test": tcal.get("reliability"), "ece": M.m(tcal.get("ece"), n, "equal-frequency bins on the held-out test split"), "brier_calibrated": tcal.get("brier_calibrated"), "brier_baseline_constant_prevalence": tcal.get("brier_baseline_train_prevalence"), "threshold": e.get("threshold"), "statement": C.STATEMENTS["score"]}  # fmt: skip
        intervals = None
    else:
        n = test.get("n") or 0
        metrics = {k: _mv(test.get(k)) for k in ("mae", "rmse", "median_ae", "bias", "relative_bias") if k in test}
        calibration = None
        iv = r.intervals or {}
        intervals = {
            "level": iv.get("level"),
            "supported": iv.get("supported"),
            "method": iv.get("method"),
            "test_coverage": iv.get("test_coverage"),
            "note": iv.get("note"),
        }
    return result(
        "completed", metrics=metrics, baselines={"entries": base, "note": "Candidates and baselines were scored on the same chronological test split."},
        folds=[{"fold": "validation", "n": val.get("n")}, {"fold": "test", "n": test.get("n")}], segments={"by_lead_time_range": e.get("test_by_lead_time_range"), "min_segment_n": cfg.min_segment_n} if not risk else None,
        calibration=calibration, intervals=intervals, split=sp, sample={"validation": val.get("n"), "test": test.get("n"), "positives_test": test.get("positives")},
        dataset_ref={"leadtime_run": r.run_uid, "data_snapshot_hash": r.data_snapshot_hash}, checks=checks, warnings=list(r.warnings or []),
        limitations=[C.STATEMENTS["recorded"], "One chronological test split: results are specific to this period and scope."],
    )  # fmt: skip


def unsupported_rule_based(mv: ModelVersion) -> dict[str, Any]:
    return result(
        "unsupported", warnings=["Predictive evaluation is not available for the rule-based supplier risk indicator: ATLAS holds no outcome labels (for example confirmed disruptions) to compare scores with."],  # fmt: skip
        limitations=["Its weights are expert-set; use the explanation and the evidence-coverage fields to judge it, not accuracy metrics.", "Recording labelled disruption outcomes would allow a PROSPECTIVE evaluation in a later version."],
        checks=[V._c("labels_available", "not_checked", "no outcome labels exist")],
    )  # fmt: skip


# ------------------------------------------------------------------------------------------------ prospective
def forecast_prospective(session: Session, mv: ModelVersion, cfg: C.EvalConfig) -> dict[str, Any]:
    r = session.get(ForecastRun, mv.source_id)
    ev = session.scalar(
        select(ForecastEvaluation)
        .where(ForecastEvaluation.run_id == r.id)
        .order_by(ForecastEvaluation.id.desc())
        .limit(1)
    )
    if ev is None or not ev.matches:
        return result(
            "insufficient_data",
            warnings=[
                "No forecast-versus-actual comparison has been recorded for this run yet. Evaluate the forecast against actuals first."
            ],
        )
    fin = [m for m in ev.matches if m["status"] == "final" and m.get("actual") is not None]
    if not fin:
        return result(
            "insufficient_data",
            warnings=["No forecast period has a final actual yet; preliminary actuals are not scored."],
            sample={"matched": ev.n_matched, "final": ev.n_final},
        )
    pred, act = [m["forecast"] for m in fin], [m["actual"] for m in fin]
    scale = (r.validation or {}).get("naive_scale")
    reg = M.regression(pred, act, scale=scale)
    lo = [
        next(
            (
                i["lower"]
                for i in m["intervals"]
                if i.get("lower") is not None and abs(i["level"] - cfg.interval_level) < 0.051
            ),
            None,
        )
        for m in fin
    ]
    hi = [
        next(
            (
                i["upper"]
                for i in m["intervals"]
                if i.get("lower") is not None and abs(i["level"] - cfg.interval_level) < 0.051
            ),
            None,
        )
        for m in fin
    ]
    cov_idx = [i for i in range(len(fin)) if lo[i] is not None]
    cov = M.interval_coverage(
        [lo[i] for i in cov_idx], [hi[i] for i in cov_idx], [act[i] for i in cov_idx], cfg.interval_level
    )
    seg = M.segment_metrics([f"h{m['step']}" for m in fin], pred, act, min_n=cfg.min_segment_n)
    metrics = {k: v for k, v in reg.items() if k != "counts"}
    warn = []
    if len(fin) < cfg.min_segment_n:
        warn.append(
            f"Only {len(fin)} final forecast period(s) have actuals; the figures are indicative, not conclusive."
        )
    return result(
        "completed", metrics=metrics, baselines={"entries": {}, "note": "not available: no baseline forecast for these periods was issued before the outcomes were known, so none can be compared fairly."},
        segments=seg, intervals=cov, sample={"final_periods": len(fin), **reg["counts"]}, dataset_ref={"forecast_run": r.run_uid, "forecast_evaluation_actuals_hash": ev.actuals_hash},
        split={"scheme": "prospective: forecasts issued before the outcomes were known"}, checks=[V._c("issued_before_outcome", "pass", "forecast values are stored with the run, which is immutable once finished")],  # fmt: skip
        warnings=warn, limitations=[C.STATEMENTS["prospective"]],
    )  # fmt: skip


def leadtime_prospective(session: Session, mv: ModelVersion, cfg: C.EvalConfig) -> dict[str, Any]:
    r = session.get(LeadTimeRun, mv.source_id)
    risk = r.target == "LATE_RISK"
    col = DeliveryAssessment.risk_run_id if risk else DeliveryAssessment.leadtime_run_id
    rows = list(session.scalars(select(DeliveryAssessment).where(col == r.id, DeliveryAssessment.subject == "DELIVERY", DeliveryAssessment.status == "ASSESSED").order_by(DeliveryAssessment.id).limit(cfg.max_records)))  # fmt: skip
    now = utcnow()
    pred, act, score, y, keys = [], [], [], [], {"supplier": [], "destination": [], "product": []}
    pending = 0
    lo, hi = [], []
    for a in rows:
        o = outcome_of(session, a, now)
        if o.get("status") != "observed":
            pending += 1
            continue
        if risk and "risk" in o:
            score.append(o["risk"]["score"])
            y.append(1 if o["risk"]["actual_late"] else 0)
        elif not risk and "lead_time" in o:
            lt = o["lead_time"]
            pred.append(lt["estimate"])
            act.append(lt["actual"])
            iv = (a.estimate or {}).get("interval") or {}
            lo.append(iv.get("lower"))
            hi.append(iv.get("upper"))
        else:
            continue
        keys["supplier"].append(a.supplier_id)
        keys["destination"].append(a.destination_facility_id)
        keys["product"].append(a.product_id)
    n = len(y) if risk else len(act)
    if n == 0:
        return result("insufficient_data", warnings=[f"No issued assessment has a known outcome yet ({len(rows)} issued, {pending} still pending or unmatched)."], sample={"issued": len(rows), "pending": pending})  # fmt: skip
    wanted = [f for f in cfg.segment_by if f in keys]
    unsupported = [f for f in cfg.segment_by if f not in keys]
    prevalence = _val(r.artifact, "train_prevalence")
    if risk:
        probabilistic = _val(r.artifact, "score_kind") == "calibrated_probability"
        thr = _val(r.artifact, "threshold") or 0.5
        cl = M.classification(score, y, thr, probabilistic=probabilistic, train_prevalence=prevalence)
        metrics = {
            k: cl.get(k)
            for k in (
                "roc_auc",
                "pr_auc",
                "brier",
                "log_loss",
                "accuracy",
                "prevalence",
                "per_class",
                "confusion",
                "majority_class_baseline",
            )
        }
        metrics["prevalence"] = M.m(
            cl.get("prevalence"),
            cl.get("n", 0),
            "share of late deliveries among matured outcomes; the no-skill level for PR-AUC",
        )
        base = {"entries": {"constant_prevalence": {"metrics": {"brier": cl.get("brier_baseline_constant_prevalence")}}} if cl.get("brier_baseline_constant_prevalence") else {}, "note": "A constant-prevalence prediction is the only baseline comparable on these records." if cl.get("brier_baseline_constant_prevalence") else "not available: no comparable baseline for a score that is not a probability."}  # fmt: skip
        calib = M.reliability(score, y, bins=cfg.ece_bins, min_n=cfg.min_calibration_n) if probabilistic else {"status": "not_applicable", "note": "The output is a score, not a calibrated probability; reliability and ECE do not apply.", "statement": C.STATEMENTS["score"]}  # fmt: skip
        seg = {f: _seg_cls(keys[f], score, y, thr, cfg.min_segment_n) for f in wanted}
        intervals = None
    else:
        reg = M.regression(pred, act)
        metrics = {k: v for k, v in reg.items() if k != "counts"}
        base = {"entries": {}, "note": "not available: no baseline estimate was issued alongside these assessments."}
        calib = None
        idx = [i for i in range(len(act)) if lo[i] is not None and hi[i] is not None]
        intervals = M.interval_coverage(
            [lo[i] for i in idx], [hi[i] for i in idx], [act[i] for i in idx], _val(r.intervals, "level")
        )
        seg = {f: M.segment_metrics(keys[f], pred, act, min_n=cfg.min_segment_n) for f in wanted}
    if unsupported:
        seg["unsupported_fields"] = {"fields": unsupported, "reason": "not recorded on issued assessments"}
    warn = [f"{pending} issued assessment(s) have no outcome yet and are excluded."] if pending else []
    if n < cfg.min_segment_n:
        warn.append(f"Only {n} matured outcome(s): treat every figure as indicative.")
    return result(
        "completed", metrics=metrics, baselines=base, segments=seg or None, calibration=calib, intervals=intervals,
        sample={"issued": len(rows), "with_outcome": n, "pending": pending}, dataset_ref={"leadtime_run": r.run_uid, "assessments": len(rows), "as_of": now.isoformat()},
        split={"scheme": "prospective: assessments issued before the outcome was known"}, checks=[V._c("issued_before_outcome", "pass", "assessments are insert-only and stamped before their outcomes")],
        warnings=warn, limitations=[C.STATEMENTS["prospective"], "Only deliveries that have completed are compared; late deliveries mature later than on-time ones, which can bias early results."],
    )  # fmt: skip


def _seg_cls(keys: list[Any], score: list[float], y: list[int], thr: float, min_n: int) -> dict[str, Any]:
    groups: dict[Any, list[int]] = {}
    for i, k in enumerate(keys):
        groups.setdefault(k, []).append(i)
    shown, sup_g, sup_n = [], 0, 0
    for k, idx in sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0]))):
        if len(idx) < min_n:
            sup_g, sup_n = sup_g + 1, sup_n + len(idx)
            continue
        c = M.classification([score[i] for i in idx], [y[i] for i in idx], thr, probabilistic=False)
        shown.append({"segment": k, "n": len(idx), "prevalence": c.get("prevalence"), "recall": c["per_class"]["positive"]["recall"], "precision": c["per_class"]["positive"]["precision"], "roc_auc": c["roc_auc"]})  # fmt: skip
    return {
        "min_segment_n": min_n,
        "segments": shown,
        "suppressed": {"segments": sup_g, "records": sup_n, "reason": f"fewer than {min_n} records"},
    }


def dispatch(session: Session, mv: ModelVersion, cfg: C.EvalConfig) -> dict[str, Any]:
    if mv.source_type == "RISK_METHODOLOGY":
        return unsupported_rule_based(mv)
    if cfg.kind == "RECORDED_BACKTEST":
        return (forecast_backtest if mv.source_type == "FORECAST_RUN" else leadtime_backtest)(session, mv, cfg)
    return (forecast_prospective if mv.source_type == "FORECAST_RUN" else leadtime_prospective)(session, mv, cfg)


def input_hash(res: dict[str, Any]) -> str:
    return checksum({k: res.get(k) for k in ("metrics", "baselines", "sample", "dataset_ref")})
