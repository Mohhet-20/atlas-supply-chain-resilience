"""Reproducible forecast benchmark on SYNTHETIC series (seeded). Not evidence about real demand or about any model's general
superiority: every figure is for the invented series below. Usage: python scripts/forecast_benchmark.py [--json]"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.forecast import backtest as bt  # noqa: E402
from app.forecast.config import ForecastConfig, ForecastLimits  # noqa: E402
from app.forecast.models import run_model  # noqa: E402
from app.forecast.pipeline import run_pipeline  # noqa: E402
from app.forecast.prep import RawObs  # noqa: E402

D0 = date(2026, 1, 5)
SHAPE = [0.6, 0.7, 0.8, 0.9, 1.0, 1.9, 2.1]


def make(kind: str, n: int, seed: int) -> list[float]:
    r = random.Random(seed)
    if kind == "stable":
        return [50 + r.gauss(0, 2) for _ in range(n)]
    if kind == "trend":
        return [20 + 0.4 * i + r.gauss(0, 2) for i in range(n)]
    if kind == "seasonal":
        return [100 * SHAPE[i % 7] + r.gauss(0, 3) for i in range(n)]
    return [float(r.choice([3, 5, 8])) if r.random() < 0.3 else 0.0 for _ in range(n)]


def obs(vals: list[float]) -> list[RawObs]:
    return [
        RawObs(i, "DEMAND", D0 + timedelta(days=i), D0 + timedelta(days=i + 1), v, "EA", f"h{i}")
        for i, v in enumerate(vals)
    ]


def main() -> None:
    rows = []
    lim = ForecastLimits()
    for n in (60, 180, 365, 730):
        for kind in ("stable", "trend", "seasonal", "intermittent"):
            vals = make(kind, n, seed=n)
            cfg = ForecastConfig(horizon=14, n_folds=6, lookback_periods=min(730, n))
            t = time.perf_counter()
            res = run_pipeline(obs(vals), cfg, lim)
            total = time.perf_counter() - t
            row = {"kind": kind, "periods": n, "status": res["status"], "total_s": round(total, 3)}
            if "timings" in res:
                row |= {
                    "prepare_s": res["timings"].get("prepare"),
                    "backtest_s": res["timings"].get("backtest"),
                    "final_fit_s": res["timings"].get("final_fit"),
                }
            if res.get("model"):
                sel = res["model"]["key"]
                ev = res["evaluation"]
                row |= {"selected": sel, "selection_status": res["selection"]["status"], "mase": ev[sel]["overall"]["mase"]["value"], "naive_mase": ev.get("naive", {}).get("overall", {}).get("mase", {}).get("value"), "folds": len(res["validation"]["fold_train_lengths"])}  # fmt: skip
            rows.append(row)
    # single-fit cost per model on 365 periods
    y = make("seasonal", 365, 1)
    d = [D0 + timedelta(days=i) for i in range(365)]
    fit = {}
    for k in (
        "naive",
        "seasonal_naive",
        "moving_average",
        "ses",
        "holt_damped",
        "holt_winters",
        "arima_like",
        "lag_ridge",
    ):
        t = time.perf_counter()
        run_model(k, y, 14, 7, d, "daily")
        fit[k] = round(time.perf_counter() - t, 4)
    _ = bt
    out = {
        "note": "synthetic series only; timings are wall-clock on this machine",
        "pipeline": rows,
        "single_fit_seconds_365_periods": fit,
    }
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
        return
    for r in rows:
        print(r)
    print("single fit (s), 365 periods:", fit)


if __name__ == "__main__":
    main()
