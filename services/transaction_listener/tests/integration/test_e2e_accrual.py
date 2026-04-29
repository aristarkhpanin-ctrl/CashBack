"""Integration / e2e test for the accrual engine.

Spins up Postgres + Redis via testcontainers, applies a minimal subset
of the Alembic schema (campaign, accruals, ``calculate_cashback`` SQL
function), then exercises the full lifecycle:

    1. user accepts an offer  →  Redis ``accepted_offers:{u}:{mcc}`` set
    2. AccrualEngine.accrue() is called with an inbound transaction
    3. assert: cashback_accruals row exists, budget_spent incremented,
       Redis hot-path key removed.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

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
class FakeProducer:
    """Captures events that the accrual engine publishes to Kafka."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, topic: str, *, key=None, value=None) -> None:
        self.sent.append({
            "topic": topic,
            "key": key.decode("utf-8") if isinstance(key, bytes) else key,
            "value": json.loads(value.decode("utf-8")) if value else None,
        })


# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
async def primed_environment(postgres_container, redis_container):
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
                CREATE TYPE campaign_status AS ENUM (
                    'DRAFT','ACTIVE','PAUSED','COMPLETED');
            EXCEPTION WHEN duplicate_object THEN null; END$$;
        """))
        await conn.execute(text("""
            DO $$BEGIN
                CREATE TYPE accrual_status AS ENUM ('PENDING','PAID');
            EXCEPTION WHEN duplicate_object THEN null; END$$;
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_campaigns (
                campaign_id UUID PRIMARY KEY,
                name TEXT, target_segment_ids INT[] DEFAULT '{}',
                cashback_rate NUMERIC(5,2) NOT NULL,
                min_transaction_amount NUMERIC(15,2),
                budget_total NUMERIC(15,2) NOT NULL,
                budget_spent NUMERIC(15,2) DEFAULT 0,
                status campaign_status DEFAULT 'ACTIVE',
                start_date TIMESTAMPTZ DEFAULT now(),
                end_date   TIMESTAMPTZ DEFAULT now() + interval '30 days',
                allowed_channels TEXT[] DEFAULT '{}',
                require_existing_behavior BOOLEAN DEFAULT FALSE,
                rate_tiers JSONB
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_accruals (
                accrual_id UUID PRIMARY KEY,
                user_id UUID NOT NULL,
                campaign_id UUID NOT NULL,
                transaction_id VARCHAR(128) NOT NULL,
                mcc_code CHAR(4) NOT NULL,
                transaction_amount NUMERIC(15,2) NOT NULL,
                cashback_amount NUMERIC(15,2) NOT NULL,
                status accrual_status DEFAULT 'PENDING',
                accrued_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (transaction_id, campaign_id)
            )
        """))
        await conn.execute(text("""
            CREATE OR REPLACE FUNCTION calculate_cashback(
                p_campaign_id UUID, p_amount NUMERIC
            ) RETURNS NUMERIC AS $$
            DECLARE v_rate NUMERIC;
            BEGIN
                SELECT cashback_rate INTO v_rate
                  FROM cashback_campaigns
                 WHERE campaign_id = p_campaign_id;
                IF v_rate IS NULL THEN RETURN 0; END IF;
                RETURN ROUND(p_amount * v_rate / 100, 2);
            END;
            $$ LANGUAGE plpgsql STABLE;
        """))
    await engine.dispose()

    import redis.asyncio as aioredis
    redis = aioredis.Redis.from_url(redis_url, decode_responses=True)

    yield {
        "pg_dsn": pg_dsn,
        "redis": redis,
    }

    await redis.aclose()


# ---------------------------------------------------------------------------
async def test_full_accrual_cycle(primed_environment):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.accrual.engine import AccrualEngine

    pg_dsn = primed_environment["pg_dsn"]
    redis = primed_environment["redis"]
    engine = create_async_engine(pg_dsn)

    user_id = str(uuid.uuid4())
    campaign_id = uuid.uuid4()
    mcc = "5411"
    transaction_id = "tx-001"
    amount = Decimal("1000.00")

    # --- Seed campaign + Redis hot-path key -------------------------------
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO cashback_campaigns (
                campaign_id, name, cashback_rate, budget_total, status
            ) VALUES (
                :cid, 'demo', 5.0, 100000, 'ACTIVE'
            )
        """), {"cid": str(campaign_id)})

    await redis.set(
        f"accepted_offers:{user_id}:{mcc}",
        json.dumps({"campaign_id": str(campaign_id)}),
    )

    # --- Run accrual ------------------------------------------------------
    producer = FakeProducer()
    accrual = AccrualEngine(
        db_engine=engine,
        kafka_producer=producer,
        redis_client=redis,
    )
    result = await accrual.accrue(
        user_id=user_id,
        campaign_id=str(campaign_id),
        transaction_id=transaction_id,
        mcc=mcc,
        amount=amount,
    )

    # --- Assert -----------------------------------------------------------
    assert result.accrued is True
    assert result.cashback_amount == Decimal("50.00")     # 5% of 1000

    async with engine.connect() as conn:
        row = (await conn.execute(text("""
            SELECT cashback_amount, transaction_amount, status
              FROM cashback_accruals
             WHERE transaction_id = :tid AND campaign_id = :cid
        """), {"tid": transaction_id, "cid": str(campaign_id)})).first()
    assert row is not None
    assert Decimal(row.cashback_amount) == Decimal("50.00")

    async with engine.connect() as conn:
        spent = (await conn.execute(text("""
            SELECT budget_spent FROM cashback_campaigns
             WHERE campaign_id = :cid
        """), {"cid": str(campaign_id)})).scalar()
    assert Decimal(spent) == Decimal("50.00")

    # Redis hot-path key removed after a successful accrual.
    assert await redis.get(f"accepted_offers:{user_id}:{mcc}") is None

    # Kafka event published with the correct topic + payload.
    assert len(producer.sent) == 1
    evt = producer.sent[0]
    assert evt["topic"] == "cashback.accrued"
    assert evt["key"] == user_id
    assert evt["value"]["transaction_id"] == transaction_id
    assert evt["value"]["cashback_amount"] == "50.00"

    await engine.dispose()


