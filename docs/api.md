# API

Base path `/api/v1`; 478 operations on 430 paths (generated OpenAPI, 2026-11-03). In development and test the
interactive reference is at `http://127.0.0.1:8000/docs` and the schema at `/openapi.json`; both are disabled in
staging/production. Each operation declares its bearer requirement and the role or permission it needs
(`x-required-access`). The API is versioned by path (`/api/v1`); breaking changes would get a new prefix.

## Authentication

```http
POST /api/v1/auth/login        {"username": "...", "password": "..."}   → {"token": "atl_s_...", "expires_at": ..., "user": {...}}
GET  /api/v1/suppliers         Authorization: Bearer atl_s_...
POST /api/v1/auth/logout
```

Credentials: user sessions (opaque, server-side, idle and absolute expiry), scoped API keys for service accounts,
and static service tokens from `ATLAS_API_TOKENS`. Tokens are only accepted in the `Authorization` header, never in
a URL. Public routes: `/health`, `/readiness`, `/auth/login`, password-reset request/confirm, and inbound webhooks
(authenticated by HMAC signature). In development with no users and no tokens, requests run as an audited
`dev-local` admin; that actor disappears as soon as the first user exists.

## Conventions

| Concern | Convention |
|---|---|
| Errors | `{"error": {"code": "...", "message": "...", "request_id": "..."}}`; 400/401/403/404/409/413/422/429/503. No stack traces or SQL in responses. A resource in another organisation answers exactly like a missing one (404). |
| Pagination | `?limit=&offset=` → `{"items": [...], "total": n, "limit": l, "offset": o}`; limits are capped per endpoint. |
| Validation | Request bodies reject unknown fields (`extra="forbid"`); size limits on bodies, uploads and rows. |
| Idempotency | Run-creating endpoints accept `Idempotency-Key` (8-64 chars); a repeat returns the original run. |
| Concurrency | Updates of versioned records (alerts, decision cases) take `expected_version`; a stale write gets 409. |
| Writes | `ATLAS_WRITE_API_ENABLED=false` turns every write into a 503 `writes_disabled` (default off in production until set). |
| Rate limits | Per credential per process (`ATLAS_API_RATE_LIMIT_PER_MINUTE`), 429 with `Retry-After`. Verified under load ([load test](performance/LOAD_TEST.md)). |
| Busy database | On SQLite, a write that waited longer than `ATLAS_SQLITE_BUSY_TIMEOUT_MS` for another writer answers 503 `database_busy` with `Retry-After: 1`. Retry it; nothing was written. |
| Request id | Every response carries `X-Request-ID`; logs carry the same id. |

## Main areas

`/registry` and `/suppliers` `/facilities` `/products` (master data) · `/ingestion` (CSV/JSON import) · `/graph`
and `/network` · `/quality` · `/risk` · `/scenarios` · `/impact` · `/inventory` · `/forecast` · `/leadtime` ·
`/modelops` · `/routing` · `/sourcing` · `/replenishment` · `/resilience` · `/decision` · `/analytics` ·
`/alerting` · `/auth` `/admin` · `/integrations` `/webhooks` `/exports`.
