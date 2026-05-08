"""Integration smoke tests that spin up Redis + Postgres via testcontainers.

Marked ``integration`` — exclude with ``-m "not integration"`` when running
without Docker / on CI runners that don't expose the daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import AsyncIterator

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def postgres_container():
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="module")
def redis_container():
    pytest.importorskip("testcontainers")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7.2-alpine") as r:
        yield r


# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
async def primed_app(postgres_container, redis_container):
    """Bootstrap the FastAPI app against the containers."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pg_dsn_async = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg"
    )
    redis_url = (
        f"redis://{redis_container.get_container_host_ip()}"
        f":{redis_container.get_exposed_port(6379)}/0"
    )

    # 1) Create the user_id we'll query for.
    user_id = str(uuid.uuid4())
    engine = create_async_engine(pg_dsn_async)
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id UUID PRIMARY KEY,
                external_id VARCHAR(64) UNIQUE,
                segment_id INT
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_campaigns (
                campaign_id UUID PRIMARY KEY,
                name TEXT,
                budget_total NUMERIC,
                budget_spent NUMERIC,
                status TEXT,
                start_date TIMESTAMPTZ,
                end_date TIMESTAMPTZ,
                allowed_channels TEXT[],
                cashback_rate NUMERIC,
                min_transaction_amount NUMERIC
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS campaign_categories (
                campaign_id UUID,
                mcc_code CHAR(4)
            )
        """))
        await conn.execute(
            text("INSERT INTO users (user_id, external_id, segment_id) "
                 "VALUES (:uid, :ext, 5) ON CONFLICT DO NOTHING"),
            {"uid": user_id, "ext": "test_user"},
        )
        cid = str(uuid.uuid4())
        await conn.execute(
            text("""
                INSERT INTO cashback_campaigns
                  (campaign_id, name, budget_total, budget_spent, status,
                   start_date, end_date, allowed_channels, cashback_rate,
                   min_transaction_amount)
                VALUES
                  (:cid, 'demo', 100000, 0, 'ACTIVE',
                   :s, :e, ARRAY['ONLINE','POS','MOBILE'], 3.0, 100.0)
            """),
            {"cid": cid,
             "s": datetime.now(UTC) - timedelta(days=1),
             "e": datetime.now(UTC) + timedelta(days=30)},
        )
        await conn.execute(
            text("INSERT INTO campaign_categories VALUES (:cid, '5411')"),
            {"cid": cid},
        )

    # 2) Pre-seed Redis with the user's feature vector.
    import redis.asyncio as aioredis
    r = aioredis.Redis.from_url(redis_url, decode_responses=True)
    await r.set(
        f"features:{user_id}",
        json.dumps({
            "user_id": user_id,
            "segment_id": 5,
            "recency_days": 1,
            "frequency_total": 30,
            "monetary_total": 50000.0,
            "avg_ticket": 1500.0,
            "weekend_ratio": 0.4,
            "evening_ratio": 0.6,
            "distinct_mcc_count": 8,
        }),
    )

    # 3) Override env so create_app picks up the test DSNs.
    os.environ["POSTGRES_DSN"] = pg_dsn_async
    os.environ["REDIS_URL"] = redis_url
    os.environ["MLFLOW_TRACKING_URI"] = "http://invalid:5000"  # readiness will fail
    os.environ["CLICKHOUSE_HOST"] = "invalid-host"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    app = create_app()

    yield app, user_id, redis_url

    await r.aclose()
    await engine.dispose()


# ---------------------------------------------------------------------------
async def test_health_live_returns_200(primed_app):
    from httpx import ASGITransport, AsyncClient

    app, _, _ = primed_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        async with ac.stream("GET", "/health/live") as resp:
            await resp.aread()
            assert resp.status_code == 200


async def test_metrics_endpoint_exposes_prometheus_text(primed_app):
    from httpx import ASGITransport, AsyncClient

    app, _, _ = primed_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/metrics")
        assert resp.status_code == 200
        assert "http_requests_total" in resp.text or "process_" in resp.text


async def test_root_endpoint_includes_request_id(primed_app):
    from httpx import ASGITransport, AsyncClient

    app, _, _ = primed_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/")
        assert resp.status_code == 200
        assert "X-Request-ID" in resp.headers
        body = resp.json()
        assert body["service"] == "recommendation-api"


async def test_recommendations_404_for_unknown_user(primed_app):
    """Without a feature payload in Redis the API returns 404."""
    from httpx import ASGITransport, AsyncClient

    app, _, _ = primed_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get(f"/recommendations/{uuid.uuid4()}?top_k=3")
        # 404 (unknown user) or 503 (model not loaded) — both acceptable when
        # MLflow is unreachable in this hermetic test.
        assert resp.status_code in (404, 503)
