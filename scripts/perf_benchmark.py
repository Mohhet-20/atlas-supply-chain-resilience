"""Repeatable API benchmark on SYNTHETIC data (measures the software on this machine; not real supply chains).

Seeds a throw-away SQLite database at a stated scale, then for each endpoint records latency percentiles at two
concurrency levels, SQL statements per request (N+1 detector) and the query plan of hot queries.
In-process (Starlette TestClient): there is no network hop, TLS or proxy, so absolute numbers understate production latency.

Usage (from backend/): PYTHONPATH=. python ../scripts/perf_benchmark.py [--scale small|base] [--out result.json]
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from alembic import command
from app.core.config import Settings
from app.db.migrations_check import alembic_config
from app.db.session import create_db_engine
from app.main import create_app
from tests.quality_helpers import TABLE, h

SCALES = {
    "small": dict(suppliers=500, facilities=100, products=200, rels=1500, obs=5000, alerts=200),
    "base": dict(suppliers=5000, facilities=800, products=2000, rels=15000, obs=50000, alerts=2000),
}
API = "/api/v1"
ENDPOINTS = [
    "/health",
    "/readiness",
    "/dashboard/summary",
    "/suppliers?limit=50",
    "/suppliers?limit=50&offset=2000",
    "/suppliers?q=Synthetic%20Supplier%2042",
    "/facilities?limit=50",
    "/products?limit=50",
    "/network/summary",
    "/alerting/alerts?limit=50",
    "/alerting/alerts?status=open&limit=50",
    "/integrations/overview",
    "/analytics/overview",
    "/registry/audit?limit=50",
]
PLANS = {
    "suppliers ordered page": "SELECT * FROM suppliers ORDER BY name LIMIT 50 OFFSET 2000",
    "suppliers by status": "SELECT * FROM suppliers WHERE status='ACTIVE' ORDER BY id LIMIT 50",
    "observations recent": "SELECT * FROM observations ORDER BY observed_at DESC LIMIT 5",
    "relationships by supplier": "SELECT * FROM supply_relationships WHERE supplier_id=10",
    "alerts open by org": "SELECT * FROM alerts WHERE organization_id=1 AND status='open' ORDER BY id DESC LIMIT 50",
    "alerts by org newest": "SELECT * FROM alerts WHERE organization_id=1 ORDER BY last_seen_at DESC LIMIT 50",
    "audit by org newest": "SELECT * FROM audit_events WHERE organization_id=1 ORDER BY id DESC LIMIT 50",
}


def seed(eng, n: dict) -> None:
    now = "2026-01-01 00:00:00"
    with Session(eng) as s:
        s.execute(
            text(
                "insert into suppliers (supplier_code,name,country_or_region,category,status,provenance,name_normalized,version,created_at,updated_at) values (:c,:n,:r,:k,:st,'SYNTHETIC_DEMO',:n,1,:t,:t)"
            ),
            [
                dict(
                    c=f"BS{i}",
                    n=f"Synthetic Supplier {i}",
                    r=f"Region {i % 30}",
                    k=f"Cat {i % 12}",
                    st="ACTIVE" if i % 9 else "ON_HOLD",
                    t=now,
                )
                for i in range(n["suppliers"])
            ],
        )
        s.execute(
            text(
                "insert into facilities (facility_code,name,facility_type,country_or_region,status,provenance,location_status,name_normalized,version,created_at,updated_at) values (:c,:n,:ft,:r,'ACTIVE','SYNTHETIC_DEMO','UNKNOWN',:n,1,:t,:t)"
            ),
            [
                dict(
                    c=f"BF{i}",
                    n=f"Synthetic Plant {i}",
                    ft=("MANUFACTURING", "WAREHOUSE", "DISTRIBUTION", "CROSS_DOCK")[i % 4],
                    r=f"Region {i % 30}",
                    t=now,
                )
                for i in range(n["facilities"])
            ],
        )
        s.execute(
            text(
                "insert into products (product_code,name,status,provenance,name_normalized,version,created_at,updated_at) values (:c,:n,'ACTIVE','SYNTHETIC_DEMO',:n,1,:t,:t)"
            ),
            [dict(c=f"BP{i}", n=f"Synthetic Product {i}", t=now) for i in range(n["products"])],
        )
        F, S = n["facilities"], n["suppliers"]
        rels, seen = [], set()
        for i in range(n["rels"]):
            sup, fac = (i * 7) % S + 1, (i * 13 + i // S) % F + 1
            if (sup, fac) in seen:
                continue
            seen.add((sup, fac))
            rels.append(dict(c=f"BR{i}", sup=sup, fac=fac, t=now))
        s.execute(
            text(
                "insert into supply_relationships (relationship_code,relationship_type,supplier_id,target_facility_id,provenance,status,version,created_at,updated_at) values (:c,'SUPPLIES',:sup,:fac,'SYNTHETIC_DEMO','CONFIRMED',1,:t,:t)"
            ),
            rels,
        )
        s.execute(
            text(
                "insert into observations (observation_code,metric_name,value,unit,observed_at,source_label,provenance,supplier_id,created_at) values (:c,'on_time_rate',:v,'ratio',:o,'synthetic','SYNTHETIC_DEMO',:sup,:t)"
            ),
            [
                dict(
                    c=f"BO{i}",
                    v=0.5 + (i % 50) / 100,
                    o=f"2026-{1 + i % 9:02d}-{1 + i % 27:02d} 00:00:00",
                    sup=i % S + 1,
                    t=now,
                )
                for i in range(n["obs"])
            ],
        )
        s.execute(
            text(
                "insert into alerts (alert_uid,detector,source,dedup_key,correlation_key,title,summary,basis,severity,priority,explanation,condition,status,escalation_level,first_seen_at,last_seen_at,occurrence_count,notifications_throttled,reopened_count,created_at,updated_at,version,organization_id) values (:u,'metric_threshold','bench',:k,:k,'Synthetic alert','Synthetic','observed',:sev,2,'{}','{}',:st,0,:t,:t,1,0,0,:t,:t,1,1)"
            ),
            [
                dict(
                    u=f"AL-B{i}",
                    k=f"bench-{i}",
                    sev=("LOW", "MEDIUM", "HIGH", "CRITICAL")[i % 4],
                    st="open" if i % 3 else "resolved",
                    t=now,
                )
                for i in range(n["alerts"])
            ],
        )
        s.commit()


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summarize(ms: list[float], wall: float | None = None) -> dict:
    r = {
        "n": len(ms),
        "mean_ms": round(statistics.fmean(ms), 2),
        "p50_ms": round(pct(ms, 50), 2),
        "p95_ms": round(pct(ms, 95), 2),
        "p99_ms": round(pct(ms, 99), 2),
        "max_ms": round(max(ms), 2),
    }
    if wall:
        r["throughput_rps"] = round(len(ms) / wall, 1)
    return r


def run(scale: str, reps: int, conc: int) -> dict:
    n = SCALES[scale]
    with tempfile.TemporaryDirectory() as d:
        url = f"sqlite:///{Path(d, 'bench.db').as_posix()}"
        command.upgrade(alembic_config(url), "head")
        eng = create_db_engine(url)
        t0 = time.perf_counter()
        seed(eng, n)
        seed_s = time.perf_counter() - t0
        with Session(eng) as s:
            plans = {k: [r[3] for r in s.execute(text("EXPLAIN QUERY PLAN " + q))] for k, q in PLANS.items()}
        eng.dispose()
        settings = Settings(
            _env_file=None,
            environment="test",
            database_url=url,
            data_dir=Path(d),
            log_level="WARNING",
            api_tokens=TABLE,
        )
        app = create_app(settings)
        counter = {"n": 0}
        event.listen(
            app.state.engine, "before_cursor_execute", lambda *a, **k: counter.__setitem__("n", counter["n"] + 1)
        )
        out: dict = {}
        tracemalloc.start()
        with TestClient(app) as c:
            hd = h("admin")
            for ep in ENDPOINTS:
                counter["n"] = 0
                r = c.get(API + ep, headers=hd)
                stmts = counter["n"]
                body = len(r.content)
                if r.status_code >= 500:
                    out[ep] = {"error": r.status_code}
                    continue
                ms = []
                for _ in range(reps):
                    a = time.perf_counter()
                    c.get(API + ep, headers=hd)
                    ms.append((time.perf_counter() - a) * 1000)
                a = time.perf_counter()
                with ThreadPoolExecutor(conc) as ex:
                    cm = list(
                        ex.map(
                            lambda _i: (lambda t: (c.get(API + ep, headers=hd), (time.perf_counter() - t) * 1000)[1])(
                                time.perf_counter()
                            ),
                            range(reps * 2),
                        )
                    )
                wall = time.perf_counter() - a
                out[ep] = {
                    "status": r.status_code,
                    "sql_statements": stmts,
                    "response_bytes": body,
                    "serial": summarize(ms),
                    f"concurrent_{conc}": summarize(cm, wall),
                }
        cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return {
        "synthetic": True,
        "scale": scale,
        "dataset": n,
        "seed_seconds": round(seed_s, 2),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpus": __import__("os").cpu_count(),
        "database": "SQLite file (single writer), in-process TestClient",
        "reps": reps,
        "concurrency": conc,
        "python_heap_peak_mb": round(peak / 1e6, 1),
        "endpoints": out,
        "query_plans": plans,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", default="base", choices=SCALES)
    ap.add_argument("--reps", type=int, default=20)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out")
    a = ap.parse_args()
    res = run(a.scale, a.reps, a.concurrency)
    txt = json.dumps(res, indent=2)
    if a.out:
        Path(a.out).write_text(txt)
    print(txt)
    sys.exit(0)
