"""Integration: mobile recommendations → respond ACCEPTED → Redis hot key.

Spins up Postgres + Redis via testcontainers; mocks the upstream
Recommendation API with httpx.MockTransport so the test stays hermetic.
The end-to-end assertion is:

    1) GET /v1/mobile/recommendations/{user} returns trimmed/enriched items
       (no model_score, no top_factors; cashback_rate as "5%"; deeplink set).
    2) POST /respond with action=ACCEPTED writes accepted_offers:{user}:{mcc}
       in Redis with the correct TTL.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
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
async def primed(postgres_container, redis_container):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pg_dsn = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg"
    )
    redis_url = (
        f"redis://{redis_container.get_container_host_ip()}"
        f":{redis_container.get_exposed_port(6379)}/0"
    )

    user_id = uuid.uuid4()
    campaign_id = uuid.uuid4()

    engine = create_async_engine(pg_dsn)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"))
        for stmt in [
            """DO $$BEGIN
                CREATE TYPE campaign_status AS ENUM
                    ('DRAFT','ACTIVE','PAUSED','COMPLETED');
               EXCEPTION WHEN duplicate_object THEN null; END$$;""",
            """DO $$BEGIN
                CREATE TYPE accrual_status AS ENUM ('PENDING','PAID');
               EXCEPTION WHEN duplicate_object THEN null; END$$;""",
            """DO $$BEGIN
                CREATE TYPE recommendation_response_status AS ENUM
                    ('PENDING','ACCEPTED','DECLINED','EXPIRED','SNOOZE');
               EXCEPTION WHEN duplicate_object THEN null; END$$;""",
        ]:
            await conn.execute(text(stmt))

        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                user_id UUID PRIMARY KEY, external_id VARCHAR(64) UNIQUE,
                segment_id INT
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_campaigns (
                campaign_id UUID PRIMARY KEY,
                name TEXT NOT NULL,
                cashback_rate NUMERIC(5,2) NOT NULL,
                budget_total NUMERIC(15,2) NOT NULL,
                budget_spent NUMERIC(15,2) DEFAULT 0,
                status campaign_status DEFAULT 'ACTIVE',
                start_date TIMESTAMPTZ DEFAULT now(),
                end_date   TIMESTAMPTZ DEFAULT now() + interval '30 days'
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS recommendations (
                recommendation_id UUID PRIMARY KEY,
                user_id UUID, campaign_id UUID,
                mcc_code CHAR(4), model_score REAL,
                generated_at TIMESTAMPTZ DEFAULT now(),
                response_status recommendation_response_status DEFAULT 'PENDING',
                expires_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_accruals (
                accrual_id UUID PRIMARY KEY,
                user_id UUID, campaign_id UUID,
                transaction_id VARCHAR(128), mcc_code CHAR(4),
                transaction_amount NUMERIC(15,2),
                cashback_amount NUMERIC(15,2),
                status accrual_status DEFAULT 'PENDING',
                accrued_at TIMESTAMPTZ DEFAULT now()
            )
        """))
        await conn.execute(
            text("INSERT INTO users (user_id, external_id, segment_id) "
                 "VALUES (:uid, 'test', 5)"),
            {"uid": user_id},
        )
        await conn.execute(text("""
            INSERT INTO cashback_campaigns (
                campaign_id, name, cashback_rate, budget_total, status,
                start_date, end_date
            ) VALUES (
                :cid, 'Grocery promo', 5.00, 100000, 'ACTIVE',
                now() - interval '1 day', now() + interval '14 days'
            )
        """), {"cid": campaign_id})
    await engine.dispose()

    os.environ["POSTGRES_DSN"] = pg_dsn
    os.environ["REDIS_URL"] = redis_url
    os.environ["RECOMMENDATION_API_URL"] = "http://recommendation-api:8001"
    os.environ["KAFKA_BOOTSTRAP_SERVERS"] = "kafka:9092"

    from app.config import get_settings
    get_settings.cache_clear()

    # ---- mock the upstream Recommendation API --------------------
    upstream_payload = {
        "user_id": str(user_id),
        "recommendations": [
            {
                "mcc_code": "5411",
                "score": 0.83,
                "campaign_id": str(campaign_id),
                "top_factors": {"recency_days": 0.12, "freq_5411": 0.07},
            }
        ],
        "model_version": "42",
        "candidates_considered": 7,
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/recommendations/"):
            return httpx.Response(200, json=upstream_payload)
        return httpx.Response(200, json={"status": "ok"})

    transport = httpx.MockTransport(mock_handler)

    from app.clients.recommendation_client import (
        CircuitBreaker,
        RecommendationClient,
    )
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as ac:
        # Wait for lifespan to attach state, then swap the upstream client
        # with a transport-mocked one for hermetic testing.
        await ac.get("/health/live")  # triggers lifespan
        mock_http = httpx.AsyncClient(transport=transport, base_url="http://up")
        app.state.http_client = mock_http
        app.state.recommendation_client = RecommendationClient(
            "http://up", mock_http, breaker=CircuitBreaker(), max_attempts=1,
        )
        # Disable Kafka producer in this hermetic env.
        app.state.kafka_producer = None
        yield {
            "client": ac,
            "user_id": user_id,
            "campaign_id": campaign_id,
            "redis_url": redis_url,
        }
        await mock_http.aclose()


# ---------------------------------------------------------------------------
async def test_get_recommendations_strips_internals_and_adds_mobile_fields(primed):
    ac = primed["client"]
    user_id = primed["user_id"]

    resp = await ac.get(f"/v1/mobile/recommendations/{user_id}?top_k=5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(user_id)
    items = body["recommendations"]
    assert len(items) == 1
    item = items[0]

    # Internals are gone.
    assert "model_score" not in item
    assert "top_factors" not in item
    assert "score" not in item

    # Mobile-friendly enrichment.
    assert item["category_name"] == "Продукты"
    assert item["category_icon_url"].endswith("/groceries.png")
    assert item["cashback_rate"] == "5%"
    assert item["expires_at"] is not None
    assert len(item["terms_summary"]) <= 140
    assert item["deeplink"].startswith("cashback://offer/")
    assert item["recommendation_id"] is not None
    assert body["model_version"] == "42"


async def test_respond_accepted_creates_redis_hot_path_key(primed):
    import redis.asyncio as aioredis

    ac = primed["client"]
    user_id = primed["user_id"]

    # Get a recommendation_id from the previous endpoint.
    resp = await ac.get(f"/v1/mobile/recommendations/{user_id}?top_k=5")
    rec = resp.json()["recommendations"][0]
    rec_id = rec["recommendation_id"]

    # Accept it.
    accept = await ac.post(
        f"/v1/mobile/recommendations/{rec_id}/respond",
        json={"action": "ACCEPTED"},
    )
    assert accept.status_code == 200, accept.text
    body = accept.json()
    assert body["action"] == "ACCEPTED"
    assert body["accepted_offer_key"] is not None
    assert body["accepted_offer_key"].startswith(f"accepted_offers:{user_id}:")

    # The Redis key really exists.
    r = aioredis.Redis.from_url(primed["redis_url"], decode_responses=True)
    raw = await r.get(body["accepted_offer_key"])
    assert raw is not None
    payload = json.loads(raw)
    assert payload["recommendation_id"] == rec_id
    ttl = await r.ttl(body["accepted_offer_key"])
    assert ttl > 60  # at least the floor we apply
    await r.aclose()


async def test_respond_snooze_returns_run_at(primed):
    ac = primed["client"]
    user_id = primed["user_id"]
    resp = await ac.get(f"/v1/mobile/recommendations/{user_id}?top_k=5")
    rec_id = resp.json()["recommendations"][0]["recommendation_id"]

    accept = await ac.post(
        f"/v1/mobile/recommendations/{rec_id}/respond",
        json={"action": "SNOOZE"},
    )
    assert accept.status_code == 200
    body = accept.json()
    assert body["action"] == "SNOOZE"
    assert body["snooze_until"] is not None


async def test_balance_endpoint_aggregates_accruals(primed):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    user_id = primed["user_id"]
    campaign_id = primed["campaign_id"]

    engine = create_async_engine(os.environ["POSTGRES_DSN"])
    async with engine.begin() as conn:
        # Two PENDING + one PAID accrual.
        for amount, status in [(10, "PENDING"), (15, "PENDING"), (25, "PAID")]:
            await conn.execute(text("""
                INSERT INTO cashback_accruals
                  (accrual_id, user_id, campaign_id, transaction_id,
                   mcc_code, transaction_amount, cashback_amount, status)
                VALUES
                  (gen_random_uuid(), :uid, :cid, :tid,
                   '5411', 100, :amt, :st)
            """), {"uid": user_id, "cid": campaign_id,
                   "tid": f"tx-{amount}", "amt": amount, "st": status})
    await engine.dispose()

    ac = primed["client"]
    resp = await ac.get(f"/v1/mobile/cashback/balance/{user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert float(body["pending_amount"]) == 25.0
    assert float(body["paid_amount"]) == 25.0
    assert float(body["available_amount"]) == 25.0


async def test_history_endpoint_returns_recent_accruals(primed):
    user_id = primed["user_id"]
    ac = primed["client"]
    resp = await ac.get(f"/v1/mobile/cashback/history/{user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert all("category_name" in item for item in body["items"])
