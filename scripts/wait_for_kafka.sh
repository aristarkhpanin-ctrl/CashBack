#!/usr/bin/env bash
# =====================================================================
# scripts/wait_for_kafka.sh
#
# Waits for the Kafka broker to be ready and (optionally) for a given
# topic to exist.
#
# Usage:
#   ./scripts/wait_for_kafka.sh                   # broker + transactions.raw
#   ./scripts/wait_for_kafka.sh my.topic          # broker + my.topic
#   ./scripts/wait_for_kafka.sh '' --no-topic     # broker only
# =====================================================================
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$ROOT_DIR/.env" ]; then
    set -a; . "$ROOT_DIR/.env"; set +a
elif [ -f "$ROOT_DIR/.env.example" ]; then
    set -a; . "$ROOT_DIR/.env.example"; set +a
fi

CONTAINER="${KAFKA_CONTAINER:-cashback-kafka}"
TOPIC="${1:-${KAFKA_TOPIC_TRANSACTIONS:-transactions.raw}}"
SKIP_TOPIC="${2:-}"

log() { printf '\033[1;36m>>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

# ----- broker readiness ----------------------------------------------
log "waiting for Kafka broker ($CONTAINER)..."
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" \
            /opt/bitnami/kafka/bin/kafka-broker-api-versions.sh \
            --bootstrap-server localhost:9092 >/dev/null 2>&1; then
        log "broker is ready"
        break
    fi
    sleep 2
done

if ! docker exec "$CONTAINER" \
        /opt/bitnami/kafka/bin/kafka-broker-api-versions.sh \
        --bootstrap-server localhost:9092 >/dev/null 2>&1; then
    err "Kafka broker did not become ready in time"
    exit 1
fi

# ----- optional topic check ------------------------------------------
if [ "$SKIP_TOPIC" = "--no-topic" ] || [ -z "$TOPIC" ]; then
    exit 0
fi

log "waiting for topic '$TOPIC' to exist..."
for _ in $(seq 1 60); do
    if docker exec "$CONTAINER" \
            /opt/bitnami/kafka/bin/kafka-topics.sh \
            --bootstrap-server localhost:9092 --list 2>/dev/null \
            | grep -qx "$TOPIC"; then
        log "topic '$TOPIC' is present"
        exit 0
    fi
    sleep 2
done

err "topic '$TOPIC' did not appear in time"
exit 1
