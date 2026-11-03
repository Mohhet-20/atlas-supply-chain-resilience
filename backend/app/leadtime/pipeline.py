"""Training and chronological evaluation for lead-time estimates and late-delivery risk.

One run = one target and one prediction point. Fit on the training split; choose models and thresholds on the validation
split; report the held-out test split once. A learned model is selected only if it beats the best baseline by
``min_improvement`` on validation AND is not worse than that baseline on the untouched test split; otherwise the baseline
is used and the status says so. Probabilities are claimed only when held-out calibration evidence meets documented criteria.
"""

from __future__ import annotations

import random
import sys
import time
from collections.abc import Callable
from typing import Any

from app.forecast.intervals import offsets, required_n
from app.forecast.stats import mean
from app.leadtime import analytics, dataset
from app.leadtime import estimators as est
from app.leadtime import features as F
from app.leadtime import metrics as M
from app.leadtime.config import (
    ALGORITHM_VERSION,
    LEADTIME_MODELS,
    RISK_MODELS,
    STATEMENTS,
    LeadTimeConfig,
    LeadTimeLimits,
)
from app.leadtime.fitters import apply_platt, platt
from app.leadtime.resolve import data_hash
from app.leadtime.semantics import Delivery

CAL = {"min_validation": 100, "min_class_validation": 20, "min_test": 100, "min_class_test": 20, "max_ece": 0.05}
LEAKAGE = [
    "Features are read from a point-in-time view that contains only fields that exist at the prediction point; arrival, receipt, cancellation, the latest (possibly revised) promise and later events are absent from the view.",  # fmt: skip
    "Supplier and lane history at a prediction uses only deliveries whose outcome was known strictly before that prediction's reference time (chronological sweep); a delivery never contributes to its own features.",  # fmt: skip
    "Levels, scaling and imputation are fitted on the training split only.",
    "Splits are chronological on prediction-point time; all lines of one order stay in the same split, so duplicates cannot straddle splits.",  # fmt: skip
    "Training examples (and validation examples) whose outcome was unknown when the next split began are purged.",
    "Model choice and the F1 threshold are selected on the validation split; calibration is fitted on the validation split; the test split is evaluated once and never used for fitting.",  # fmt: skip
    "The label uses the configured commitment (original or latest promise); the prediction features never include the latest promise.",
]


