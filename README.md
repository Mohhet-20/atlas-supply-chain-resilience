# ATLAS

Supply-chain resilience platform. ATLAS keeps one model of your suppliers, facilities, products and the
relationships between them, and uses it to answer the questions a planner actually asks:
- Which suppliers are risky, and why?
- What happens downstream if one of them goes down for three weeks?
- Where will we run out of stock?
- Which mitigation is worth it?

It is decision support. It never places orders. Every result records its inputs, method version and data coverage,
and is labelled as observed, assessed, predicted or simulated.

![Supplier risk with evidence](docs/screenshots/05-supplier-risk-evidence.png)

## What's in it

| Module | What it does |
|---|---|
| Registry & ingestion | CSV/JSON import with mapping profiles, 20 validation rules and lineage; duplicate detection and safe merges |
| Network graph | Typed supplier → facility → product relationships, bounded traversal, path search, integrity checks |
| Supplier risk | Rule-based score (`rm-1`) with evidence coverage and reliability: an indicator, not a probability |
| Disruption scenarios | Deterministic what-if simulation of outages, delays and capacity loss |
| Cascading impact | Which entities a disruption reaches through several tiers, bottlenecks and alternates |
| Inventory & stockout | Inventory position, days of cover, projected stockouts (missing data is never treated as zero) |
| Demand forecasting | Naive, seasonal, Holt-Winters, ARIMA-like and Croston models, chosen by rolling-origin validation |
| Lead time & delivery risk | Ridge lead-time model and logistic late-delivery risk with leakage-safe evaluation |
| Model governance | Model registry, metrics, leakage checks, coefficients and permutation importance |
| Optimisation | Routing, exact supplier allocation, (s,Q)/(s,S) inventory policies, multi-objective resilience trade-offs |
| Decisions | Decision cases comparing finished runs, with a review trail |
| Dashboards | Every figure documented: calculation, source, time window and missing-data rule |
| Alerts & incidents | Versioned rules, deduplicated alerts, acknowledgement, assignment and escalation |
| Identity & audit | Organisations, users, sessions, API keys, role-based access, hash-chained audit log |
| Integrations | Service accounts, signed webhooks, exports, a generic REST connector |

## Screenshots

| | |
|---|---|
| ![Overview](docs/screenshots/01-overview.png) Overview | ![Executive dashboard](docs/screenshots/02-executive-dashboard.png) Executive dashboard |
| ![Network](docs/screenshots/03-network.png) Network | ![Supplier risk](docs/screenshots/04-supplier-risk.png) Supplier risk |
| ![Scenario run](docs/screenshots/06-scenario-run.png) Scenario run | ![Cascading impact](docs/screenshots/07-cascading-impact.png) Cascading impact |
| ![Stockout analysis](docs/screenshots/08-inventory-stockout.png) Stockout analysis | ![Alerts](docs/screenshots/09-alerts.png) Alerts |

## Architecture

```mermaid
flowchart LR
  UI["React SPA"] -- "JSON + bearer token" --> MW
  subgraph API["FastAPI"]
    MW["Middleware: request id, security headers,<br/>body limit, rate limit, metrics"] --> R["/api/v1 routers"]
    R --> AUTH["Auth · RBAC · organisation scope · audit"] --> ENG["Domain engines: graph, risk, scenarios,<br/>impact, inventory, forecast, lead time,<br/>routing, sourcing, replenishment, resilience"]
  end
  W["Worker: alert tick, integration sync,<br/>webhook delivery, exports"]
  ENG --> DB[("SQLite or PostgreSQL")]
  W --> DB
```

A modular monolith: one API process, an optional worker, one database. Details and design trade-offs are in
[docs/architecture.md](docs/architecture.md).

**Stack:**
- **Backend:** Python 3.10/3.11, FastAPI, SQLAlchemy 2, Alembic, Pydantic 2. The models and solvers are plain Python
  (no numpy or scikit-learn).
- **Frontend:** React 19, TypeScript, React Router 7, Vite, Tailwind CSS 4.
- **Tests:** pytest, Vitest and Testing Library, Playwright with axe-core.

## Getting started

You need Python 3.10 or 3.11 and Node 22 or newer.

