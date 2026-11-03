# Contributing

1. Keep route handlers thin; calculations live in the domain packages under `backend/app/`. ORM models and API
   schemas stay separate.
2. Schema changes ship as an Alembic migration (`alembic revision --autogenerate`, then review it). The app never
   calls `create_all()`.
3. Every change comes with tests. Run before pushing:
   - backend: `python -m pytest -q`, `ruff check .`, `ruff format --check .`
   - frontend: `npm run lint && npm run typecheck && npm test && npm run build`
   - browser: `npx playwright test` (see [`docs/testing.md`](docs/testing.md))
4. Domain models inherit `TenantOwned`, so their queries are organisation-scoped automatically. Use ORM statements,
   not raw SQL, for application data.
5. Provenance is part of the data: never present synthetic values as real ones.
6. Never commit `.env`, databases, `node_modules`, virtual environments or build output.
