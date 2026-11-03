"""Forecast generation pipeline (pure; no database). prepare → diagnose → classify → eligibility → backtest → select →
intervals → final fit → non-negativity → package. Cooperative cancellation via ``check``; compute is bounded by the limits."""

from __future__ import annotations

import platform
import sys
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from app.forecast import backtest as bt
from app.forecast import diagnostics as dg
from app.forecast import features as feat
from app.forecast import intervals as iv
from app.forecast.config import (
    ALGORITHM_VERSION,
    BASELINE_KEYS,
    PERIOD_DAYS,
    STATEMENTS,
    ForecastConfig,
    ForecastLimits,
)
from app.forecast.models import MODEL_VERSION, REGISTRY, ModelFailure, eligibility, run_model
from app.forecast.prep import Prepared, RawObs, prepare

FALLBACK_KEY = "moving_average"


def reproducibility(cfg: ForecastConfig, snapshot: str) -> dict[str, Any]:
    deps: dict[str, str] = {}
    for name in ("fastapi", "pydantic", "sqlalchemy", "alembic"):
        try:
            from importlib.metadata import version

            deps[name] = version(name)
        except Exception:  # noqa: BLE001
            deps[name] = "unknown"
    return {
        "algorithm_version": ALGORITHM_VERSION, "model_version": MODEL_VERSION, "feature_version": feat.FEATURE_VERSION,
        "python": sys.version.split()[0], "platform": platform.system(), "dependencies": deps,
        "numeric_libraries": "none (stdlib only)", "seed": cfg.seed, "deterministic": True,
        "config_digest": cfg.digest(), "data_snapshot_hash": snapshot,
    }  # fmt: skip


def candidate_list(cfg: ForecastConfig, p: Prepared, season: int | None) -> tuple[list[str], dict[str, str]]:
    y = p.values
    zp = sum(1 for v in y if v <= 0) / len(y)
    nz = sum(1 for v in y if v > 0)
    keys = cfg.candidates or list(REGISTRY)
    ok, why = [], {}
    for k in keys:
        good, reason = eligibility(REGISTRY[k], n=len(y), season=season, freq=cfg.frequency, zero_prop=zp, nonzero=nz)
        if good:
            ok.append(k)
        else:
            why[k] = reason or "ineligible"
    # a baseline is always compared when it is eligible, even if the caller restricted the candidate list
    for b in BASELINE_KEYS:
        if b not in ok and b not in why:
            good, _ = eligibility(REGISTRY[b], n=len(y), season=season, freq=cfg.frequency, zero_prop=zp, nonzero=nz)
            if good:
                ok.insert(0, b)
    return ok, why


