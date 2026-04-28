#!/usr/bin/env bash
# =====================================================================
# scripts/init_db.sh
#
# Waits for Postgres + ClickHouse to be ready, then applies all
# OLTP and OLAP migrations.
#
#   * PostgreSQL — Alembic (db_migrations/)
#   * ClickHouse — plain SQL files (infrastructure/clickhouse/migrations/)
#
# Usage:  ./scripts/init_db.sh   or   make migrate
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# ----- env --------------------------------------------------------------
if [ -f .env ]; then
    set -a; . ./.env; set +a
elif [ -f .env.example ]; then
    set -a; . ./.env.example; set +a
fi

PG_HOST_LOCAL="${PG_HOST_LOCAL:-localhost}"
PG_PORT_LOCAL="${POSTGRES_PORT:-5432}"
PG_USER="${POSTGRES_USER:-cashback}"
PG_PASS="${POSTGRES_PASSWORD:-cashback}"
PG_DB="${POSTGRES_DB:-cashback}"

CH_HOST_LOCAL="${CH_HOST_LOCAL:-localhost}"
CH_HTTP_PORT="${CLICKHOUSE_HTTP_PORT:-8123}"
CH_USER="${CLICKHOUSE_USER:-cashback}"
CH_PASS="${CLICKHOUSE_PASSWORD:-cashback}"
CH_DB="${CLICKHOUSE_DB:-cashback}"

PG_CONTAINER="${PG_CONTAINER:-cashback-postgres}"
CH_CONTAINER="${CH_CONTAINER:-cashback-clickhouse}"

# Network used to attach throwaway containers (alembic) so they can
# talk to postgres via its service name.
NETWORK="$(docker network ls --format '{{.Name}}' | grep -E 'cashback[-_]net$' | head -n1 || true)"
if [ -z "$NETWORK" ]; then
    NETWORK="cashback_cashback-net"
fi

log() { printf '\033[1;36m>>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

# ----- waiters ----------------------------------------------------------
wait_for_postgres() {
    log "waiting for PostgreSQL ($PG_CONTAINER)..."
    for _ in $(seq 1 60); do
        if docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
            log "postgres is ready"
            return 0
        fi
        sleep 2
    done
    err "PostgreSQL did not become ready in time"
    exit 1
}

wait_for_clickhouse() {
    log "waiting for ClickHouse (${CH_HOST_LOCAL}:${CH_HTTP_PORT})..."
    for _ in $(seq 1 60); do
        if curl -fsS "http://${CH_HOST_LOCAL}:${CH_HTTP_PORT}/ping" 2>/dev/null | grep -q "Ok"; then
            log "clickhouse is ready"
            return 0
        fi
        sleep 2
    done
    err "ClickHouse did not become ready in time"
    exit 1
}

# ----- alembic ----------------------------------------------------------
run_alembic() {
    log "applying Alembic migrations (db_migrations/)..."

    if command -v alembic >/dev/null 2>&1; then
        (
            cd db_migrations
            POSTGRES_DSN="postgresql+psycopg2://${PG_USER}:${PG_PASS}@${PG_HOST_LOCAL}:${PG_PORT_LOCAL}/${PG_DB}" \
                alembic upgrade head
        )
        return
    fi

    log "alembic not installed locally — using disposable Python container"
    docker run --rm \
        --network "$NETWORK" \
        -v "$ROOT_DIR/db_migrations:/app" \
        -w /app \
        -e POSTGRES_DSN="postgresql+psycopg2://${PG_USER}:${PG_PASS}@postgres:5432/${PG_DB}" \
        python:3.11-slim \
        bash -lc "pip install --quiet --no-cache-dir alembic==1.13.2 SQLAlchemy==2.0.30 psycopg2-binary==2.9.9 && alembic upgrade head"
}

# ----- clickhouse SQL files --------------------------------------------
run_clickhouse_migrations() {
    log "applying ClickHouse migrations..."

    docker exec "$CH_CONTAINER" clickhouse-client \
        --user "$CH_USER" --password "$CH_PASS" \
        --query "CREATE DATABASE IF NOT EXISTS ${CH_DB}" >/dev/null

    shopt -s nullglob
    local files=( infrastructure/clickhouse/migrations/*.sql )
    shopt -u nullglob
    if [ ${#files[@]} -eq 0 ]; then
        log "no ClickHouse migrations to apply"
        return
    fi

    for f in $(printf '%s\n' "${files[@]}" | sort); do
        log "  applying $(basename "$f")"
        docker exec -i "$CH_CONTAINER" clickhouse-client \
            --user "$CH_USER" --password "$CH_PASS" \
            --database "$CH_DB" \
            --multiquery < "$f"
    done
}

# ----- main -------------------------------------------------------------
wait_for_postgres
wait_for_clickhouse
run_alembic
run_clickhouse_migrations
log "all migrations applied successfully"
