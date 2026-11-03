"""Reproducible lead-time benchmark on SYNTHETIC data (software verification only; says nothing about real suppliers).

Run from ``backend/``:  python ../scripts/leadtime_benchmark.py [--sizes 300,1000,3000] [--seed 7] [--out FILE]
Timings are wall-clock on the machine that runs it; counts, splits and metrics are deterministic for a given seed.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time

sys.path.insert(0, ".")
from app.leadtime import analytics  # noqa: E402
from app.leadtime.config import LeadTimeConfig, LeadTimeLimits  # noqa: E402
from app.leadtime.pipeline import run_pipeline  # noqa: E402
from tests.leadtime_helpers import NOW, gen_rows, special_rows, to_deliveries  # noqa: E402


def one(n: int, seed: int) -> dict:
    ds = to_deliveries(gen_rows(n, seed) + list(special_rows().values()))
    out: dict = {"records": len(ds), "seed": seed}
    t = time.perf_counter()
    analytics.summarize(ds, LeadTimeConfig(), NOW)
    analytics.group_summary(ds, LeadTimeConfig(), NOW, "supplier")
    out["analytics_seconds"] = round(time.perf_counter() - t, 3)
    for name, cfg in (
        ("lead_time", LeadTimeConfig(target="LEAD_TIME", seed=seed)),
        ("late_risk_order_placement", LeadTimeConfig(target="LATE_RISK", seed=seed)),
        ("late_risk_dispatch", LeadTimeConfig(target="LATE_RISK", prediction_point="DISPATCH", seed=seed)),
    ):
        t = time.perf_counter()
        r = run_pipeline(ds, cfg, LeadTimeLimits(time_budget_seconds=600), NOW, lambda: None)
        dt = time.perf_counter() - t
        sp = r.get("splits") or {}
        row = {
            "status": r["status"], "seconds": round(dt, 2), "selected": r.get("selected_model"),
            "selection": (r.get("selection") or {}).get("status"),
            "split_n": {k: sp[k]["n"] for k in ("train", "validation", "test") if k in sp}, "purged_from_train": sp.get("purged_from_train"),
        }  # fmt: skip
        ev = (r.get("evaluation") or {}).get("models") or {}
        if r.get("selected_model") in ev:
            row["selected_test"] = {
                k: v for k, v in ev[r["selected_model"]].get("test", {}).items() if isinstance(v, (int, float))
            }
        if cfg.target == "LATE_RISK":
            row["score_kind"] = r.get("score_kind")
        else:
            row["interval"] = {
                k: v for k, v in (r.get("intervals") or {}).items() if k in ("supported", "level", "test_coverage")
            }
        out[name] = row
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="300,1000,3000")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out")
    a = ap.parse_args()
    res = {
        "python": platform.python_version(),
        "machine": platform.platform(),
        "synthetic": True,
        "runs": [one(int(n), a.seed) for n in a.sizes.split(",")],
    }
    text = json.dumps(res, indent=2, default=str)
    print(text)
    if a.out:
        open(a.out, "w").write(text)


if __name__ == "__main__":
    main()
