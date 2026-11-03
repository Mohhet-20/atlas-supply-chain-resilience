# Deployment

**Nothing has been deployed.** This page separates what was verified from what only exists as files. Detailed
procedures are in [`deployment/`](deployment/): start with `PRODUCTION_SETUP.md`, `ENVIRONMENT_CONFIGURATION.md`,
`BACKUP_RESTORE.md` and `ROLLBACK_PROCEDURE.md`.

## Verified

| Procedure | Where verified |
|---|---|
| Local development on Linux (Python 3.10, Node 22) | full test suite, before 2026-11-03 |
| Local development on Windows 11 PowerShell (Python 3.11, Node 24) | 2026-11-03: fresh DB → `alembic upgrade head` → seed → `uvicorn` → `/readiness` = ready |
| Production-like configuration validation | `scripts/validate_config.py --strict` (backend tests and CI definition) |
| Staging-rule API start + smoke test | `scripts/smoke_test.py` against a live server (Linux) |
| Online backup and restore-then-run | `scripts/backup_db.py`, `tests/test_deploy_restore.py` |
| Production build of the SPA served statically | `npm run build` + `vite preview` under the browser E2E suite |

## Not verified (no capability in the build environment)

Docker images and `docker-compose.yml` / `docker-compose.staging.yml`; the GitHub Actions workflow; TLS
termination and reverse proxy; PostgreSQL; any hosted environment; scheduled or off-site backups.

## Minimum production checklist

1. `ATLAS_ENVIRONMENT=production` (enables strict CORS, https origins, security headers, no dev admin, docs off).
2. `ATLAS_CORS_ALLOWED_ORIGINS` = the exact https origin of the SPA.
3. `ATLAS_INTEGRATIONS_MASTER_KEY` from a secret store (≥32 chars) if webhooks are used.
4. `python scripts/validate_config.py --env-file .env.production --strict` passes.
5. `alembic upgrade head` before starting the new version; take a backup first (`scripts/backup_db.py`).
6. Create the first administrator: `ATLAS_NEW_PASSWORD=... python -m app.auth.cli create-user <name> --platform-admin`.
7. Choose the database. SQLite (the default) is a file on a local disk; set `ATLAS_SQLITE_WAL=true` there. For
   PostgreSQL, set `ATLAS_DATABASE_URL=postgresql+psycopg://user:password@host:5432/atlas`; the migrations and test suite
   pass on PostgreSQL 16, but no deployment has run on it yet. Several organisations can share a deployment
   ([`security/TENANT_ISOLATION.md`](security/TENANT_ISOLATION.md)).
8. Run `scripts/smoke_test.py --base-url <url> --token <token>` after start.

## Processes

- API: `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000`
- Worker (when `ATLAS_EMBEDDED_WORKER=false`): `python -m app.worker`
- Health: `GET /api/v1/health` (liveness), `GET /api/v1/readiness` (database + schema at head).
