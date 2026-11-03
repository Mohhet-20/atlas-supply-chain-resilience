# Environment configuration

All configuration is `ATLAS_*` environment variables (or an untracked `.env`). `backend/app/core/config.py` is the single source;
`.env.example` lists the safe placeholders. Validate before every deploy:

```
cd backend && PYTHONPATH=. python ../scripts/validate_config.py --env-file ../.env.production --strict
```

## Environments
| | development | test | staging | production |
|---|---|---|---|---|
| Unauthenticated `dev-local` admin when no tokens/users | yes | yes | **no** (401/503) | **no** |
| API docs (`/docs`) | yes | yes | no | no |
| HSTS header | no | no | yes | yes |
| Strict validation (https CORS, no wildcard, no DEBUG, key length, security headers on) | no | no | **yes** | **yes** |
| Write API default | on | on | on | **off** until `ATLAS_WRITE_API_ENABLED=true` |
| Demo/synthetic seeding default | on | on | on (`ATLAS_DEMO_DATA_ENABLED`) | off |

Startup refuses to run (clear message, secret values never printed) when a production-like environment has: wildcard or non-https CORS (localhost excepted),
no CORS origins, `ATLAS_SECURITY_HEADERS_ENABLED=false`, `ATLAS_LOG_LEVEL=DEBUG`, or an `ATLAS_INTEGRATIONS_MASTER_KEY` shorter than 32 characters.

## Mandatory for a working production deployment
| Variable | Purpose |
|---|---|
| `ATLAS_ENVIRONMENT` | `production` or `staging` |
| `ATLAS_CORS_ALLOWED_ORIGINS` | public https origin(s), comma separated |
| `ATLAS_DATA_DIR` | persistent volume path (compose uses `/data`) |
| `ATLAS_INTEGRATIONS_MASTER_KEY` | >= 32 random chars; required to create webhooks (fails closed without it); losing it invalidates derived secrets |
| first administrator | `python -m app.auth.cli create-user <name> --platform-admin` (password prompt or `ATLAS_NEW_PASSWORD`; never argv) |

## Common optional settings
`ATLAS_DATABASE_URL` (default SQLite in the data dir; other engines untested), `ATLAS_WRITE_API_ENABLED`, `ATLAS_API_RATE_LIMIT_PER_MINUTE` (1200; 0 = off),
`ATLAS_ALERTING_TICK_SECONDS` (worker interval; 0 disables background work), `ATLAS_EMBEDDED_WORKER` (false when `app.worker` runs), `ATLAS_CACHE_*`,
`ATLAS_SQLITE_BUSY_TIMEOUT_MS`, `ATLAS_SQLITE_WAL` (local disks only), `ATLAS_DB_POOL_*` (non-SQLite), `ATLAS_JOB_LEASE_SECONDS`, `ATLAS_SYNC_MAX_SECONDS`,
`ATLAS_CIRCUIT_*`, `ATLAS_MAX_REQUEST_BYTES`, `ATLAS_MAX_UPLOAD_BYTES`, `ATLAS_EXPORT_*`, `ATLAS_LOG_LEVEL`. Credentials for external providers are references (`env:ATLAS_CRED_NAME`), never values in the database.

## Secret handling
* No secret is in the repository (pattern scan clean); `.env`, `.env.*` and keys are git-ignored.
* Frontend: only `VITE_API_BASE_URL` is read at build time and it is public by design. Never add a secret to a `VITE_` variable.
* CI uses throwaway synthetic values; real deployment credentials are not stored in workflows.
* Rotation: see PRODUCTION_OPERATIONS.md.
