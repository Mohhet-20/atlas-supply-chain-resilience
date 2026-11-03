"""HTTP load test against a running ATLAS API (synthetic demo data only).

Usage (repo root, backend venv active), against the server started by scripts/e2e_server.py:
    python scripts/load_test.py --base-url http://127.0.0.1:8765/api/v1 --out docs/performance/load_test_result.json

What it measures
- Read mix: a weighted set of the main read endpoints, each phase at a fixed number of concurrent clients for a fixed time.
  Every client signs in once; requests reuse that session. Reported per phase: throughput, error rate, latency p50/p95/p99/max.
- Write contention: concurrent synchronous risk assessments (each one writes a run, assessments, factors and evidence), to
  show how SQLite's single writer behaves under concurrent writes.
- Rate limiting: one credential sending faster than the per-credential limit must receive 429s.

Run the read and write phases with ATLAS_API_RATE_LIMIT_PER_MINUTE=0, otherwise the limiter, not the server, caps the
numbers; run the rate-limit phase (--phase ratelimit) against a server with the default limit. Client and server share
one machine, so the results describe that machine and are not a capacity claim for any deployment.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

USER = os.environ.get("ATLAS_E2E_USERNAME", "e2e-admin")
PASSWORD = os.environ.get("ATLAS_E2E_PASSWORD", "Synthetic-E2E-Pass-Phrase-41!")  # synthetic, throwaway database only

READ_MIX: list[tuple[int, str]] = [  # (weight, path)
    (4, "/suppliers?limit=25"),
    (3, "/dashboard/summary"),
    (2, "/network/summary"),
    (3, "/risk/suppliers"),
    (2, "/analytics/overview"),
    (2, "/alerting/alerts"),
    (2, "/graph/relationships"),
    (1, "/registry/search?q=DEMO"),
    (1, "/inventory/analyses"),
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * len(s)) - 1))]


def sign_in(base: str) -> str:
    r = httpx.post(f"{base}/auth/login", json={"username": USER, "password": PASSWORD}, timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def run_phase(base: str, tokens: list[str], clients: int, seconds: float, pick) -> dict:  # noqa: ANN001
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    lock = threading.Lock()
    stop = time.perf_counter() + seconds

    def worker(i: int) -> None:
        rng = random.Random(i)  # noqa: S311 - workload shuffling, not security
        headers = {"Authorization": f"Bearer {tokens[i % len(tokens)]}"}
        with httpx.Client(base_url=base, headers=headers, timeout=60) as c:
            while time.perf_counter() < stop:
                method, path, body = pick(rng)
                t0 = time.perf_counter()
                try:
                    r = c.request(method, path, json=body)
                    code = str(r.status_code)
                except httpx.HTTPError as e:
                    code = type(e).__name__
                dt = (time.perf_counter() - t0) * 1000
                with lock:
                    latencies.append(dt)
                    statuses[code] = statuses.get(code, 0) + 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(clients)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    total = sum(statuses.values())
    ok = sum(n for code, n in statuses.items() if code.startswith("2"))
    return {
        "clients": clients,
        "seconds": round(elapsed, 2),
        "requests": total,
        "throughput_rps": round(total / elapsed, 1),
        "error_rate": round(1 - ok / total, 4) if total else None,
        "statuses": dict(sorted(statuses.items())),
        "latency_ms": {
            "p50": round(pct(latencies, 50), 1),
            "p95": round(pct(latencies, 95), 1),
            "p99": round(pct(latencies, 99), 1),
            "max": round(max(latencies), 1) if latencies else None,
            "mean": round(statistics.fmean(latencies), 1) if latencies else None,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://127.0.0.1:8765/api/v1")
    ap.add_argument("--phase", choices=("reads", "writes", "ratelimit", "all"), default="all")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--clients", default="1,4,16,32")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    tokens = [sign_in(base) for _ in range(4)]  # a few sessions, as several signed-in users would have
    weighted = [p for w, p in READ_MIX for _ in range(w)]
    suppliers = httpx.get(f"{base}/suppliers?limit=25", headers={"Authorization": f"Bearer {tokens[0]}"}).json()
    supplier_ids = [s["id"] for s in suppliers["items"]]

    result: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": {"os": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()},
        "server": "uvicorn, 1 process, SQLite (WAL), synthetic demo world; client on the same machine",
        "read_mix": [{"weight": w, "path": p} for w, p in READ_MIX],
        "phases": {},
    }
    if args.phase in ("reads", "all"):
        result["phases"]["reads"] = []
        for n in (int(x) for x in args.clients.split(",")):
            r = run_phase(base, tokens, n, args.seconds, lambda rng: ("GET", rng.choice(weighted), None))
            result["phases"]["reads"].append(r)
            print(f"reads  clients={n:<3} {r['throughput_rps']:>7} rps  p50={r['latency_ms']['p50']}ms "
                  f"p95={r['latency_ms']['p95']}ms p99={r['latency_ms']['p99']}ms errors={r['error_rate']}")  # fmt: skip
    if args.phase in ("writes", "all"):
        result["phases"]["writes"] = []
        for n in (1, 4, 8):
            r = run_phase(base, tokens, n, args.seconds,
                          lambda rng: ("POST", f"/risk/suppliers/{rng.choice(supplier_ids)}/assess", None))  # fmt: skip
            result["phases"]["writes"].append(r)
            print(f"writes clients={n:<3} {r['throughput_rps']:>7} rps  p50={r['latency_ms']['p50']}ms "
                  f"p95={r['latency_ms']['p95']}ms statuses={r['statuses']}")  # fmt: skip
    if args.phase == "ratelimit":
        r = run_phase(base, tokens[:1], 8, args.seconds, lambda rng: ("GET", "/suppliers?limit=1", None))
        result["phases"]["ratelimit"] = r
        print(f"ratelimit {r['throughput_rps']} rps statuses={r['statuses']}")
    if args.out:
        existing = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
        existing_phases = existing.get("phases", {})
        result["phases"] = existing_phases | result["phases"]
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
