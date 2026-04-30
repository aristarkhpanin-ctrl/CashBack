#!/usr/bin/env bash
# =====================================================================
# scripts/run_load_test.sh
#
# Runs the Locust scenario in headless mode and writes an HTML report
# to ``test-reports/load-<timestamp>.html``.
#
# Defaults: 1000 simulated users, 5-minute window. Override via env:
#
#   LOAD_USERS=1500 LOAD_DURATION=10m bash scripts/run_load_test.sh
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

USERS="${LOAD_USERS:-1000}"
SPAWN_RATE="${LOAD_SPAWN_RATE:-100}"
DURATION="${LOAD_DURATION:-5m}"
HOST="${LOAD_HOST:-http://localhost}"
RECO_BASE="${LOAD_RECO_BASE:-http://localhost:8001}"
MOBILE_BASE="${LOAD_MOBILE_BASE:-http://localhost:8003}"
CAMPAIGN_BASE="${LOAD_CAMPAIGN_BASE:-http://localhost:8002}"

REPORTS_DIR="$ROOT_DIR/test-reports"
mkdir -p "$REPORTS_DIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
HTML="$REPORTS_DIR/load-${TS}.html"
CSV_PREFIX="$REPORTS_DIR/load-${TS}"

log() { printf '\033[1;36m>>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

if ! command -v locust >/dev/null 2>&1; then
    err "locust not installed. Install with: pip install locust"
    exit 1
fi

log "load test: users=$USERS spawn-rate=$SPAWN_RATE duration=$DURATION"
log "writing HTML → $HTML"

locust -f tests/load/locustfile.py \
    --headless \
    --users "$USERS" \
    --spawn-rate "$SPAWN_RATE" \
    --run-time "$DURATION" \
    --host "$HOST" \
    --reco-base "$RECO_BASE" \
    --mobile-base "$MOBILE_BASE" \
    --campaign-base "$CAMPAIGN_BASE" \
    --html "$HTML" \
    --csv "$CSV_PREFIX" \
    --only-summary \
    --print-stats

log "done — open $HTML"
