#!/usr/bin/env bash
# Repeatable deployment for the compose topology. Usage:
#   scripts/deploy.sh --env-file .env.production [--staging] [--check-only]
# Steps: validate config -> dependency checks -> build images -> migrate (explicit, one-shot) -> start -> wait for readiness -> smoke test.
# The docker steps are UNVERIFIED (no container runtime existed when this was written); --check-only is verified.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=""; STAGING=0; CHECK_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2;;
    --staging) STAGING=1; shift;;
    --check-only) CHECK_ONLY=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done
[ -n "$ENV_FILE" ] && [ -f "$ENV_FILE" ] || { echo "ERROR: --env-file <file> is required and must exist (copy .env.example)" >&2; exit 2; }

PY="${PYTHON:-python3}"
echo "== 1/6 validate configuration"
( cd backend && PYTHONPATH=. "$PY" ../scripts/validate_config.py --env-file "../$ENV_FILE" --strict ) \
  || { echo "Configuration invalid (warnings fail a deployment; fix them or deploy knowingly with validate_config.py without --strict)"; exit 1; }
[ "$CHECK_ONLY" = 1 ] && { echo "check-only: configuration valid, nothing deployed"; exit 0; }

echo "== 2/6 dependency checks"
command -v docker >/dev/null || { echo "ERROR: docker is required" >&2; exit 1; }
docker compose version >/dev/null || { echo "ERROR: docker compose v2 is required" >&2; exit 1; }

FILES=(-f docker-compose.yml); [ "$STAGING" = 1 ] && FILES+=(-f docker-compose.staging.yml)
DC=(docker compose "${FILES[@]}" --env-file "$ENV_FILE")

echo "== 3/6 back up the database before migrating (skip on first install)"
if "${DC[@]}" ps --status running --services 2>/dev/null | grep -q backend; then
  "${DC[@]}" exec -T backend python - <<'PYEOF'
import sqlite3, pathlib, time
src = pathlib.Path("/data/atlas.db")
if src.exists():
    dst = pathlib.Path(f"/data/pre-deploy-{int(time.time())}.db")
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(dst); s.backup(d); d.close(); s.close(); print("backup", dst)
PYEOF
fi

echo "== 4/6 build + migrate"
"${DC[@]}" build
"${DC[@]}" run --rm migrate

echo "== 5/6 start and wait for readiness"
"${DC[@]}" up -d backend worker web
for i in $(seq 1 30); do
  if "${DC[@]}" exec -T backend python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/readiness', timeout=2).status==200 else 1)" 2>/dev/null; then READY=1; break; fi
  sleep 2
done
[ "${READY:-0}" = 1 ] || { echo "ERROR: backend not ready after 60 s"; "${DC[@]}" logs --tail=50 backend; exit 1; }

echo "== 6/6 smoke test (public checks; pass SMOKE_TOKEN for authenticated checks)"
PORT="${ATLAS_WEB_PORT:-$([ "$STAGING" = 1 ] && echo 8081 || echo 8080)}"
"$PY" scripts/smoke_test.py --base-url "http://127.0.0.1:$PORT" ${SMOKE_TOKEN:+--token "$SMOKE_TOKEN"} ${SMOKE_OTHER_TOKEN:+--other-token "$SMOKE_OTHER_TOKEN"}
echo "Deployment finished. Inspect logs: ${DC[*]} logs --tail=100"
