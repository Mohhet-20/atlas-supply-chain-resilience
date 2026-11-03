# HTTP load test (2026-11-03)

The first load test of the running API. It found a concurrency defect, which was fixed; both runs are kept.

- **Script:** [`scripts/load_test.py`](../../scripts/load_test.py)
- **Raw results:** [`load_test_result.json`](load_test_result.json) (after the fix) and
  [`load_test_before_fix.json`](load_test_before_fix.json)

## Setup

| | |
|---|---|
| Machine | Windows 11, 22 logical CPUs, Python 3.11.9; client and server on the same machine |
| Server | `uvicorn`, **one process**, SQLite in WAL mode, busy timeout 5 s, started by `scripts/e2e_server.py` |
| Data | Synthetic demo world: 3 suppliers, 3 facilities, 2 products, inventory, 90 days of demand, risk results, 2 alerts |
| Clients | Threads, each with its own HTTP connection; 4 signed-in sessions shared round-robin |
| Rate limit | Disabled (`ATLAS_API_RATE_LIMIT_PER_MINUTE=0`) for the read and write phases; default for the rate-limit phase |
| Duration | 20 s per phase (15 s for the rate-limit phase) |

The read mix is weighted towards supplier lists, the dashboard summary and risk lists. It also covers the network
summary, the analytics overview, alerts, the relationship list, registry search and inventory analyses.

## Results after the fix

### Reads

| Concurrent clients | Throughput | p50 | p95 | p99 | Errors |
|---:|---:|---:|---:|---:|---:|
| 1 | 149 req/s | 5.2 ms | 20.1 ms | 28.3 ms | 0 of 2,980 |
| 4 | 138 req/s | 23.4 ms | 74.3 ms | 109.4 ms | 0 of 2,757 |
| 16 | 125 req/s | 91.3 ms | 342.2 ms | 504.1 ms | 0 of 2,515 |
| 32 | 105 req/s | 218.7 ms | 590.9 ms | 843.3 ms | 0 of 2,116 |

Throughput peaks with one client and falls slowly as clients are added. Latency grows roughly linearly with
concurrency. That is the shape of one Python process doing CPU-bound work: adding clients only adds queueing.
Capacity therefore scales with processes or hosts, not threads. Multi-process serving is untested, since the
deployment files that run several workers have never been run.

### Writes (synchronous supplier risk assessments)

| Concurrent clients | Completed | Throughput | p50 | p95 | Other responses |
|---:|---:|---:|---:|---:|---|
| 1 | 340 | 17.0 req/s | 56.9 ms | 61.7 ms | none |
| 4 | 318 | 21.5 req/s | 57.7 ms | 968.6 ms | 119 × 409 |
| 8 | 316 | 22.1 req/s | 59.6 ms | 2,036 ms | 133 × 409, 3 × 503 |

SQLite allows one writer at a time, so write throughput stops at about 22 assessments per second however many
clients there are; the extra clients wait. There are only three suppliers, so concurrent clients often ask to assess
the same one. The 409s are the "an assessment of this supplier is already running" guard. The three 503s are writers
that waited longer than the 5-second busy timeout; they carry `Retry-After: 1`.

### Rate limiting (default configuration)

With one credential and 8 clients: **1,199 requests allowed, then 20,603 refused with 429** within 15 s.
The per-credential limit is 1,200 per minute per process. The 429 path is cheap (p50 3.3 ms), so the limiter itself
is not a bottleneck.

## The defect the first run found

The first run was made before the fix:

| Phase | Before | After |
|---|---|---|
| Reads, 16 clients | 5 × 500 and 4 dropped connections out of 2,323 | 0 errors |
| Writes, 4 clients | 171 OK, 470 × 500, 138 dropped connections | 318 OK, 0 × 500, 0 dropped |
| Writes, 8 clients | 98 OK, 632 × 500, 421 dropped connections | 316 OK, 0 × 500, 3 × 503, 0 dropped |
| Server log | 1,107 "Exception in ASGI application" | 0 |

**Cause.** A SQLite transaction that reads first and writes later is refused at once ("database is locked") when
another connection wrote in between, and the busy timeout does not apply to that case. Two kinds of transaction did
this:
- Every authenticated request's session-activity touch, reads included.
- Every write request's handler.

The lock error from the authentication dependency reached the catch-all error handler. Starlette re-raises from that
handler, so some clients saw the connection dropped instead of a response.

**Fix:**
1. Requests that can change data, and background runs, start their transaction with `BEGIN IMMEDIATE`
   (`app/db/session.py: immediate_bind`), so writers queue behind the busy timeout instead of failing.
2. The session-activity touch is best-effort, inside a savepoint.
3. A writer that still waits too long gets `503 database_busy` with `Retry-After: 1`, never a 500.

**Regression tests:** `backend/tests/test_concurrency_sqlite.py` holds a real write lock from a second connection. One
test checks that a writer waits and succeeds. The other checks that a writer that waits too long gets a 503 while a
read that needs the activity write still succeeds. The 503 test fails (500) without the fix.

## What this does not show

- Behaviour on other hardware, over a network, or with more than one server process.
- PostgreSQL, which is untested. A multi-writer database is the real answer to the write ceiling above.
- Long soak runs (memory growth, log volume) and large datasets: the demo world is tiny. The single-process
  benchmarks in [`PERFORMANCE_BASELINE.md`](PERFORMANCE_BASELINE.md) cover larger synthetic datasets for individual
  engines.
