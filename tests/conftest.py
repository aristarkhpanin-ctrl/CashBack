"""Shared pytest fixtures — chapter 3.3, listing 3.15.

Provides session-scoped testcontainers for Postgres, Redis, ClickHouse
and Kafka (the same images as ``docker-compose.yml``). Without Docker or
``testcontainers`` the fixtures skip locally, so the test files are still
collectible; under CI (``CI=true``, set by GitHub Actions) they fail
instead — a CI job whose integration tests were all skipped verified
nothing and must not be green.

Usage in a test module::

    @pytest.mark.integration
    async def test_something(postgres_container, redis_container):
        dsn = postgres_container.get_connection_url()
        ...
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, NoReturn

import pytest

# ---------------------------------------------------------------------------
# Path patching — make per-service `app` packages importable from repo-level
# tests (each service is a self-contained Python project).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _add_service_path(service: str) -> None:
    p = _REPO_ROOT / "services" / service
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


# NOTE: every service ships its own self-contained ``app`` package, so we
# can't blindly insert all of them onto sys.path (Python would resolve
# ``import app`` to whichever path comes first and unrelated submodules
# would fail). Instead, each integration test module puts its service's
# directory first on ``sys.path`` at the top of the file (see
# :func:`use_service` below), and ``make test-integration`` runs every
# module in its own pytest process.


# ---------------------------------------------------------------------------
# Session-scoped containers
# ---------------------------------------------------------------------------
# Same images as docker-compose.yml (Bitnami images are gone from Docker Hub;
# Kafka is apache/kafka in KRaft mode there too).
POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7.2-alpine"
CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:24.3-alpine"
KAFKA_IMAGE = "apache/kafka:3.7.2"
SCHEMA_REGISTRY_IMAGE = "confluentinc/cp-schema-registry:7.6.1"
CLICKHOUSE_MIGRATIONS = _REPO_ROOT / "infrastructure" / "clickhouse" / "migrations"


def _infra_unavailable(reason: str) -> NoReturn:
    """Skip locally, fail under CI: integration tests must really run there."""
    if os.getenv("CI", "").strip().lower() in {"1", "true", "yes"}:
        pytest.fail(f"{reason} — integration tests must not be skipped in CI",
                    pytrace=False)
    pytest.skip(reason)


def _require(module: str) -> Any:
    """``importorskip`` that fails instead of skipping under CI."""
    try:
        return importlib.import_module(module)
    except ImportError:
        _infra_unavailable(f"{module} is not installed")


def _tc(module: str, name: str) -> Any:
    """Import a testcontainers class (4.15+ moved the modules under ``community``)."""
    _require("testcontainers")
    for path in (f"testcontainers.community.{module}", f"testcontainers.{module}"):
        try:
            return getattr(importlib.import_module(path), name)
        except ImportError:
            continue
    _infra_unavailable(f"testcontainers.{module} is not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def _docker_available() -> bool:
    """Detect whether Docker is reachable. Skips heavy fixtures if not."""
    try:
        import docker  # type: ignore[import]
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def postgres_container(_docker_available):
    if not _docker_available:
        _infra_unavailable("Docker is not available — skipping testcontainers fixture")
    PostgresContainer = _tc("postgres", "PostgresContainer")

    with PostgresContainer(POSTGRES_IMAGE) as pg:
        yield pg


@pytest.fixture(scope="session")
def redis_container(_docker_available):
    if not _docker_available:
        _infra_unavailable("Docker is not available — skipping testcontainers fixture")
    RedisContainer = _tc("redis", "RedisContainer")

    with RedisContainer(REDIS_IMAGE) as r:
        yield r


@pytest.fixture(scope="session")
def clickhouse_container(_docker_available):
    """ClickHouse with the same user / database as docker-compose.yml."""
    if not _docker_available:
        _infra_unavailable("Docker is not available — skipping testcontainers fixture")
    ClickHouseContainer = _tc("clickhouse", "ClickHouseContainer")
    with ClickHouseContainer(CLICKHOUSE_IMAGE, username="cashback",
                             password="cashback", dbname="cashback") as ch:
        yield ch


@pytest.fixture(scope="session")
def kafka_container(_docker_available):
    """Single-node KRaft broker, configured like the compose ``kafka`` service.

    The broker joins its own docker network as ``kafka`` (INTERNAL listener
    ``kafka:9092`` — what Schema Registry connects to, as in compose). The
    EXTERNAL listener is published on a fixed free host port, because the
    broker has to advertise the address host-side clients connect to.
    """
    if not _docker_available:
        _infra_unavailable("Docker is not available — skipping testcontainers fixture")
    _require("testcontainers")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    port = _free_port()
    with Network() as network:
        container = (
            DockerContainer(KAFKA_IMAGE)
            .with_network(network)
            .with_network_aliases("kafka")
            .with_bind_ports(9094, port)
            .with_env("KAFKA_NODE_ID", "1")
            .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
            .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
            .with_env("KAFKA_LISTENERS",
                      "INTERNAL://:9092,CONTROLLER://:9093,EXTERNAL://:9094")
            .with_env("KAFKA_ADVERTISED_LISTENERS",
                      f"INTERNAL://kafka:9092,EXTERNAL://127.0.0.1:{port}")
            .with_env("KAFKA_LISTENER_SECURITY_PROTOCOL_MAP",
                      "INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,EXTERNAL:PLAINTEXT")
            .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
            .with_env("KAFKA_INTER_BROKER_LISTENER_NAME", "INTERNAL")
            .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
            .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
            .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
            .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
            .waiting_for(LogMessageWaitStrategy(r"Kafka Server started")
                         .with_startup_timeout(120))
        )
        with container:
            container.bootstrap_server = f"127.0.0.1:{port}"
            container.network = network
            yield container


@pytest.fixture(scope="session")
def schema_registry_container(kafka_container):
    """Confluent Schema Registry on the broker's network (compose ``schema-registry``)."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import HttpWaitStrategy

    container = (
        DockerContainer(SCHEMA_REGISTRY_IMAGE)
        .with_network(kafka_container.network)
        .with_exposed_ports(8081)
        .with_env("SCHEMA_REGISTRY_HOST_NAME", "schema-registry")
        .with_env("SCHEMA_REGISTRY_LISTENERS", "http://0.0.0.0:8081")
        .with_env("SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS",
                  "PLAINTEXT://kafka:9092")
        .with_env("SCHEMA_REGISTRY_KAFKASTORE_TOPIC", "_schemas")
        .waiting_for(HttpWaitStrategy(8081, "/subjects")
                     .with_startup_timeout(180))
    )
    with container:
        yield container


