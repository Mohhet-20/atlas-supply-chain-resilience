"""Rolling-origin (expanding window) backtests and transparent, deterministic model selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.forecast import metrics as mx
from app.forecast.config import BASELINE_KEYS, ForecastConfig, ForecastLimits
from app.forecast.models import REGISTRY, ModelFailure, run_model


@dataclass
class FoldPlan:
    origins: list[int]  # training length per fold
    horizon: int
    min_train: int
    status: str  # ok | insufficient_evidence
    note: str | None = None


@dataclass
class ModelEval:
    key: str
    records: list[tuple[int, int, float, float]] = field(default_factory=list)  # (fold, h, actual, forecast)
    failures: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0


def plan_folds(n: int, cfg: ForecastConfig, limits: ForecastLimits, season: int | None) -> FoldPlan:
    min_train = cfg.min_train_periods or max(limits.min_history_periods, 2 * (season or 0), n // 2)
    H = min(cfg.horizon, max(1, (n - min_train) // 2))
    if n - H < min_train:
        return FoldPlan([], H, min_train, "insufficient_evidence", f"history of {n} periods cannot support an evaluation window after {min_train} training periods")  # fmt: skip
    k_max = (n - H - min_train) // cfg.fold_step + 1
    k = min(cfg.n_folds, k_max)
    origins = [n - H - (k - 1 - i) * cfg.fold_step for i in range(k)]
    st = "ok" if k >= limits.min_folds_for_selection else "insufficient_evidence"
    note = (
        None
        if st == "ok"
        else f"only {k} fold(s) available; at least {limits.min_folds_for_selection} are needed for selection"
    )
    if H < cfg.horizon:
        note = (note + "; " if note else "") + f"evaluation horizon limited to {H} (configured {cfg.horizon})"
    return FoldPlan(origins, H, min_train, st, note)


def fold_scale(y: list[float]) -> float | None:
    if len(y) < 2:
        return None
    s = sum(abs(y[i] - y[i - 1]) for i in range(1, len(y))) / (len(y) - 1)
    return s if s > 1e-12 else None


def backtest_model(
    key: str, y: list[float], dates: list[date], observed: list[bool], plan: FoldPlan, season: int | None, freq: str
) -> ModelEval:
    ev = ModelEval(key)
    for fi, t in enumerate(plan.origins):
        try:
            out = run_model(key, y[:t], plan.horizon, season, dates[:t], freq)
        except ModelFailure as e:
            ev.failures.append({"fold": fi, "train_periods": t, "error": str(e)})
            continue
        for h in range(1, plan.horizon + 1):
            if observed[t + h - 1]:  # imputed/excluded actuals are never scored
                ev.records.append((fi, h, y[t + h - 1], out.values[h - 1]))
    return ev


def evaluate(ev: ModelEval, y: list[float], plan: FoldPlan) -> dict[str, Any]:
    scales = {fi: fold_scale(y[:t]) for fi, t in enumerate(plan.origins)}
    res = mx.split(ev.records, scales)
    res["failed_folds"] = len({f["fold"] for f in ev.failures})
    res["complete"] = not ev.failures and bool(ev.records)
    return res


def select(evals: dict[str, dict[str, Any]], cfg: ForecastConfig, plan: FoldPlan) -> dict[str, Any]:
    """Deterministic selection: best primary metric, simplest within 1 %, and a baseline must be beaten by ≥ min_improvement
    **and** in a majority of folds, otherwise the best baseline is kept ("no reliable winner")."""
    metric = cfg.selection_metric
    cands = {k: e for k, e in evals.items() if e["complete"]}
    skipped = {
        k: ("failed or unscored in some folds" if not e["complete"] else None)
        for k, e in evals.items()
        if not e["complete"]
    }
    if plan.status != "ok" or not cands:
        return {"status": "insufficient_evidence", "selected": None, "metric": metric, "ranking": [], "skipped": skipped,
                "rationale": plan.note or "no candidate could be evaluated on every fold"}  # fmt: skip
    used = metric if all(e["overall"][metric]["value"] is not None for e in cands.values()) else "mae"
    score = {k: e["overall"][used]["value"] for k, e in cands.items() if e["overall"][used]["value"] is not None}
    if not score:
        return {"status": "insufficient_evidence", "selected": None, "metric": metric, "ranking": [], "skipped": skipped, "rationale": f"{metric} is undefined for every candidate (e.g. all-zero actuals)"}  # fmt: skip
    ranking = sorted(score, key=lambda k: (score[k], REGISTRY[k].rank, k))
    best_v = score[ranking[0]]
    near = [k for k in ranking if score[k] <= best_v * 1.01 + 1e-12]
    pick = min(near, key=lambda k: (REGISTRY[k].rank, k))
    bases = [k for k in ranking if k in BASELINE_KEYS]
    why = [
        f"Ranked by {used} (lower is better) over {len(plan.origins)} rolling-origin fold(s) at horizon ≤ {plan.horizon}."
    ]
    status = "selected"
    if pick not in BASELINE_KEYS and bases:
        b = bases[0]
        gain = (score[b] - score[pick]) / score[b] if score[b] > 0 else 0.0
        pf, bf = cands[pick]["per_fold"], cands[b]["per_fold"]
        wins = sum(1 for f in pf if (_fv(pf[f], used) is not None and _fv(bf.get(f, {}), used) is not None and _fv(pf[f], used) < _fv(bf[f], used)))  # fmt: skip
        nf = len(pf)
        if gain < cfg.min_improvement or wins * 2 <= nf:
            why.append(f"{pick} improved on the best baseline ({b}) by {gain:.1%} and won {wins}/{nf} folds — below the required {cfg.min_improvement:.0%} improvement and majority of folds; keeping the baseline.")  # fmt: skip
            pick, status = b, "no_reliable_winner"
        else:
            why.append(f"{pick} beat the best baseline ({b}) by {gain:.1%} and won {wins}/{nf} folds.")
    elif pick in BASELINE_KEYS:
        why.append(f"A baseline ({pick}) is the best or within 1% of the best; more complex models did not demonstrate an advantage.")  # fmt: skip
    elif not bases:
        status = "selected_no_baseline_comparison"
        why.append("No baseline could be evaluated on every fold, so the choice is not compared with a baseline.")
    if len(near) > 1 and pick == min(near, key=lambda k: (REGISTRY[k].rank, k)):
        why.append(f"Within 1% of the best score, the simplest model ({pick}) was preferred.")
    return {"status": status, "selected": pick, "metric": used, "ranking": [{"model": k, "score": score[k]} for k in ranking], "skipped": skipped, "rationale": " ".join(why), "best_baseline": bases[0] if bases else None}  # fmt: skip


def _fv(m: dict[str, Any], metric: str) -> float | None:
    return (m.get(metric) or {}).get("value")
