# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository owner (use GitHub's private vulnerability
reporting once the repository is published). Do not open a public issue. Include the affected version or commit,
steps to reproduce, and impact. No bug bounty is offered.

## Supported versions

ATLAS is pre-release. Only the latest commit on `main` receives fixes.

## Controls implemented

Engineering controls, not a certification. Details: [`docs/security/`](docs/security/).

- Passwords hashed with scrypt; login throttling; generic login errors; forced change for admin-created accounts.
- Opaque server-side sessions with idle and absolute expiry, revocation and session epochs; scoped API keys.
- Deny-by-default routes with a role/permission matrix; every operation's requirement is declared in OpenAPI.
- Organisation scoping at the query layer for all data: domain records, runs, alerting, audit, users and
  integrations. Cross-organisation reads answer 404. Cross-organisation references are refused, and response caches
  are keyed by organisation. See `docs/security/TENANT_ISOLATION.md`.
- A strict Content-Security-Policy on the web app (`script-src 'self'`, `style-src 'self'`, no inline or eval code,
  `frame-ancestors 'none'`), defined once in `frontend/csp.ts`. nginx serves it, and the browser test suite runs under
  it and fails on any violation.
- Request validation with unknown-field rejection; body, upload and row limits; per-credential rate limiting.
- SSRF-guarded outbound calls with host allow-lists; HMAC-signed inbound and outbound webhooks with replay window.
- Strict CORS, security headers and disabled API docs in staging/production; configuration validated at start-up.
- Hash-chained audit log with a verification endpoint; secrets, tokens and passwords are never logged or returned.
- Frontend: tokens only in memory/sessionStorage and the `Authorization` header (never URLs or localStorage); no
  `dangerouslySetInnerHTML`/`eval` (enforced by a test).

## Scans (2026-11-03)

`bandit -r app -ll`: 0 medium/high. `pip-audit -r requirements.txt`: no known vulnerabilities.
`npm audit --omit=dev`: 0. Dev-only tooling has 11 moderate advisories (eslint → minimatch).

## Known gaps

- No penetration test or external review.
- All organisations share one database; record ids are global, so totals can be inferred
  (`docs/security/TENANT_ISOLATION.md`, residual risks).
- The CSP served by nginx has never been checked in a running container. The same policy is verified in the browser
  suite through `vite preview`.
- SQLite file permissions are the operator's responsibility.
- No secret-manager integration beyond environment variables.
