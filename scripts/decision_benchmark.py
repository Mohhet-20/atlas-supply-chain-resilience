"""Benchmark of the pure decision comparison (SYNTHETIC snapshots; measures the software, not real supply chains).

Usage (from backend/): python ../scripts/decision_benchmark.py [out.json]
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time

from app.decision import compare

N = "NORMAL"
COST = {"currency": "USD", "basis": "PURCHASE_ONLY"}


def snapshot(plans: int, conds: int, seed: int = 1) -> dict:
    rnd = random.Random(seed)
    cs = [{"key": N, "name": N, "mode": "NORMAL", "probability": None}] + [
        {"key": f"c{i} [FIXED]", "name": f"c{i}", "mode": "FIXED", "probability": None} for i in range(conds)
    ]
    scope = {"product_id": 1, "destination_facility_id": 1, "quantity": 100, "unit": "kg", "currency": "USD"}
    ps = [
        {
            "key": f"P{i}",
            "label": f"P{i}",
            "role": "BASELINE" if i == 0 else "ALTERNATIVE",
            "source": f"P{i}",
            "scope": scope,
            "allocation": [],
            "constraint_checks": [],
            "feasible": True,
            "reference_kind": None,
        }
        for i in range(plans)
    ]
    cells = {
        p["key"]: {
            c["key"]: {
                "MODELLED_COST": {"value": rnd.uniform(50, 150), "unit": "currency", **COST},
                "UNMET_UNITS": {"value": rnd.uniform(0, 30), "unit": "units"},
                "FULFILMENT": {"value": rnd.uniform(0.6, 1), "unit": "fraction"},
            }
            for c in cs
        }
        for p in ps
    }
    return {
        "algorithm_version": "dc-1",
        "metric_registry_version": "mr-1",
        "horizon_days": None,
        "assumptions": [],
        "scenario_ids": [],
        "plans": ps,
        "conditions": cs,
        "cells": cells,
        "exposure": [],
        "probability_basis": None,
        "evidence": [],
        "warnings": [],
        "issues": [],
    }


def main() -> None:
    out = []
    for plans, conds in [(2, 2), (5, 5), (12, 10), (40, 20)]:
        s = snapshot(plans, conds)
        t = []
        for _ in range(15):
            a = time.perf_counter()
            r = compare.evaluate(s)
            t.append((time.perf_counter() - a) * 1000)
        again = compare.evaluate(s)
        out.append(
            {
                "plans": plans,
                "conditions": conds,
                "cells": plans * (conds + 1) * 3,
                "median_ms": round(statistics.median(t), 2),
                "max_ms": round(max(t), 2),
                "deterministic": r["result_hash"] == again["result_hash"],
            }
        )
        print(out[-1])
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as f:
            json.dump({"note": "SYNTHETIC; measures the pure comparison only", "results": out}, f, indent=2)


if __name__ == "__main__":
    main()