def _threshold(score: list[float], y: list[int]) -> tuple[float, float | None]:
    uniq = sorted(set(score))
    if len(uniq) == 1:
        c = M.prf(M.confusion(score, y, uniq[0]))
        return uniq[0], c["f1"]
    step = max(1, len(uniq) // 200)
    best, best_f = uniq[0], -1.0
    for t in uniq[::step]:
        f = M.prf(M.confusion(score, y, t))["f1"]
        if f is not None and f >= best_f:
            best, best_f = t, f
    return best, (best_f if best_f >= 0 else None)


def _select(c: dict[str, dict[str, Any]], higher_better: bool, min_impr: float) -> dict[str, Any]:
    """c[key] = {kind, val, test, complexity}. Baselines: simplest within 1% of the best. Learned must clear min_impr on validation
    and not be worse than the best baseline on the held-out test split."""
    vs = {k: v["val"] for k, v in c.items() if v["val"] is not None}
    base = {k: v for k, v in vs.items() if c[k]["kind"] == "baseline"}
    if not base:
        return {"status": "no_baseline_available", "selected": None, "rationale": "No baseline could be evaluated."}
    sign = 1 if higher_better else -1
    best_b = max(base, key=lambda k: sign * base[k])
    tol = 0.01 * abs(base[best_b])
    near = [k for k in base if sign * (base[best_b] - base[k]) <= tol]
    simple_b = min(near, key=lambda k: (c[k]["complexity"], k))
    learned = {k: v for k, v in vs.items() if c[k]["kind"] == "learned"}
    ranking = [
        {"model": k, "validation": vs[k], "test": c[k]["test"], "kind": c[k]["kind"]}
        for k in sorted(vs, key=lambda k: -sign * vs[k])
    ]
    if learned:
        best_l = max(learned, key=lambda k: sign * learned[k])
        ref = base[best_b]
        clears = sign * (learned[best_l] - ref) > min_impr * abs(ref) if ref else sign * (learned[best_l] - ref) > 0
        if clears:
            tl, tb = c[best_l]["test"], c[best_b]["test"]
            if tl is not None and tb is not None and sign * (tl - tb) >= 0:
                return {"status": "selected", "selected": best_l, "ranking": ranking, "rationale": f"{best_l} beat the best baseline ({best_b}) on validation by more than {min_impr:.0%} and was not worse on the untouched test split."}  # fmt: skip
            return {"status": "inconclusive", "selected": simple_b, "ranking": ranking, "rationale": f"{best_l} beat the baselines on validation but did not confirm on the held-out test split, so the baseline {simple_b} is used. Evidence is inconclusive."}  # fmt: skip
        return {"status": "baseline_selected", "selected": simple_b, "ranking": ranking, "rationale": f"No learned model beat the best baseline by more than {min_impr:.0%} on validation; the simplest baseline within 1% of the best ({simple_b}) is used."}  # fmt: skip
    return {"status": "baseline_selected", "selected": simple_b, "ranking": ranking, "rationale": f"Only baselines were eligible; the simplest within 1% of the best ({simple_b}) is used."}  # fmt: skip


def _perm_importance(
    predict_loss: Callable[[list[tuple]], float], raws: list[tuple], groups: list[str], seed: int, repeats: int = 3
) -> list[dict[str, Any]]:  # noqa: ANN401
    rng = random.Random(seed)
    base = predict_loss(raws)
    out = []
    for g in groups:
        incs = []
        for _ in range(repeats):
            idx = list(range(len(raws)))
            rng.shuffle(idx)
            shuffled = []
            for i, (num, cat) in enumerate(raws):
                n2, c2 = dict(num), dict(cat)
                src_n, src_c = raws[idx[i]]
                if g in n2:
                    n2[g] = src_n.get(g)
                if g in c2:
                    c2[g] = src_c[g]
                shuffled.append((n2, c2))
            incs.append(predict_loss(shuffled) - base)
        out.append({"feature": g, "loss_increase": mean(incs)})
    return sorted(out, key=lambda r: -(r["loss_increase"] or 0))


def _top_coefs(sp: F.Spec, beta: list[float], k: int = 15) -> list[dict[str, Any]]:
    rows = sorted(((c, b) for c, b in zip(sp.columns, beta, strict=True) if c != "intercept"), key=lambda r: -abs(r[1]))
    return [{"column": c, "coefficient": b} for c, b in rows[:k]]


def run_pipeline(
    deliveries: list[Delivery], cfg: LeadTimeConfig, limits: LeadTimeLimits, as_of, check: Callable[[], None]
) -> dict[str, Any]:  # noqa: ANN001, C901
    t0 = time.monotonic()
    timings: dict[str, float] = {}
    part = analytics.partition(deliveries, cfg, as_of)
    examples, skipped = dataset.build_examples(part, cfg)
    res: dict[str, Any] = {
        "status": "completed", "algorithm_version": ALGORITHM_VERSION, "statements": STATEMENTS,
        "preparation": {"deliveries_considered": len(deliveries), "eligible_completed": len(part["complete"]), "eligible_examples": len(examples), "excluded_by_reason": part["excluded"], "skipped_for_target": skipped, "data_hash": data_hash(deliveries), "min_eligible": cfg.min_eligible, "censored": part["excluded"].get("CENSORED_IN_TRANSIT", 0) + part["excluded"].get("CENSORED_OPEN", 0)},  # fmt: skip
        "leakage_controls": LEAKAGE, "warnings": [], "candidates": {"evaluated": [], "ineligible": {}, "failed": {}},
        "reproducibility": {"seed": cfg.seed, "config_hash": cfg.digest(), "algorithm_version": ALGORITHM_VERSION, "python": sys.version.split()[0]},  # fmt: skip
    }  # fmt: skip
    if res["preparation"]["censored"]:
        res["warnings"].append(f"{res['preparation']['censored']} in-transit/open orders are right-censored: excluded from training and evaluation, which biases observed lead times toward faster deliveries.")  # fmt: skip
    if len(examples) < cfg.min_eligible:
        res["status"] = "insufficient_data"
        res["decline_reason"] = (
            f"{len(examples)} eligible examples; at least {cfg.min_eligible} are required for chronological train/validation/test evaluation."
        )
        return _finish(res, timings, t0)
    t = time.monotonic()
    dataset.attach_history(examples, dataset.outcomes_of(part["complete"]))
    sp = dataset.split(examples, cfg)
    res["splits"] = dataset.split_report(sp)
    train, val, test = sp["train"], sp["val"], sp["test"]
    if len(train) < 20 or len(val) < 9 or len(test) < 8:
        res["status"] = "insufficient_data"
        res["decline_reason"] = f"After the chronological split and purging there are {len(train)} training, {len(val)} validation and {len(test)} test examples (minimum 20 / 9 / 8)."  # fmt: skip
        return _finish(res, timings, t0)
    timings["prepare"] = time.monotonic() - t
    t = time.monotonic()
    body = (
        _lead(train, val, test, cfg, limits, check, res)
        if cfg.target == "LEAD_TIME"
        else _risk(train, val, test, cfg, limits, check, res)
    )
    res.update(body)
    timings["fit_and_evaluate"] = time.monotonic() - t
    return _finish(res, timings, t0)


def _finish(res: dict[str, Any], timings: dict[str, float], t0: float) -> dict[str, Any]:
    timings["total"] = time.monotonic() - t0
    res["timings"] = {k: round(v, 4) for k, v in timings.items()}
    return res


def _lead(train, val, test, cfg, limits, check, res) -> dict[str, Any]:  # noqa: ANN001, C901
    keys = cfg.models or list(LEADTIME_MODELS)
    base = est.lead_baseline_from(train, cfg.min_group_n, cfg.recency_half_life_days)
    cand: dict[str, dict[str, Any]] = {}
    ev: dict[str, Any] = {}
    fitted: dict[str, Any] = {}
    yv, yt = [e.y for e in val], [e.y for e in test]
    for key in keys:
        check()
        why = est.eligibility(key, len(train))
        if why:
            res["candidates"]["ineligible"][key] = why
            continue
        try:
            if key in est.RISK_BASELINES or key not in est.REGISTRY:
                raise ValueError("not a lead-time model")
            if key in ("global_median", "supplier_median", "lane_median", "hierarchical_median"):

                def pred(xs, key=key):  # noqa: ANN001, ANN202
                    r = [base.predict(key, e.d.supplier_id, e.d.destination_id, e.d.product_id) for e in xs]
                    return [x[0] for x in r], [x[1] for x in r]

                pv, lv = pred(val)
                pt, lt = pred(test)
                ptr, _ = pred(train)
                share = {lvl: sum(1 for x in lt if x == lvl) / len(lt) for lvl in set(lt)}
                extra: dict[str, Any] = {"levels_used_on_test": share, "fallback_share_on_test": 1 - share.get("global", 0) if key == "global_median" else share.get("global", 0)}  # fmt: skip
                fitted[key] = {"kind": "baseline"}
            else:
                m = est.fit_ridge_model(train, cfg.prediction_point, cfg.ridge_lambda, limits.max_levels_per_feature)
                if m is None:
                    res["candidates"]["failed"][key] = "singular system: the features are collinear or constant"
                    continue
                if m["spec"].dim() > limits.max_features + 40:
                    res["candidates"]["failed"][key] = "too many features"
                    continue
                rv, rt, rtr = (
                    est.raws_of(val, cfg.prediction_point),
                    est.raws_of(test, cfg.prediction_point),
                    est.raws_of(train, cfg.prediction_point),
                )
                pv, cv = est.predict_ridge(m, rv)
                pt, ct = est.predict_ridge(m, rt)
                ptr, _ = est.predict_ridge(m, rtr)
                extra = {"negative_predictions_clamped": {"validation": cv, "test": ct}}
                fitted[key] = {"kind": "learned", "model": m}
        except Exception as exc:  # noqa: BLE001
            res["candidates"]["failed"][key] = f"{type(exc).__name__}"
            continue
        mv, mt, mtr = M.regression(pv, yv), M.regression(pt, yt), M.regression(ptr, [e.y for e in train])
        ev[key] = {
            "validation": mv,
            "test": mt,
            "training_in_sample": mtr,
            "test_by_lead_time_range": M.by_range(pt, yt),
            **extra,
            "preds": {"val": pv, "test": pt},
        }
        cand[key] = {
            "kind": fitted[key]["kind"],
            "val": mv["mae"]["value"],
            "test": mt["mae"]["value"],
            "complexity": est.COMPLEXITY[key],
        }
        res["candidates"]["evaluated"].append(key)
    if not cand:
        res["status"] = "insufficient_data"
        res["decline_reason"] = "No model was eligible or all failed; see candidates."
        return {"evaluation": {}, "selection": {"status": "none", "selected": None, "rationale": res["decline_reason"]}}
    sel = _select(cand, higher_better=False, min_impr=cfg.min_improvement)
    chosen = sel["selected"]
    for k in ev:
        ev[k].pop("preds", None)
    pv_sel = _predict_sel(chosen, fitted, base, val, cfg)
    pt_sel = _predict_sel(chosen, fitted, base, test, cfg)
    resid = [a - p for a, p in zip(yv, pv_sel, strict=True)]
    need = required_n(cfg.interval_level, 9)
    off = offsets(resid, cfg.interval_level) if len(resid) >= need else None
    interval: dict[str, Any] = {"level": cfg.interval_level, "method": "empirical quantiles of validation-split residuals (actual - prediction); absolute offsets", "n_residuals": len(resid), "required_n": need, "supported": off is not None, "unit": cfg.unit}  # fmt: skip
    if off:
        lo, hi = off
        inside = [1 for p, a in zip(pt_sel, yt, strict=True) if p + lo <= a <= p + hi]
        interval.update({"lower_offset": lo, "upper_offset": hi, "test_coverage": {"n": len(yt), "empirical_coverage": len(inside) / len(yt), "mean_width": hi - lo, "note": "Coverage on the held-out test split is an estimate from limited data and may differ from the nominal level."}})  # fmt: skip
    else:
        interval["note"] = f"Not supported: {len(resid)} validation residuals; at least {need} are needed for a {cfg.interval_level:.0%} interval. No bounds are produced."  # fmt: skip
    expl = _lead_explanation(chosen, fitted, val, cfg, yv)
    art = {"model": chosen, "kind": "lead_time", "point": cfg.prediction_point, "definition": cfg.definition, "unit": cfg.unit, "interval": interval if off else None, "baseline": {"min_group_n": cfg.min_group_n, "half_life_days": cfg.recency_half_life_days}}  # fmt: skip
    if fitted[chosen]["kind"] == "learned":
        m = fitted[chosen]["model"]
        art.update({"spec": m["spec"].to_json(), "beta": m["beta"]})
    return {"evaluation": {"target": "LEAD_TIME", "unit": cfg.unit, "selection_metric": "mae (validation)", "models": ev}, "selection": sel, "intervals": interval, "explanation": expl, "artifact": art, "calibration": None, "selected_model": chosen}  # fmt: skip


def _predict_sel(key: str, fitted: dict[str, Any], base, xs, cfg) -> list[float]:  # noqa: ANN001
    if fitted[key]["kind"] == "baseline":
        return [base.predict(key, e.d.supplier_id, e.d.destination_id, e.d.product_id)[0] for e in xs]
    return est.predict_ridge(fitted[key]["model"], est.raws_of(xs, cfg.prediction_point))[0]


def _lead_explanation(chosen: str, fitted: dict[str, Any], val, cfg, yv) -> dict[str, Any]:  # noqa: ANN001
    ex: dict[str, Any] = {
        "model": chosen,
        "statement": "Importance describes how the model uses its inputs; it is not evidence of causation.",
    }
    if fitted[chosen]["kind"] != "learned":
        ex["method"] = (
            "none: the selected model is a transparent history lookup (see the level and sample size reported with each estimate)"
        )
        return ex
    m = fitted[chosen]["model"]
    raws = est.raws_of(val, cfg.prediction_point)

    def loss(rs: list[tuple]) -> float:
        p, _ = est.predict_ridge(m, rs)
        return sum(abs(a - b) for a, b in zip(p, yv, strict=True)) / len(yv)

    groups = list(F.CATEGORICAL[cfg.prediction_point]) + F.numeric_names(cfg.prediction_point)
    ex.update({"method": "ridge coefficients (standardised features) and permutation importance on the validation split (MAE increase, 3 repeats, fixed seed)", "coefficients": _top_coefs(m["spec"], m["beta"]), "permutation_importance": _perm_importance(loss, raws, groups, cfg.seed)[:15]})  # fmt: skip
    return ex


def _risk(train, val, test, cfg, limits, check, res) -> dict[str, Any]:  # noqa: ANN001, C901
    keys = cfg.models or list(RISK_MODELS)
    ytr, yv, yt = [int(e.y) for e in train], [int(e.y) for e in val], [int(e.y) for e in test]
    pos = sum(ytr)
    prev = pos / len(ytr)
    res["preparation"]["class_balance"] = {"train": {"late": pos, "on_time": len(ytr) - pos}, "validation": {"late": sum(yv), "on_time": len(yv) - sum(yv)}, "test": {"late": sum(yt), "on_time": len(yt) - sum(yt)}}  # fmt: skip
    if sum(yv) == 0 or sum(yt) == 0:
        res["warnings"].append(
            "The validation or test split contains no late delivery: PR-AUC and ROC-AUC are undefined there and model comparison is not possible."
        )
    cand: dict[str, dict[str, Any]] = {}
    ev: dict[str, Any] = {}
    fitted: dict[str, Any] = {}
    for key in keys:
        check()
        why = est.eligibility(key, len(train), pos)
        if why:
            res["candidates"]["ineligible"][key] = why
            continue
        try:
            if key == "base_rate":
                sv, st, str_ = [prev] * len(val), [prev] * len(test), [prev] * len(train)
                fitted[key] = {"kind": "baseline"}
            elif key == "supplier_rate":
                sr = est.SupplierRate(train)
                sv, st, str_ = [sr.predict(e.d.supplier_id) for e in val], [sr.predict(e.d.supplier_id) for e in test], [sr.predict(e.d.supplier_id) for e in train]  # fmt: skip
                fitted[key] = {"kind": "baseline", "sr": sr}
            else:
                m = est.fit_logistic_model(
                    train, cfg.prediction_point, cfg.ridge_lambda, limits.max_levels_per_feature, check
                )
                if m is None:
                    res["candidates"]["failed"][key] = "fit failed (numerical)"
                    continue
                if not m["info"]["converged"]:
                    res["warnings"].append(
                        f"{key}: Newton iterations did not fully converge ({m['info']['iterations']} iterations); results kept and flagged."
                    )
                sv, st, str_ = (
                    est.predict_logistic(m, est.raws_of(x, cfg.prediction_point)) for x in (val, test, train)
                )
                fitted[key] = {"kind": "learned", "model": m}
        except Exception as exc:  # noqa: BLE001
            res["candidates"]["failed"][key] = f"{type(exc).__name__}"
            continue
        thr, f1v = _threshold(sv, yv)
        cv, ct = M.classification(sv, yv, thr, prev), M.classification(st, yt, thr, prev)
        entry = {"validation": cv, "test": ct, "threshold": {"value": thr, "selected_on": "validation (maximises F1)", "validation_f1": f1v}, "training_in_sample": {"brier_raw": M.brier(str_, ytr)}, "scores": {"val": sv, "test": st}}  # fmt: skip
        ev[key] = entry
        cand[key] = {
            "kind": fitted[key]["kind"],
            "val": cv["pr_auc"],
            "test": ct["pr_auc"],
            "complexity": est.COMPLEXITY[key],
        }
        res["candidates"]["evaluated"].append(key)
    if not cand or all(c["val"] is None for c in cand.values()):
        res["status"] = "insufficient_data"
        res["decline_reason"] = (
            "No candidate has a defined PR-AUC on the validation split (no late deliveries there, or no eligible model)."
        )
        for v in ev.values():
            v.pop("scores", None)
        return {"evaluation": {"target": "LATE_RISK", "models": ev}, "selection": {"status": "none", "selected": None, "rationale": res["decline_reason"]}}  # fmt: skip
    sel = _select(
        {k: v for k, v in cand.items() if v["val"] is not None}, higher_better=True, min_impr=cfg.min_improvement
    )
    chosen = sel["selected"]
    cal = None
    kind = "historical_rate"
    ab = None
    sv, st = ev[chosen]["scores"]["val"], ev[chosen]["scores"]["test"]
    if fitted[chosen]["kind"] == "learned":
        cal = _calibrate(sv, yv, st, yt, prev)
        kind = "calibrated_probability" if cal["decision"] == "calibrated_probability" else "uncalibrated_score"
        ab = cal.get("params")
    for v in ev.values():
        v.pop("scores", None)
    expl = _risk_explanation(chosen, fitted, val, cfg, yv)
    art: dict[str, Any] = {"model": chosen, "kind": "late_risk", "point": cfg.prediction_point, "commitment": cfg.commitment, "grace_hours": cfg.grace_hours, "score_kind": kind, "threshold": ev[chosen]["threshold"]["value"], "train_prevalence": prev, "platt": ab if kind == "calibrated_probability" else None}  # fmt: skip
    if chosen == "supplier_rate":
        art["supplier_rate"] = fitted[chosen]["sr"].to_json()
    if fitted[chosen]["kind"] == "learned":
        m = fitted[chosen]["model"]
        art.update({"spec": m["spec"].to_json(), "beta": m["beta"]})
    return {"evaluation": {"target": "LATE_RISK", "selection_metric": "pr_auc (validation)", "models": ev}, "selection": sel, "calibration": cal, "explanation": expl, "intervals": None, "artifact": art, "selected_model": chosen, "score_kind": kind}  # fmt: skip


def _calibrate(sv: list[float], yv: list[int], st: list[float], yt: list[int], prev: float) -> dict[str, Any]:
    reasons: list[str] = []
    out: dict[str, Any] = {"method": "platt (sigmoid on logit of the raw score)", "fitted_on": "validation split", "criteria": CAL, "n_validation": len(yv), "n_test": len(yt)}  # fmt: skip
    pv, nv = sum(yv), len(yv) - sum(yv)
    pt_, nt = sum(yt), len(yt) - sum(yt)
    if len(yv) < CAL["min_validation"] or min(pv, nv) < CAL["min_class_validation"]:
        reasons.append(f"validation split too small for calibration ({len(yv)} examples; {pv} late / {nv} on time; need {CAL['min_validation']} with at least {CAL['min_class_validation']} per class)")  # fmt: skip
    if len(yt) < CAL["min_test"] or min(pt_, nt) < CAL["min_class_test"]:
        reasons.append(f"test split too small to verify calibration ({len(yt)} examples; {pt_} late / {nt} on time; need {CAL['min_test']} with at least {CAL['min_class_test']} per class)")  # fmt: skip
    ab = platt(sv, yv) if len(set(yv)) == 2 else None
    if ab is None:
        reasons.append("calibration map could not be fitted")
    else:
        out["params"] = list(ab)
        cal_t = [apply_platt(s, ab) for s in st]
        rel = M.reliability(cal_t, yt)
        bc = M.brier(cal_t, yt)
        bb = M.brier([prev] * len(yt), yt)
        out["test"] = {
            "brier_calibrated": bc,
            "brier_raw": M.brier(st, yt),
            "brier_baseline_train_prevalence": bb,
            "ece": rel["ece"],
            "reliability": rel,
        }
        if rel["ece"] is None:
            reasons.append("too few test examples for a reliability assessment")
        elif rel["ece"] > CAL["max_ece"]:
            reasons.append(f"expected calibration error on the test split is {rel['ece']:.3f} (limit {CAL['max_ece']})")
        if bc is not None and bb is not None and not bc < bb:
            reasons.append("calibrated Brier score does not beat the constant train-prevalence baseline")
    out["reasons_not_calibrated"] = reasons
    out["decision"] = "calibrated_probability" if not reasons else "uncalibrated_score"
    out["statement"] = STATEMENTS["score"]
    return out


def _risk_explanation(chosen: str, fitted: dict[str, Any], val, cfg, yv) -> dict[str, Any]:  # noqa: ANN001
    ex: dict[str, Any] = {
        "model": chosen,
        "statement": "Importance describes how the model uses its inputs; it is not evidence of causation.",
    }
    if fitted[chosen]["kind"] != "learned":
        ex["method"] = (
            "none: the selected model is a historical rate (see the supplier and sample size reported with each assessment)"
        )
        return ex
    m = fitted[chosen]["model"]
    raws = est.raws_of(val, cfg.prediction_point)

    def loss(rs: list[tuple]) -> float:
        p = est.predict_logistic(m, rs)
        return M.brier(p, yv) or 0.0

    groups = list(F.CATEGORICAL[cfg.prediction_point]) + F.numeric_names(cfg.prediction_point)
    ex.update({"method": "logistic coefficients (standardised features, log-odds) and permutation importance on the validation split (Brier increase, 3 repeats, fixed seed)", "coefficients": _top_coefs(m["spec"], m["beta"]), "permutation_importance": _perm_importance(loss, raws, groups, cfg.seed)[:15]})  # fmt: skip
    return ex
