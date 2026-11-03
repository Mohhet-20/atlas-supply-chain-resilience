"""Synthetic timing of the route engine (pure Python). Not a statement about real-data or production-scale performance."""

import json
import os
import platform
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.routing import algorithms  # noqa: E402
from app.routing.network import Lane  # noqa: E402

rng = random.Random(13)
res = {
    "python": platform.python_version(),
    "platform": platform.platform(),
    "cpus": os.cpu_count(),
    "synthetic": True,
    "note": "random layered graphs; one run each; timing varies with machine load",
    "timings_s": {},
}


def layered(layers: int, width: int, fan: int) -> tuple[dict[int, list[Lane]], int, int]:
    ids = [[l * width + i + 1 for i in range(width)] for l in range(layers)]
    src, dst = 0, layers * width + 1
    out: dict[int, list[Lane]] = {n: [] for row in ids for n in row}
    out[src], out[dst] = [], []
    lid = 0
    for a in ids[0]:
        lid += 1
        out[src].append(Lane(lid, f"L{lid}", src, a, "ROAD", duration_hours=rng.uniform(1, 10)))
    for l in range(layers - 1):
        for a in ids[l]:
            for b in rng.sample(ids[l + 1], min(fan, width)):
                lid += 1
                out[a].append(Lane(lid, f"L{lid}", a, b, "ROAD", duration_hours=rng.uniform(1, 10)))
    for a in ids[-1]:
        lid += 1
        out[a].append(Lane(lid, f"L{lid}", a, dst, "ROAD", duration_hours=rng.uniform(1, 10)))
    return out, src, dst


def weight(ln: Lane) -> float | None:
    return ln.duration_hours


for layers, width, fan in ((4, 5, 3), (6, 8, 3), (8, 12, 3), (10, 20, 3)):
    g, s, t = layered(layers, width, fan)
    nlanes = sum(len(v) for v in g.values())
    t0 = time.perf_counter()
    best = algorithms.dijkstra(g, s, t, weight)
    t_dij = time.perf_counter() - t0
    t0 = time.perf_counter()
    paths, stats = algorithms.enumerate_paths(g, s, t, layers + 1, budget=200_000)
    t_enum = time.perf_counter() - t0
    res["timings_s"][f"{layers}x{width}"] = {
        "lanes": nlanes,
        "dijkstra_s": round(t_dij, 5),
        "dijkstra_found": best is not None,
        "enumerate_s": round(t_enum, 4),
        "routes": len(paths),
        "expansions": stats.expansions,
        "truncated": stats.truncated,
    }

Path(__file__).with_name("routing_benchmark_result.json").write_text(json.dumps(res, indent=2))
print(json.dumps(res))
