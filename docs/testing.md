# Testing

Five layers, all runnable locally.

| Layer | Tool | Location | What it proves |
|---|---|---|---|
| Backend unit + integration | pytest | `backend/tests/` | Calculations, solvers checked against brute force, leakage rules, constraints, migrations, API contracts, permissions, organisation isolation, the audit chain, the worker, backup and restore, SQLite write contention. Every test gets a fresh database migrated with Alembic (no `create_all`). |
| Same suite on PostgreSQL | pytest + PostgreSQL 16 | `ATLAS_TEST_POSTGRES_URL` | The same tests on PostgreSQL. Each test gets a database cloned from a migrated template. Tests about SQLite itself (files, PRAGMAs, triggers, write locks) are marked `sqlite_only` and skipped. |
| Frontend unit | Vitest + Testing Library (jsdom) | `frontend/src/test/` | Rendering, labelling of observed, predicted and simulated values, permission-aware UI, token handling, error states, and the Content-Security-Policy staying in step with `nginx.conf`. `fetch` is mocked here, so these do **not** prove the UI works with the real API. |
| Browser end-to-end | Playwright + Chrome | `frontend/e2e/release.spec.ts` | Real sign-in; sessions surviving reloads; bad credentials and forged tokens rejected; all 25 pages loading with no error state, console error or CSP violation under the production CSP; the demo decision workflow (risk evidence → scenario → impact → stockout → alert acknowledgement → audit-chain check). Nothing is mocked. |
| Accessibility | axe-core in Playwright | `frontend/e2e/accessibility.spec.ts` | Zero WCAG 2.1 A/AA violations from automated rules on the sign-in page and all 25 pages, with real data loaded. |
| Load | `scripts/load_test.py` | `docs/performance/LOAD_TEST.md` | Throughput and latency of a read mix at 1-32 clients, behaviour under concurrent writes, and the rate limiter engaging. It runs against a live server and is not part of the release gate. |

## Commands

```bash
# backend (from backend/, venv active)
python -m pytest -q                      # full suite; ~40 min on a laptop
python -m pytest tests/test_security_*.py -q
ruff check . && ruff format --check .

# backend on PostgreSQL (an administrative URL; databases atlas_tpl_* and atlas_t_* are created and dropped)
ATLAS_TEST_POSTGRES_URL=postgresql+psycopg://postgres:pw@127.0.0.1:5432/postgres python -m pytest -q
# no PostgreSQL installed? `pip install pgserver` and `pgserver.get_server(<dir>).get_uri()` start a local one

# frontend (from frontend/)
npm run lint && npm run typecheck && npm test && npm run build

# browser E2E + accessibility (from frontend/; needs the backend venv at backend/.venv and Chrome)
npx playwright test                      # starts scripts/e2e_server.py on :8765 and vite preview on :4180
PW_CHANNEL=chromium npx playwright test  # Playwright's Chromium instead (npx playwright install chromium)

# screenshots for the README (same servers, synthetic data)
SCREENSHOTS=1 npx playwright test e2e/screenshots.spec.ts      # PowerShell: $env:SCREENSHOTS=1; npx playwright test ...

# load test (repo root): a server without the rate limit, then the script
ATLAS_API_RATE_LIMIT_PER_MINUTE=0 python scripts/e2e_server.py --port 8766
python scripts/load_test.py --base-url http://127.0.0.1:8766/api/v1 --out docs/performance/load_test_result.json
```

`scripts/e2e_server.py` rebuilds `data/e2e/atlas_e2e.db` from `scripts/demo_world.py` on every run and creates a
throwaway administrator through the operator CLI. E2E runs are therefore reproducible and never touch another
database.

## Rules followed

- **No skipping for a green run.** No test is skipped to get a green run. Only two kinds of test are not part of
  the gate: the opt-in screenshot spec, and `sqlite_only` tests on PostgreSQL, which test SQLite itself and still run
  in the default suite.
- **Flakes fixed at the cause.** Flaky tests were fixed at their cause (lazy-loaded routes, clock resolution), not
  retried.
- **Regression tests are checked against the old code.** The cache-isolation, database-busy and accessibility fixes
  were each confirmed to fail without the fix.
- **Solvers.** Optimisation tests compare solvers with exhaustive enumeration on small instances.
- **ML.** Tests assert chronological, group-safe splits and preprocessing fitted on training rows only. A learned
  model is not selected unless it beats the baseline.

## Gaps

Not covered:
- containers and a hosted deployment;
- several API processes or hosts under load, and soak tests;
- audits with assistive technology;
- visual regression;
- real (non-synthetic) data.

CI is defined in `.github/workflows/ci.yml` (Python 3.10 and 3.11, PostgreSQL 16, browser E2E and accessibility, and
container builds) but has never run, because there is no remote.

## Latest results (2026-11-03, Windows 11, Python 3.11, PostgreSQL 16.2)

| Suite | Result |
|---|---|
| Backend on SQLite | 1,608 passed |
| Backend on PostgreSQL | 1,586 passed, 22 skipped (`sqlite_only`) |
| Frontend unit | 337 passed |
| Browser E2E + accessibility | 62 passed |
| Lint, types, build, `bandit`, `pip-audit`, `npm audit --omit=dev` | clean |
