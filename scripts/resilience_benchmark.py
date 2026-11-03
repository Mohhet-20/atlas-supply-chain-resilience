# ruff: noqa: E501
"""Reproducible timing of the resilience optimiser on SYNTHETIC data (software verification only).

Run from ``backend/``:  python ../scripts/resilience_benchmark.py [--out FILE]
engine.compute is a pure function of a snapshot and a request, so no database is needed: it builds synthetic supplier snapshots of increasing
size, times the full search (enumeration, validation, Pareto, scenarios, optional sensitivity) and checks that a repeat returns an identical
recommendation hash. Timings are wall-clock on the machine that runs it; counts and hashes are deterministic. It says nothing about real data
or production scale, and the solver is an approximate grid search, not a proof of global optimality.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, ".")
from app.resilience import engine  # noqa: E402
from app.resilience.config import Limits  # noqa: E402
from app.resilience.plan import Budget  # noqa: E402
from tests.resilience_helpers import objs, outage, req, snap, stress, sup  # noqa: E402


def suppliers(n: int) -> list[dict]:
    return [sup(i + 1, price=10.0 + 1.5 * i, cap=60.0 + 10 * (i % 3), lead=8.0 + 2 * (i % 4)) for i in range(n)]


def timed(s: dict, r, limits: Limits) -> tuple[dict | None, float]:
    """Run engine.compute under the same wall-clock budget the API enforces; None means the budget stopped it."""
    t0 = time.monotonic()

    def stop() -> None:
        if time.monotonic() - t0 > limits.time_budget_seconds:
            raise Budget

    try:
        return engine.compute(s, r, limits, stop=stop), time.perf_counter()
    except Budget:
        return None, time.perf_counter()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).with_name("resilience_benchmark_result.json")))
    a = ap.parse_args()
    lim = Limits()
    res: dict = {
        "python": platform.python_version(), "platform": platform.platform(), "cpus": os.cpu_count(), "synthetic": True,
        "note": "synthetic suppliers (price 10 + 1.5i, capacity 60-80, lead 8-14 d), demand 100, default Limits, one timed run per row after one warm-up; wall-clock seconds on a shared machine",
        "limits": {"time_budget_seconds": lim.time_budget_seconds, "max_candidates": lim.max_candidates, "max_eligible_suppliers": lim.max_eligible_suppliers},
        "rows": [],
    }  # fmt: skip
    cases = [
        ("3 suppliers, 10 steps", 3, 10, 0, False, lim),
        ("5 suppliers, 10 steps", 5, 10, 0, False, lim),
        ("5 suppliers, 10 steps, 4 stress", 5, 10, 4, False, lim),
        ("6 suppliers, 10 steps, 4 stress, sensitivity", 6, 10, 4, True, lim),
        ("8 suppliers, 10 steps, 8 stress", 8, 10, 8, False, lim),
        ("6 suppliers, 20 steps, 4 stress", 6, 20, 4, False, lim),
        ("8 suppliers, 20 steps (candidate cap applies)", 8, 20, 4, False, lim),
        ("8 suppliers, 20 steps, 1 ms time budget (bounded)", 8, 20, 4, False, replace(lim, time_budget_seconds=0.001)),
    ]  # fmt: skip
    for name, n, steps, k, sens, limits in cases:
        sups = suppliers(n)
        kinds = [outage(f"S{i + 1} out", f"OF-{i + 1}") for i in range(min(k, n))]
        s = snap(sups, demand=100.0, scenarios=kinds)
        stresses = [stress(f"S{i + 1} out", supplier_id=i + 1) for i in range(min(k, n))]
        o = objs(
            COST=1,
            CONCENTRATION=1,
            DISRUPTION_EXPOSURE=1,
            DELIVERY_DELAY=0.5,
            **({"WORST_CASE_SERVICE": 1} if stresses else {}),
        )
        r = req(100.0, grid_steps=steps, objectives=o, stress=stresses, sensitivity={"enabled": sens})
        timed(s, r, limits)  # warm-up
        t0 = time.perf_counter()
        out, t1 = timed(s, r, limits)
        dt = t1 - t0
        if out is None:
            res["rows"].append({"case": name, "eligible_suppliers": n, "grid_steps": steps, "stress_scenarios": len(stresses), "sensitivity": sens, "seconds": round(dt, 3), "result_status": "timed_out", "stopped_by_time_budget": True, "repeat_identical": True})  # fmt: skip
            print(f"{name:55s} {dt:7.3f}s  timed_out (time budget {limits.time_budget_seconds}s)")
            continue
        again, _ = timed(s, r, limits)
        st = out["statistics"]
        res["rows"].append({
            "case": name, "eligible_suppliers": n, "grid_steps": steps, "stress_scenarios": len(stresses), "sensitivity": sens, "seconds": round(dt, 3),
            "result_status": out["result_status"], "generated": st.get("generated"), "leaves_visited": st.get("leaves_visited"), "truncated": st.get("truncated"),
            "pareto_front": (out.get("pareto") or {}).get("front_size"), "inexact": out.get("inexact"),
            "recommendation_hash": (out.get("recommendation_hash") or "")[:16], "repeat_identical": again is not None and out.get("recommendation_hash") == again.get("recommendation_hash") and out["result_status"] == again["result_status"],
        })  # fmt: skip
        print(
            f"{name:55s} {dt:7.3f}s  {out['result_status']:24s} generated={st.get('generated')} truncated={st.get('truncated')} front={(out['pareto'] or {}).get('front_size')}"
        )
    res["all_repeats_identical"] = all(r["repeat_identical"] for r in res["rows"])
    Path(a.out).write_text(json.dumps(res, indent=2) + "\n")
    print("written", a.out, "| repeats identical:", res["all_repeats_identical"])


if __name__ == "__main__":
    main()
