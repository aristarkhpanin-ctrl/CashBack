"""Integration smoke for the Campaign Manager CRUD path.

Spins up Postgres + Redis via testcontainers, applies the schema for
``cashback_campaigns`` + ``campaign_categories`` (a minimal subset of
the production migration), and exercises the public API through
``httpx.AsyncClient(transport=ASGITransport(app))``.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration


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


@pytest.fixture(scope="module")
async def app_client(postgres_container, redis_container):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pg_dsn = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg",
    )
    redis_url = (
        f"redis://{redis_container.get_container_host_ip()}"
        f":{redis_container.get_exposed_port(6379)}/0"
    )

    engine = create_async_engine(pg_dsn)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"))
        await conn.execute(text("""
            DO $$BEGIN
                CREATE TYPE campaign_status AS ENUM ('DRAFT','ACTIVE','PAUSED','COMPLETED');
            EXCEPTION WHEN duplicate_object THEN null; END$$;
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_campaigns (
                campaign_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                name TEXT NOT NULL,
                target_segment_ids INT[] NOT NULL,
                cashback_rate NUMERIC(5,2) NOT NULL,
                min_transaction_amount NUMERIC(15,2),
                budget_total NUMERIC(15,2) NOT NULL,
                budget_spent NUMERIC(15,2) DEFAULT 0,
                status campaign_status DEFAULT 'DRAFT',
                start_date TIMESTAMPTZ NOT NULL,
                end_date TIMESTAMPTZ NOT NULL,
                allowed_channels TEXT[] DEFAULT '{}',
                require_existing_behavior BOOLEAN DEFAULT FALSE,
                rate_tiers JSONB
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS campaign_categories (
                campaign_id UUID REFERENCES cashback_campaigns ON DELETE CASCADE,
                mcc_code CHAR(4),
                min_transaction_amount NUMERIC(15,2),
                PRIMARY KEY(campaign_id, mcc_code)
            )
        """))
    await engine.dispose()

    os.environ["POSTGRES_DSN"] = pg_dsn
    os.environ["REDIS_URL"] = redis_url
    os.environ["CLICKHOUSE_HOST"] = "invalid"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
async def test_create_get_and_status_transitions(app_client):
    payload = {
        "name": "Test grocery",
        "target_segment_ids": [1, 2, 3],
        "cashback_rate": "3.0",
        "min_transaction_amount": "100.00",
        "budget_total": "10000.00",
        "start_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end_date":   (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "allowed_channels": ["POS", "ONLINE"],
        "mcc_codes": ["5411"],
    }
    create = await app_client.post("/campaigns", json=payload)
    assert create.status_code == 201, create.text
    body = create.json()
    cid = body["campaign_id"]
    assert body["status"] == "DRAFT"
    assert body["mcc_codes"] == ["5411"]

    got = await app_client.get(f"/campaigns/{cid}")
    assert got.status_code == 200
    assert got.json()["campaign_id"] == cid

    activated = await app_client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"},
    )
    assert activated.status_code == 200
    assert activated.json()["current"] == "ACTIVE"

    illegal = await app_client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"},
    )
    assert illegal.status_code == 409  # ACTIVE→activate is forbidden


async def test_invalid_payload_rejected(app_client):
    bad = {
        "name": "x",
        "target_segment_ids": [],   # ← empty list is invalid
        "cashback_rate": "200",     # > 100 — invalid
        "budget_total": "0",        # not > 0
        "start_date": datetime.now(UTC).isoformat(),
        "end_date":   datetime.now(UTC).isoformat(),
        "mcc_codes": ["abc"],       # not 4-digit
    }
    resp = await app_client.post("/campaigns", json=bad)
    assert resp.status_code == 422


async def test_metrics_and_root(app_client):
    root = await app_client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "campaign-manager"

    metrics = await app_client.get("/metrics")
    assert metrics.status_code == 200
    assert "process_" in metrics.text or "http_requests_total" in metrics.text
