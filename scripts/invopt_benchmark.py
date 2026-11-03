"""Reproducible timing of the replenishment engine on SYNTHETIC data (software verification only).

Run from ``backend/``:  python ../scripts/invopt_benchmark.py [--seed 7] [--out FILE]
It builds a throw-away migrated SQLite database, loads synthetic inventory and 120 days of demand for one
product-facility pair, then times data loading and engine.compute for several simulation sizes.
Timings are wall-clock on the machine that runs it and vary with load; the counts and the chosen policy
are deterministic for a given seed. It says nothing about real data or production-scale performance.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")
from fastapi.testclient import TestClient  # noqa: E402

from alembic import command  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.db.migrations_check import alembic_config  # noqa: E402
from app.db.session import create_db_engine  # noqa: E402
from app.invopt import data, engine  # noqa: E402
from app.invopt.config import Limits  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.invopt_helpers import request, setup  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(Path(__file__).with_name("invopt_benchmark_result.json")))
    a = ap.parse_args()
    lim = Limits()
    res: dict = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": os.cpu_count(),
        "synthetic": True,
        "seed": a.seed,
        "note": "one product-facility pair, 120 days of synthetic demand (mean 10, sd 3), lead time 5 +/- 1 days, default Limits, one run per row, "
        "wall-clock seconds on a shared machine; engine.compute is a pure function of the snapshot, so repeated runs return identical policies",
        "rows": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp, 'bench.db').as_posix()}"
        command.upgrade(alembic_config(url), "head")
        eng = create_db_engine(url)
        settings = Settings(
            _env_file=None, environment="test", database_url=url, data_dir=Path(tmp), log_level="WARNING"
        )
        with TestClient(create_app(settings, eng)) as client:
            w = setup(client, seed=a.seed)
            for reps, days, kw in (
                (30, 90, {}),
                (100, 90, {}),
                (200, 180, {}),
                (100, 90, {"service_type": "FILL_RATE", "service_target": 0.98}),
                (100, 90, {"policy_type": "S_Q"}),
                (100, 90, {"policy_type": "R_S", "review_period_days": 7}),
            ):
                req = request(w, replications=reps, simulation_days=days, seed=a.seed, **kw)
                t0 = time.perf_counter()
                with client.app.state.session_factory() as s:
                    snap = data.load(s, req, datetime.now(timezone.utc), lim)
                t1 = time.perf_counter()
                out = engine.compute(snap, req, lim)
                t2 = time.perf_counter()
                ev = out.get("evaluation") or {}
                res["rows"].append(
                    {
                        "replications_requested": reps,
                        "days": days,
                        "request_extras": kw,
                        "replications_used": (ev.get("budget") or {}).get("replications"),
                        "candidates": (ev.get("budget") or {}).get("candidates"),
                        "load_seconds": round(t1 - t0, 3),
                        "compute_seconds": round(t2 - t1, 3),
                        "result_status": out.get("result_status"),
                        "selected": ev.get("selected"),
                        "budget_note": (ev.get("budget") or {}).get("note"),
                    }
                )
        eng.dispose()
    Path(a.out).write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
