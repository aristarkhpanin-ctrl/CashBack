"""Infrastructure for the listener's integration tests (stage C, B45).

Everything is real: Postgres migrated to alembic head with the repo's
``db_migrations`` (the production ``calculate_cashback`` /
``eligible_campaigns`` — no hand-written DDL), Redis, Kafka (KRaft,
apache/kafka as in docker-compose.yml) and Confluent Schema Registry.

Each service comes from an environment variable or, without it, from
testcontainers (needs Docker):

* ``LISTENER_IT_POSTGRES_DSN`` — server DSN of a role with CREATEDB (e.g.
  ``postgresql://cashback:cashback@127.0.0.1:5432/postgres``); the session
  creates its own database, migrates it to head and drops it at the end;
* ``LISTENER_IT_REDIS_URL`` — e.g. ``redis://127.0.0.1:6379/13``;
* ``LISTENER_IT_KAFKA_BOOTSTRAP`` + ``LISTENER_IT_SCHEMA_REGISTRY_URL``.

Migrating needs ``alembic`` and ``psycopg2`` (db_migrations/env.py) in this
interpreter. A missing dependency or Docker skips locally and fails under CI
(``CI=true``): an integration test that did not run proved nothing.
"""
from __future__ import annotations

import importlib
import os
import re
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit, urlunsplit

import pytest

REPO = Path(__file__).resolve().parents[4]
MIGRATIONS = REPO / "db_migrations"

POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7.2-alpine"
KAFKA_IMAGE = "apache/kafka:3.7.2"
SCHEMA_REGISTRY_IMAGE = "confluentinc/cp-schema-registry:7.6.1"


def _infra_unavailable(reason: str) -> NoReturn:
    if os.getenv("CI", "").strip().lower() in {"1", "true", "yes"}:
        pytest.fail(f"{reason} — integration tests must not be skipped in CI",
                    pytrace=False)
    pytest.skip(reason)


def _require(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError:
        _infra_unavailable(f"{module} is not installed")


def _tc(module: str, name: str) -> Any:
    """A testcontainers class (4.15+ moved the modules under ``community``)."""
    _require("testcontainers")
    for path in (f"testcontainers.community.{module}", f"testcontainers.{module}"):
        try:
            return getattr(importlib.import_module(path), name)
        except ImportError:
            continue
    _infra_unavailable(f"testcontainers.{module} is not installed")


def _docker_or_skip(what: str) -> None:
    try:
        import docker  # type: ignore[import-untyped]
        docker.from_env().ping()
    except Exception as exc:  # noqa: BLE001
        _infra_unavailable(f"{what} is not set and Docker is unavailable: {exc}")


def plain_dsn(dsn: str) -> str:
    """``postgresql+<driver>://…`` → ``postgresql://…`` (libpq / alembic)."""
    return re.sub(r"^postgres(?:ql)?\+\w+://", "postgresql://", dsn)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Postgres at alembic head
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_server_dsn():
    dsn = os.getenv("LISTENER_IT_POSTGRES_DSN")
    if dsn:
        yield plain_dsn(dsn)
        return
    _docker_or_skip("LISTENER_IT_POSTGRES_DSN")
    PostgresContainer = _tc("postgres", "PostgresContainer")
    with PostgresContainer(POSTGRES_IMAGE) as pg:
        yield plain_dsn(pg.get_connection_url())


@pytest.fixture(scope="session")
def pg_database(pg_server_dsn):
    """``(plain DSN, database name)`` of a fresh database migrated to head."""
    psycopg2 = _require("psycopg2")
    _require("alembic")
    name = f"listener_it_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(pg_server_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        dsn = urlunsplit(urlsplit(pg_server_dsn)._replace(path=f"/{name}"))
        # db_migrations/env.py reads POSTGRES_DSN; the bootstrap passwords of
        # 002/008 are random when unset.
        env = {k: v for k, v in os.environ.items()
               if k not in ("ADMIN_BOOTSTRAP_PASSWORD", "DEMO_USERS_PASSWORD")}
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=MIGRATIONS, env={**env, "POSTGRES_DSN": dsn},
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            pytest.fail(f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}",
                        pytrace=False)
        yield dsn, name
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@pytest.fixture(scope="session")
def pg_dsn(pg_database) -> str:
    """asyncpg DSN of the migrated database."""
    return pg_database[0].replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture()
async def db(pg_dsn):
    from sqlalchemy.ext.asyncio import create_async_engine

    # pre-ping: pg_outage terminates this pool's connections too.
    engine = create_async_engine(pg_dsn, pool_size=25, max_overflow=5,
                                 pool_pre_ping=True)
    yield engine
    await engine.dispose()


@pytest.fixture()
def pg_outage(pg_server_dsn, pg_database):
    """``cut()`` / ``restore()``: the database refuses new connections and
    every open one is terminated — a Postgres outage for this database only."""
    import psycopg2

    name = pg_database[1]

    def _admin(sql: str) -> None:
        conn = psycopg2.connect(pg_server_dsn)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()

    class Outage:
        def cut(self) -> None:
            _admin(f'ALTER DATABASE "{name}" WITH ALLOW_CONNECTIONS false')
            _admin("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                   f"WHERE datname = '{name}' AND pid <> pg_backend_pid()")

        def restore(self) -> None:
            _admin(f'ALTER DATABASE "{name}" WITH ALLOW_CONNECTIONS true')

    outage = Outage()
    try:
        yield outage
    finally:
        outage.restore()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def redis_url():
    url = os.getenv("LISTENER_IT_REDIS_URL")
    if url:
        yield url
        return
    _docker_or_skip("LISTENER_IT_REDIS_URL")
    RedisContainer = _tc("redis", "RedisContainer")
    with RedisContainer(REDIS_IMAGE) as r:
        yield f"redis://{r.get_container_host_ip()}:{r.get_exposed_port(6379)}/0"


@pytest.fixture()
async def redis(redis_url):
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# Kafka + Schema Registry
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def kafka_stack():
    """``(bootstrap, schema registry URL)``."""
    bootstrap = os.getenv("LISTENER_IT_KAFKA_BOOTSTRAP")
    registry = os.getenv("LISTENER_IT_SCHEMA_REGISTRY_URL")
    if bootstrap and registry:
        yield bootstrap, registry
        return
    _docker_or_skip("LISTENER_IT_KAFKA_BOOTSTRAP / LISTENER_IT_SCHEMA_REGISTRY_URL")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.network import Network
    from testcontainers.core.wait_strategies import (
        HttpWaitStrategy,
        LogMessageWaitStrategy,
    )

    port = _free_port()
    with Network() as network:
        kafka = (
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
        with kafka:
            registry_container = (
                DockerContainer(SCHEMA_REGISTRY_IMAGE)
                .with_network(network)
                .with_exposed_ports(8081)
                .with_env("SCHEMA_REGISTRY_HOST_NAME", "schema-registry")
                .with_env("SCHEMA_REGISTRY_LISTENERS", "http://0.0.0.0:8081")
                .with_env("SCHEMA_REGISTRY_KAFKASTORE_BOOTSTRAP_SERVERS",
                          "PLAINTEXT://kafka:9092")
                .with_env("SCHEMA_REGISTRY_KAFKASTORE_TOPIC", "_schemas")
                .waiting_for(HttpWaitStrategy(8081, "/subjects")
                             .with_startup_timeout(180))
            )
            with registry_container:
                host = registry_container.get_container_host_ip()
                yield (f"127.0.0.1:{port}",
                       f"http://{host}:{registry_container.get_exposed_port(8081)}")