```bash
# backend
cd backend
python -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
alembic upgrade head                   # creates backend/var/atlas.db
python ../scripts/seed_demo.py         # optional: a small synthetic dataset
uvicorn app.main:create_app --factory --port 8000   # API docs at http://127.0.0.1:8000/docs

# frontend, in a second terminal
cd frontend
npm ci
npm run dev                            # http://localhost:5173
```

Until a user exists, local requests run as a development admin. To sign in properly, create an account:

```bash
cd backend
ATLAS_NEW_PASSWORD='choose-a-long-passphrase' python -m app.auth.cli create-user admin --platform-admin
# PowerShell: $env:ATLAS_NEW_PASSWORD='...'; python -m app.auth.cli create-user admin --platform-admin
```

Configuration is all `ATLAS_*` environment variables. See [.env.example](.env.example) and
[docs/configuration.md](docs/configuration.md). To use PostgreSQL, set
`ATLAS_DATABASE_URL=postgresql+psycopg://user:password@host:5432/atlas`.

## Demo

`scripts/demo_world.py` builds a small synthetic network through the real ingestion engine: three suppliers, three
facilities, two products, inventory, 90 days of demand and an inbound order. It then runs the risk, scenario,
impact, stockout, replenishment and alerting engines over that data.

To click through it:

```bash
python scripts/e2e_server.py           # builds the demo database and serves the API on :8765
cd frontend
VITE_API_BASE_URL=http://127.0.0.1:8765/api/v1 npm run build
npx vite preview --port 4180           # http://127.0.0.1:4180, sign in as e2e-admin / Synthetic-E2E-Pass-Phrase-41!
```

### Hosted demo (Render)

[`render.yaml`](render.yaml) deploys the same synthetic demo as two free Render services: the API
([`deploy/demo/Dockerfile`](deploy/demo/Dockerfile), started by [`scripts/demo_start.py`](scripts/demo_start.py)) and the
static frontend, which proxies `/api/*` to it so the browser sees one origin and the strict CSP holds. In Render choose
**New > Blueprint**, pick this repository and set one demo password (used by `ATLAS_DEMO_PASSWORD` and
`VITE_DEMO_PASSWORD`). The API runs in `staging` mode: strict configuration checks, no unauthenticated access, no
`/docs`. The demo account is an `editor`, so visitors can run scenarios but not administer. The database is rebuilt on
every start, so the demo resets itself; the free tier sleeps when idle and takes about a minute to wake.

A good path through the app:
1. **Overview.** The demo network.
2. **Supplier risk → DEMO Meridian Metals.** High score but low reliability, because the evidence is synthetic.
3. **Scenarios → DEMO Core supplier outage.** A 21-day outage.
4. **Cascading impact.** The six entities the outage reaches.
5. **Inventory.** A projected stockout at the distribution hub.
6. **Alerts.** Acknowledge one, then check the audit log.

## Tests

```bash
cd backend && python -m pytest -q                 # about 1,600 tests; add -n 8 with pytest-xdist
cd frontend && npm test                           # unit tests
cd frontend && npx playwright test                # browser tests and accessibility scans against the real API
```

The backend suite also runs on PostgreSQL (`ATLAS_TEST_POSTGRES_URL=...`). Setup, layers and current results are in
[docs/testing.md](docs/testing.md); a load test is in [docs/performance/LOAD_TEST.md](docs/performance/LOAD_TEST.md).

## Security

The main controls:
- **Accounts and access:** scrypt password hashing, server-side sessions, scoped API keys and a deny-by-default
  permission matrix.
- **Organisation isolation:** enforced at the query layer
  ([docs/security/TENANT_ISOLATION.md](docs/security/TENANT_ISOLATION.md)).
- **Web app and abuse limits:** a strict Content-Security-Policy and per-credential rate limits.
- **Webhooks:** signed, with an SSRF guard.
- **Audit:** an append-only, hash-chained audit log.

See [SECURITY.md](SECURITY.md) for reporting and known gaps.

## Limitations

- **Data:** all data in the repository is synthetic, and no model has been evaluated on real data.
- **Containers:** the hosted-demo start path is verified locally in staging mode; the Dockerfiles, Compose files and CI
  workflow haven't been built or run on a container engine yet.
- **No vendor connectors:** there are none for ERP, WMS or TMS systems; the REST connector is generic.

More in [docs/limitations.md](docs/limitations.md).

## License

[MIT](LICENSE)
