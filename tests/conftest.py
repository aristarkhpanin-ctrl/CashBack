"""Shared pytest fixtures — chapter 3.3, listing 3.15.

Provides session-scoped testcontainers for Postgres, Redis, ClickHouse
and Kafka. Fixtures gracefully ``importorskip`` when ``testcontainers``
is unavailable, so the test files are still collectible in environments
without Docker.

Usage in a test module::

    @pytest.mark.integration
    async def test_something(postgres_container, redis_container):
        dsn = postgres_container.get_connection_url()
        ...
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

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
# would fail). Instead, each integration test module calls
# :func:`use_service` (defined in :mod:`tests.integration._service_path`)
# at the top of the file to pick which service's ``app`` it needs.


# ---------------------------------------------------------------------------
# Session-scoped containers
# ---------------------------------------------------------------------------
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
        pytest.skip("Docker is not available — skipping testcontainers fixture")
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="session")
def redis_container(_docker_available):
    if not _docker_available:
        pytest.skip("Docker is not available — skipping testcontainers fixture")
    pytest.importorskip("testcontainers")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7.2-alpine") as r:
        yield r


@pytest.fixture(scope="session")
def clickhouse_container(_docker_available):
    if not _docker_available:
        pytest.skip("Docker is not available — skipping testcontainers fixture")
    try:
        from testcontainers.clickhouse import ClickHouseContainer
    except ImportError:
        pytest.skip("testcontainers[clickhouse] is not installed")
    with ClickHouseContainer("clickhouse/clickhouse-server:24.3-alpine") as ch:
        yield ch


@pytest.fixture(scope="session")
def kafka_container(_docker_available):
    if not _docker_available:
        pytest.skip("Docker is not available — skipping testcontainers fixture")
    pytest.importorskip("testcontainers")
    from testcontainers.kafka import KafkaContainer
    with KafkaContainer("confluentinc/cp-kafka:7.6.1") as k:
        yield k


# ---------------------------------------------------------------------------
# Connection strings derived from the containers above
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def postgres_dsn(postgres_container) -> str:
    return postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg",
    )


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
        "username": "default",
        "password": "",
        "database": "default",
    }


@pytest.fixture(scope="session")
def kafka_bootstrap(kafka_container) -> str:
    return kafka_container.get_bootstrap_server()


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