async def test_duplicate_transaction_does_not_double_debit(primed_environment):
    """A second call with the same transaction_id must be a no-op."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.accrual.engine import AccrualEngine

    engine = create_async_engine(primed_environment["pg_dsn"])
    redis = primed_environment["redis"]
    producer = FakeProducer()

    cid = uuid.uuid4()
    user_id = str(uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO cashback_campaigns (
                campaign_id, name, cashback_rate, budget_total, status
            ) VALUES (
                :cid, 'demo2', 4.0, 100000, 'ACTIVE'
            )
        """), {"cid": str(cid)})

    await redis.set(f"accepted_offers:{user_id}:5411",
                    json.dumps({"campaign_id": str(cid)}))

    accrual = AccrualEngine(engine, producer, redis)
    first = await accrual.accrue(
        user_id=user_id, campaign_id=str(cid),
        transaction_id="tx-dup", mcc="5411", amount=Decimal("500"),
    )
    assert first.accrued is True

    # Re-set the redis key so we can call accrue() again — the engine
    # otherwise short-circuits on missing offer (this is solely an
    # accrual-level idempotency test).
    await redis.set(f"accepted_offers:{user_id}:5411",
                    json.dumps({"campaign_id": str(cid)}))

    second = await accrual.accrue(
        user_id=user_id, campaign_id=str(cid),
        transaction_id="tx-dup", mcc="5411", amount=Decimal("500"),
    )
    assert second.accrued is False
    assert second.skipped_reason == "duplicate_transaction"

    async with engine.connect() as conn:
        spent = (await conn.execute(text("""
            SELECT budget_spent FROM cashback_campaigns
             WHERE campaign_id = :cid
        """), {"cid": str(cid)})).scalar()
    # 4% of 500 = 20.00 — debited exactly once.
    assert Decimal(spent) == Decimal("20.00")

    await engine.dispose()