# ---------------------------------------------------------------------------
# Connection strings derived from the containers above
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def postgres_dsn(postgres_container) -> str:
    return postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg",
    )


@pytest.fixture(scope="session")
def migrated_postgres_dsn(postgres_container) -> str:
    """asyncpg DSN of a database migrated to alembic head (``db_migrations/``).

    Tests run against the real schema, not a hand-written copy of the DDL
    that silently drifts away from the migrations.
    """
    _require("alembic")
    _require("psycopg2")  # db_migrations/migrations/env.py connects via psycopg2
    pg = postgres_container
    auth = f"{pg.username}:{pg.password}"
    where = f"{pg.get_container_host_ip()}:{pg.get_exposed_port(5432)}/{pg.dbname}"
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT / "db_migrations",
        env={**os.environ, "POSTGRES_DSN": f"postgresql://{auth}@{where}"},
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}",
                    pytrace=False)
    return f"postgresql+asyncpg://{auth}@{where}"


@pytest.fixture(scope="session")
def redis_url(redis_container) -> str:
    return (
        f"redis://{redis_container.get_container_host_ip()}"
        f":{redis_container.get_exposed_port(6379)}/0"
    )


@pytest.fixture(scope="session")
def clickhouse_url(clickhouse_container) -> dict[str, Any]:
    """Return a dict suitable for `clickhouse_connect.get_client(**kwargs)`."""
    return {
        "host": clickhouse_container.get_container_host_ip(),
        "port": int(clickhouse_container.get_exposed_port(8123)),
        "username": clickhouse_container.username,
        "password": clickhouse_container.password,
        "database": clickhouse_container.dbname,
    }


@pytest.fixture(scope="session")
def migrated_clickhouse_url(clickhouse_url) -> dict[str, Any]:
    """``clickhouse_url`` with ``infrastructure/clickhouse/migrations`` applied.

    Same SQL files, in the same order, as ``scripts/init_db.sh``.
    """
    clickhouse_connect = _require("clickhouse_connect")
    client = clickhouse_connect.get_client(**clickhouse_url)
    try:
        for path in sorted(CLICKHOUSE_MIGRATIONS.glob("*.sql")):
            body = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("--")
            )
            for stmt in (s.strip() for s in body.split(";")):
                if stmt:
                    client.command(stmt)
    finally:
        client.close()
    return clickhouse_url


@pytest.fixture(scope="session")
def kafka_bootstrap(kafka_container) -> str:
    return kafka_container.bootstrap_server


@pytest.fixture(scope="session")
def schema_registry_url(schema_registry_container) -> str:
    c = schema_registry_container
    return f"http://{c.get_container_host_ip()}:{c.get_exposed_port(8081)}"


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def random_user_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def random_campaign_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def make_recommendation_payload():
    """Build a Recommendation API-shaped payload for tests."""

    def _build(user_id: str = "u-1", **overrides) -> dict:
        base = {
            "user_id": user_id,
            "recommendations": [
                {
                    "mcc_code": "5411",
                    "score": 0.83,
                    "campaign_id": str(uuid.uuid4()),
                    "top_factors": {"recency_days": 0.12, "freq_5411": 0.07},
                },
            ],
            "model_version": "42",
            "candidates_considered": 5,
        }
        base.update(overrides)
        return base

    return _build


# ---------------------------------------------------------------------------
# Per-service path injection helpers
# ---------------------------------------------------------------------------
def use_service(name: str) -> None:
    """Make ``services/<name>/app`` importable as a top-level ``app`` package.

    Designed to be called from a test-module top-level *before* importing
    ``app.*``. Removes any other service's app from ``sys.modules`` so the
    fresh namespace is loaded.
    """
    p = _REPO_ROOT / "services" / name
    sp = str(p)

    # Drop currently-loaded `app` modules — they may belong to a different
    # service from a previous test file.
    for mod in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[mod]

    # Ensure our path is the first hit for `import app`.
    if sp in sys.path:
        sys.path.remove(sp)
    sys.path.insert(0, sp)


# ---------------------------------------------------------------------------
# Asyncio loop policy on Linux — pytest-asyncio uses function-scope by
# default; tests use `asyncio_mode = "auto"` so async tests run as-is.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()