def run_pipeline(
    obs: list[RawObs],
    cfg: ForecastConfig,
    limits: ForecastLimits | None = None,
    check: Callable[[], None] | None = None,
) -> dict[str, Any]:  # noqa: C901
    limits = limits or ForecastLimits()
    cfg.check_limits(limits)
    check = check or (lambda: None)
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    p = prepare(obs, cfg, limits)
    timings["prepare"] = time.perf_counter() - t0
    result: dict[str, Any] = {
        "statements": STATEMENTS, "preparation": p.summary(), "config": cfg.canonical(), "status": "insufficient_data",
        "warnings": list(p.warnings), "reproducibility": reproducibility(cfg, p.snapshot_hash),
    }  # fmt: skip
    if not p.usable:
        result["status"] = p.status
        result["decline_reason"] = p.warnings[-1] if p.warnings else p.status
        result["timings"] = timings
        return result
    season = cfg.season()
    if season and len(p.values) < 2 * season:
        result["warnings"].append(
            f"Fewer than two full cycles of the seasonal period {season}; seasonal models are ineligible."
        )
    diag = dg.diagnose(p, season)
    pat = dg.classify(diag, limits.min_history_periods)
    result.update(diagnostics=diag, patterns=pat)
    if "insufficient_history" in pat["labels"]:
        result["status"] = "insufficient_data"
        result["decline_reason"] = "history too short to forecast responsibly"
        result["timings"] = timings
        return result
    check()
    y, dates, observed = p.values, p.dates, p.observed
    cands, inelig = candidate_list(cfg, p, season)
    plan = bt.plan_folds(len(y), cfg, limits, season)
    result["candidates"] = {"evaluated": cands, "ineligible": inelig}
    result["validation"] = {
        "scheme": "expanding-window rolling origin", "naive_scale": bt.fold_scale(y), "fold_train_lengths": plan.origins, "evaluation_horizon": plan.horizon,
        "configured_horizon": cfg.horizon, "min_train_periods": plan.min_train, "status": plan.status, "note": plan.note,
        "leakage_controls": ["each fold trains on y[:origin] only", "features use shifted history", "imputed/excluded actuals are never scored", "model selection uses only backtest folds, never the forecast period"],  # fmt: skip
    }  # fmt: skip
    evals: dict[str, dict[str, Any]] = {}
    raw_evals: dict[str, bt.ModelEval] = {}
    t1 = time.perf_counter()
    if plan.origins:
        for k in cands:
            check()
            ts = time.perf_counter()
            ev = bt.backtest_model(k, y, dates, observed, plan, season, cfg.frequency)
            ev.seconds = time.perf_counter() - ts
            raw_evals[k] = ev
            evals[k] = bt.evaluate(ev, y, plan)
            evals[k]["failures"] = ev.failures
            evals[k]["seconds"] = round(ev.seconds, 4)
    timings["backtest"] = time.perf_counter() - t1
    sel = bt.select(evals, cfg, plan)
    result["evaluation"] = evals
    result["selection"] = sel
    fallback = None
    key = sel["selected"]
    if key is None:
        # explicit, labelled fallback: never presented as a validated winner
        key = FALLBACK_KEY if FALLBACK_KEY in cands else (cands[0] if cands else "naive")
        fallback = {"used": key, "reason": sel["rationale"], "validated": False}
        result["warnings"].append(f"No model could be validated ({sel['status']}); a labelled fallback ({key}) is used and carries no accuracy claim.")  # fmt: skip
    check()
    t2 = time.perf_counter()
    out = None
    tried = [key] + [r["model"] for r in sel.get("ranking", []) if r["model"] != key]
    for cand in tried:
        try:
            out = run_model(cand, y, cfg.horizon, season, dates, cfg.frequency)
            if cand != key:
                fallback = {"used": cand, "from": key, "reason": f"{key} failed on the full history", "validated": cand in evals and evals[cand]["complete"]}  # fmt: skip
                result["warnings"].append(
                    f"{key} could not be refitted on the full history; fell back to {cand} (labelled)."
                )
                key = cand
            break
        except ModelFailure as e:
            result["warnings"].append(f"{cand} final fit failed: {e}")
    if out is None:
        out = run_model("naive", y, cfg.horizon, season, dates, cfg.frequency)
        fallback = {"used": "naive", "reason": "every candidate failed on the full history", "validated": False}
        key = "naive"
    timings["final_fit"] = time.perf_counter() - t2
    # intervals (selected, validated model only)
    levels: list[dict[str, Any]] = []
    ev_sel = raw_evals.get(key)
    can_iv = ev_sel is not None and sel["selected"] == key and plan.status == "ok" and evals[key]["complete"]
    for lv in cfg.interval_levels:
        if not can_iv:
            levels.append({"level": lv, "supported": False, "reason": "no validated model, so no calibrated interval"})
            continue
        cal = iv.calibrate(ev_sel.records, plan.horizon, lv, limits.min_interval_calibration_n)  # type: ignore[union-attr]
        cal["coverage_evaluation"] = iv.coverage(
            ev_sel.records, plan.horizon, lv, limits.min_interval_calibration_n, cfg.nonnegative
        )  # type: ignore[union-attr]
        levels.append(cal)
    # package values (store unrounded; report the effect of non-negative clamping)
    step = PERIOD_DAYS[cfg.frequency]
    origin = p.origin
    assert origin is not None
    rows: list[dict[str, Any]] = []
    clamp = {"points_clamped": 0, "bounds_clamped": 0, "total_point_shift": 0.0}
    for h in range(1, cfg.horizon + 1):
        raw = out.values[h - 1]
        pt = max(0.0, raw) if cfg.nonnegative else raw
        if pt != raw:
            clamp["points_clamped"] += 1
            clamp["total_point_shift"] += pt - raw
        row: dict[str, Any] = {"h": h, "period_start": (origin + timedelta(days=step * (h - 1))).isoformat(), "point": pt, "point_raw": raw, "intervals": []}  # fmt: skip
        for c in levels:
            o = c.get("per_horizon", {}).get(h) if c.get("supported") else None
            if o is None:
                row["intervals"].append({"level": c["level"], "lower": None, "upper": None, "status": "unsupported"})
                continue
            lo, hi = raw + o["lower_offset"], raw + o["upper_offset"]
            if cfg.nonnegative:
                if lo < 0 or hi < 0:
                    clamp["bounds_clamped"] += 1
                lo, hi = max(0.0, lo), max(0.0, hi)
            row["intervals"].append({"level": c["level"], "lower": lo, "upper": hi, "status": "supported", "n": o["n"]})
        rows.append(row)
    result.update(
        status="completed" if fallback is None else "completed_with_fallback",
        model={"key": key, "label": REGISTRY[key].label, "version": REGISTRY[key].version, "params": out.params, "family": REGISTRY[key].family},  # fmt: skip
        fallback=fallback, interval_calibration=levels, values=rows, nonnegative_clamping={**clamp, "enabled": cfg.nonnegative},
        feature_lineage=feat.lineage(season, cfg.frequency) if key == "lag_ridge" else None,
        origin=origin.isoformat(), horizon=cfg.horizon, unit=p.unit, frequency=cfg.frequency, training_periods=len(y),
        training_interval={"start": dates[0].isoformat(), "end_exclusive": origin.isoformat()},
    )  # fmt: skip
    if plan.horizon < cfg.horizon:
        result["warnings"].append(f"Intervals and accuracy were validated only up to horizon {plan.horizon}; later horizons carry no bounds.")  # fmt: skip
    timings["total"] = time.perf_counter() - t0
    result["timings"] = {k: round(v, 4) for k, v in timings.items()}
    return result
