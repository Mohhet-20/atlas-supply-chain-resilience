"""Benchmark of the analytics endpoints on SYNTHETIC suppliers (measures the software, not real supply chains).

Usage (from backend/): PYTHONPATH=. python ../scripts/analytics_benchmark.py [out.json]
"""

from __future__ import annotations

import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import Settings
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from app.main import create_app
from tests.quality_helpers import TABLE, h

A = "/api/v1/analytics"
ENDPOINTS = [
    "overview",
    "suppliers?limit=20",
    "inventory",
    "logistics",
    "network",
    "disruption",
    "trends",
    "alerts",
    "activity",
]


def run(n: int, ttl: float) -> dict:
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d, 'b.db').as_posix()}"
        command.upgrade(alembic_config(url), "head")
        eng = create_db_engine(url)
        with Session(eng) as s:
            for i in range(n):
                s.execute(
                    text(
                        "insert into suppliers (supplier_code,name,country_or_region,category,status,provenance,name_normalized,version,created_at,updated_at) values (:c,:n,:r,'Cat','ACTIVE','SYNTHETIC_DEMO',:n,1,'2026-01-01','2026-01-01')"
                    ),
                    {"c": f"B{i}", "n": f"Bench {i}", "r": f"R{i % 20}"},
                )
            s.commit()
        cfg = Settings(
            _env_file=None,
            environment="test",
            database_url=url,
            data_dir=Path(d),
            log_level="WARNING",
            api_tokens=TABLE,
            risk_auto_reassess=False,
            analytics_cache_ttl_seconds=ttl,
        )
        out = {}
        with TestClient(create_app(cfg, eng)) as c:
            for ep in ENDPOINTS:
                t = []
                for _ in range(5):
                    a = time.perf_counter()
                    r = c.get(f"{A}/{ep}", headers=h("viewer"))
                    t.append((time.perf_counter() - a) * 1000)
                    assert r.status_code == 200, (ep, r.text)
                out[ep.split("?")[0]] = {"median_ms": round(statistics.median(t), 1), "max_ms": round(max(t), 1)}
        eng.dispose()
        return out


def main() -> None:
    res = [
        {"suppliers": n, "cache_ttl_s": ttl, "endpoints": run(n, ttl)} for n in (100, 1000, 5000) for ttl in (0.0, 60.0)
    ]
    for r in res:
        print(r["suppliers"], r["cache_ttl_s"], {k: v["median_ms"] for k, v in r["endpoints"].items()})
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(
            json.dumps(
                {"note": "SYNTHETIC data; one machine; median of 5 requests per endpoint", "results": res}, indent=2
            )
        )


if __name__ == "__main__":
    main()
