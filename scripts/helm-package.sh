#!/usr/bin/env bash
# =====================================================================
# scripts/helm-package.sh
#
# Lints the cashback Helm chart and packages it as a versioned .tgz
# under `dist/` for upload to a Helm repo (Chartmuseum / GHCR / S3).
#
# Override the chart path or output directory via env:
#   CHART_DIR=helm/cashback OUT_DIR=dist bash scripts/helm-package.sh
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CHART_DIR="${CHART_DIR:-helm/cashback}"
OUT_DIR="${OUT_DIR:-dist}"
APP_VERSION="${APP_VERSION:-}"
CHART_VERSION="${CHART_VERSION:-}"

log() { printf '\033[1;36m>>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

if ! command -v helm >/dev/null 2>&1; then
    err "helm not installed (https://helm.sh/docs/intro/install/)"
    exit 1
fi
if [ ! -f "$CHART_DIR/Chart.yaml" ]; then
    err "$CHART_DIR/Chart.yaml not found"
    exit 1
fi

mkdir -p "$OUT_DIR"

log "lint $CHART_DIR"
helm lint "$CHART_DIR"

log "template $CHART_DIR (smoke render)"
helm template cashback "$CHART_DIR" >/dev/null

PKG_ARGS=("$CHART_DIR" --destination "$OUT_DIR")
[ -n "$APP_VERSION" ]   && PKG_ARGS+=(--app-version "$APP_VERSION")
[ -n "$CHART_VERSION" ] && PKG_ARGS+=(--version "$CHART_VERSION")

log "package → $OUT_DIR/"
helm package "${PKG_ARGS[@]}"

log "done"
ls -lh "$OUT_DIR"/cashback-*.tgz | tail -3
