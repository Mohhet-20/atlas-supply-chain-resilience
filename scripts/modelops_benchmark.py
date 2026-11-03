"""Synthetic timing of the model evaluation metric and drift library (pure Python). Not a statement about real-data performance."""

import json
import platform
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.modelops import drift, metrics  # noqa: E402

rng = random.Random(1)
out = {"python": platform.python_version(), "synthetic": True, "timings_s": {}}
for n in (1_000, 10_000, 20_000):
    y = [rng.gauss(10, 3) for _ in range(n)]
    p = [v + rng.gauss(0, 1) for v in y]
    lab = [1 if rng.random() < 0.3 else 0 for _ in range(n)]
    sc = [min(1, max(0, 0.3 + (0.3 if t else -0.1) * rng.random())) for t in lab]
    t0 = time.perf_counter()
    metrics.regression(p, y)
    metrics.classification(sc, lab, 0.5, probabilistic=True)
    metrics.reliability(sc, lab)
    drift.psi_numeric(y, p)
    drift.ks_statistic(y, p)
    out["timings_s"][str(n)] = round(time.perf_counter() - t0, 4)
Path(__file__).with_name("modelops_benchmark_result.json").write_text(json.dumps(out, indent=2))
print(json.dumps(out))
