#!/usr/bin/env bash
# =====================================================================
# scripts/generate-api-types.sh
#
# Pulls /openapi.json from each FastAPI service and pipes it through
# `openapi-typescript` to regenerate strongly-typed TS clients in
# `frontend/src/shared/api/generated/`.
#
# Run after `make up` (the services need to be live):
#   bash scripts/generate-api-types.sh
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="$ROOT_DIR/frontend/src/shared/api/generated"
mkdir -p "$OUT_DIR"

REC_HOST="${RECOMMENDATION_API_HOST:-http://localhost:8001}"
CMP_HOST="${CAMPAIGN_API_HOST:-http://localhost:8002}"
MOB_HOST="${MOBILE_API_HOST:-http://localhost:8003}"

log() { printf '\033[1;36m>>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

# Use the locally-installed `openapi-typescript` if available, otherwise
# fall back to npx (downloads on demand).
if command -v openapi-typescript >/dev/null 2>&1; then
    GEN="openapi-typescript"
else
    GEN="npx --yes openapi-typescript@7"
fi

generate() {
    local label="$1" host="$2" out="$3"
    local url="${host%/}/openapi.json"
    log "fetching $label schema → $url"
    if ! curl -fsS -o "$out.tmp" "$url"; then
        err "could not reach $url — is the service running?"
        rm -f "$out.tmp"
        return 1
    fi
    log "generating $label types → $out"
    $GEN "$out.tmp" --output "$out"
    rm -f "$out.tmp"
}

generate "Recommendation API" "$REC_HOST" "$OUT_DIR/recommendation.ts"
generate "Campaign Manager"   "$CMP_HOST" "$OUT_DIR/campaign.ts"
generate "Mobile BFF"         "$MOB_HOST" "$OUT_DIR/mobile.ts"

log "done"
