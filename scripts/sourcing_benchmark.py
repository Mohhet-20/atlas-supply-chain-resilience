"""Synthetic timing of the sourcing allocation solver (pure Python). Not a statement about real-data or production-scale performance."""

import json
import os
import platform
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.sourcing import solver  # noqa: E402

rng = random.Random(14)
res = {
    "python": platform.python_version(),
    "platform": platform.platform(),
    "cpus": os.cpu_count(),
    "synthetic": True,
    "note": "random supplier sets, demand = 30% of total capacity, no supplier-count cap, a bound-exceeded result is reported as such (never as optimal), one solve each, default solver bounds (200k nodes, 20k grid units, 3M DP ops); timing varies with machine load",
    "timings_s": {},
}


def items(n: int, lot: float | None) -> list[solver.Item]:
    out = []
    for i in range(n):
        hi = rng.uniform(50, 400)
        lo = rng.choice([0.0, 5.0, 10.0]) or 1e-9
        out.append(solver.Item(f"s{i}", rng.uniform(5, 20), lo, hi, lot))
    return out


for n in (5, 10, 20, 40):
    for lot in (None, 10.0):
        its = items(n, lot)
        cap = sum(it.hi for it in its)
        demand = round(cap * 0.3 / 10) * 10 or 10  # feasible by construction without a supplier-count cap
        t0 = time.perf_counter()
        sol = solver.solve(its, demand, min_n=1, max_n=None)
        dt = time.perf_counter() - t0
        res["timings_s"][f"{n}_suppliers_{'lots' if lot else 'continuous'}"] = {
            "seconds": round(dt, 4),
            "status": sol.status,
            "method": sol.method,
            "exact": sol.exact,
            "nodes_or_ops": sol.nodes,
        }
out = Path(__file__).with_name("sourcing_benchmark_result.json")
out.write_text(json.dumps(res, indent=2) + "\n")
print(json.dumps(res, indent=2))
