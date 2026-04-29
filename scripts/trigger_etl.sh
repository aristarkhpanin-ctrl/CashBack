#!/usr/bin/env bash
# =====================================================================
# scripts/trigger_etl.sh
#
# Triggers an Airflow DAG run via the REST API.
#
# Usage:
#   ./scripts/trigger_etl.sh                       # cashback_daily_etl
#   ./scripts/trigger_etl.sh ml_retrain_pipeline
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
    set -a; . "$ROOT_DIR/.env"; set +a
elif [ -f "$ROOT_DIR/.env.example" ]; then
    set -a; . "$ROOT_DIR/.env.example"; set +a
fi

DAG_ID="${1:-cashback_daily_etl}"
USER="${AIRFLOW_ADMIN_USERNAME:-admin}"
PASS="${AIRFLOW_ADMIN_PASSWORD:-admin}"
HOST="${AIRFLOW_HOST:-http://localhost:8080}"
RUN_ID="manual__$(date -u +%Y%m%dT%H%M%SZ)"

log() { printf '\033[1;36m>>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

log "waiting for Airflow API at ${HOST} ..."
ready=0
for _ in $(seq 1 60); do
    if curl -fsS -u "${USER}:${PASS}" "${HOST}/api/v1/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    err "Airflow API not reachable at ${HOST} (check 'docker compose ps')"
    exit 1
fi
log "Airflow API is up"

log "triggering ${DAG_ID} (run_id=${RUN_ID}) ..."
http_code=$(curl -sS -o /tmp/airflow_trigger.json -w '%{http_code}' \
    -u "${USER}:${PASS}" \
    -H "Content-Type: application/json" \
    -X POST "${HOST}/api/v1/dags/${DAG_ID}/dagRuns" \
    -d "{\"dag_run_id\":\"${RUN_ID}\"}")

if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
    log "DAG run accepted (HTTP $http_code)"
    cat /tmp/airflow_trigger.json
    echo
    log "tail logs in Airflow UI: ${HOST}/dags/${DAG_ID}/grid"
else
    err "trigger failed (HTTP $http_code):"
    cat /tmp/airflow_trigger.json >&2
    echo >&2
    exit 1
fi
