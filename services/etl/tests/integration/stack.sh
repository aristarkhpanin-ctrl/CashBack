#!/usr/bin/env bash
# Throw-away infrastructure for services/etl/tests/integration:
# Kafka (KRaft) + Confluent Schema Registry + ClickHouse on one Docker network.
#
#   tests/integration/stack.sh up     # start and wait until all three are ready
#   tests/integration/stack.sh env    # print the ETL_IT_* variables for pytest
#   tests/integration/stack.sh down   # remove containers and network
#
# Same images as docker-compose.yml. Host ports and the container prefix come
# from the environment, so parallel stacks do not collide:
#   ETL_IT_PREFIX (etl-it), ETL_IT_CLICKHOUSE_PORT (18450),
#   ETL_IT_SR_PORT (18451), ETL_IT_KAFKA_PORT (18452),
#   ETL_IT_CH_NOFILE (262144; a sandboxed dockerd may need e.g. 20000).
set -euo pipefail

PREFIX="${ETL_IT_PREFIX:-etl-it}"
CH_PORT="${ETL_IT_CLICKHOUSE_PORT:-18450}"
SR_PORT="${ETL_IT_SR_PORT:-18451}"
KAFKA_PORT="${ETL_IT_KAFKA_PORT:-18452}"
CH_NOFILE="${ETL_IT_CH_NOFILE:-262144}"
NET="${PREFIX}-net"
KAFKA="${PREFIX}-kafka"
SR="${PREFIX}-schema-registry"
CH="${PREFIX}-clickhouse"

wait_for() {  # wait_for <what> <seconds> <command...>
  local what=$1 deadline=$((SECONDS + $2)); shift 2
  until "$@" >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      echo "::error::${what} did not become ready" >&2
      docker logs --tail 80 "${what}" >&2 || true
      return 1
    fi
    sleep 2
  done
  echo "${what} ready"
}

up() {
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  docker run -d --name "$KAFKA" --network "$NET" -p "127.0.0.1:${KAFKA_PORT}:9094" \
    -e CLUSTER_ID=m0fPzfelTjSXE-sbwO0Wtw \
    -e KAFKA_NODE_ID=1 \
    -e KAFKA_PROCESS_ROLES=broker,controller \
    -e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
    -e KAFKA_LISTENERS=INTERNAL://:9092,CONTROLLER://:9093,EXTERNAL://:9094 \
    -e "KAFKA_ADVERTISED_LISTENERS=INTERNAL://${KAFKA}:9092,EXTERNAL://localhost:${KAFKA_PORT}" \
    -e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,EXTERNAL:PLAINTEXT \
    -e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
    -e KAFKA_INTER_BROKER_LISTENER_NAME=INTERNAL \
    -e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
    -e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 \
    -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 \
    apache/kafka:3.7.2 >/dev/null
  docker run -d --name "$CH" --network "$NET" -p "127.0.0.1:${CH_PORT}:8123" \
    --ulimit "nofile=${CH_NOFILE}:${CH_NOFILE}" \
    -e CLICKHOUSE_USER=cashback -e CLICKHOUSE_PASSWORD=cashback \
    -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
    clickhouse/clickhouse-server:24.3-alpine >/dev/null
  wait_for "$KAFKA" 120 docker exec "$KAFKA" \
    /opt/kafka/bin/kafka-broker-api-versions.sh --bootstrap-server localhost:9092
  docker run -d --name "$SR" --network "$NET" -p "127.0.0.1:${SR_PORT}:8081" \
    -e SCHEMA_REGISTRY_HOST_NAME=schema-registry \
    -e SCHEMA_REGISTRY_LISTENERS=http://0.0.0.0:8081 \
    -e "SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS=PLAINTEXT://${KAFKA}:9092" \
    confluentinc/cp-schema-registry:7.6.1 >/dev/null
  wait_for "$CH" 120 curl -fsS "http://127.0.0.1:${CH_PORT}/ping"
  wait_for "$SR" 180 curl -fsS "http://127.0.0.1:${SR_PORT}/subjects"
}

down() {
  docker rm -f -v "$SR" "$KAFKA" "$CH" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}

env_vars() {
  cat <<EOF
ETL_IT_CLICKHOUSE_PORT=${CH_PORT}
ETL_IT_KAFKA_BOOTSTRAP=localhost:${KAFKA_PORT}
ETL_IT_SCHEMA_REGISTRY_URL=http://127.0.0.1:${SR_PORT}
EOF
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  env) env_vars ;;
  *) echo "usage: $0 up|down|env" >&2; exit 2 ;;
esac
