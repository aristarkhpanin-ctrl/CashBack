"""Integration: TransactionEvent → transaction_listener → начисление (B48).

Денежный путь от Kafka до Postgres на настоящей инфраструктуре:
Kafka (KRaft) + Schema Registry + Postgres (миграции до head, боевая
``calculate_cashback``) + Redis. Слушатель — настоящий
``TransactionListener`` с настоящим ``AccrualEngine``.

Транзакции публикует тот же код, что и e2e-тест
(``tests/e2e/avro_producer.py``: AvroSerializer + Schema Registry, как
tx_simulator). Поэтому здесь, в CI, доказывается, что e2e-тест шлёт
сообщения, которые слушатель действительно декодирует; прежний e2e слал
JSON, слушатель отбрасывал его как ``bad_payload``, и тест денежного пути
навсегда уходил в skip.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.e2e.avro_producer import publish_transactions, transaction_event

# Use the transaction_listener service's `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "transaction_listener"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

# В первый тест модуля входит подъём Kafka, Schema Registry, Postgres и
# Redis — 60 с из tests/pyproject.toml на холодном раннере мало. Маркер
# timeout регистрирует только сам плагин (--strict-markers).
_TIMEOUT = [pytest.mark.timeout(300)] if importlib.util.find_spec("pytest_timeout") else []
pytestmark = [pytest.mark.integration, *_TIMEOUT]

ACCRUAL_WAIT_S = 60.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
async def stores(migrated_postgres_dsn, redis_url):
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated_postgres_dsn)
    redis = Redis.from_url(redis_url, decode_responses=True)
    yield engine, redis
    await redis.aclose()
    await engine.dispose()


async def _create_topic_from_offset_zero(bootstrap: str, topic: str, group: str) -> None:
    """Create ``topic`` and commit offset 0 for ``group``.

    The listener subscribes with ``auto_offset_reset="latest"``; with a
    committed offset it reads the topic from the start no matter whether
    it joined the group before or after the test published.
    """
    from aiokafka import AIOKafkaConsumer, TopicPartition
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    futures = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    await asyncio.to_thread(futures[topic].result, 30)

    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, group_id=group,
                                enable_auto_commit=False)
    await consumer.start()
    try:
        tp = TopicPartition(topic, 0)
        consumer.assign([tp])
        await consumer.commit({tp: 0})
    finally:
        await consumer.stop()


@pytest.fixture()
async def listener(kafka_bootstrap, schema_registry_url, migrated_postgres_dsn,
                   redis_url, stores):
    """The real listener + accrual engine on a fresh topic / consumer group."""
    from aiokafka import AIOKafkaProducer
    from app.accrual.engine import AccrualEngine
    from app.config import Settings
    from app.listener import TransactionListener

    suffix = uuid.uuid4().hex[:8]
    settings = Settings(
        kafka_bootstrap_servers=kafka_bootstrap,
        schema_registry_url=schema_registry_url,
        transactions_topic=f"transactions.raw.it-{suffix}",
        accruals_topic=f"cashback.accrued.it-{suffix}",
        consumer_group_accrual=f"cashback-accrual-group-it-{suffix}",
        postgres_dsn=migrated_postgres_dsn,
        redis_url=redis_url,
    )
    await _create_topic_from_offset_zero(
        kafka_bootstrap, settings.transactions_topic, settings.consumer_group_accrual,
    )

    engine, redis = stores
    producer = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap)
    await producer.start()
    accrual = AccrualEngine(
        db_engine=engine, kafka_producer=producer, redis_client=redis,
        accruals_topic=settings.accruals_topic,
        accepted_offer_key_fmt=settings.accepted_offer_key_fmt,
    )
    task = asyncio.create_task(TransactionListener(settings, accrual, redis).run())
    try:
        yield settings
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await producer.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _accepted_offer(engine, redis, settings, *, mcc: str, rate: str,
                          budget: str = "100000") -> tuple[str, str, str]:
    """ACTIVE campaign + user + accepted offer key, as mobile_api leaves it."""
    from sqlalchemy import text

    user_id, campaign_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (user_id, external_id, segment_id) "
                 "VALUES (:u, :ext, 5)"),
            {"u": user_id, "ext": f"it-{user_id[:8]}"},
        )
        await conn.execute(
            text("""INSERT INTO cashback_campaigns
                       (campaign_id, name, target_segment_ids, cashback_rate,
                        min_transaction_amount, budget_total, status,
                        start_date, end_date, allowed_channels)
                    VALUES (:c, :name, ARRAY[5], :rate, 100, :budget, 'ACTIVE',
                            now() - interval '1 day', now() + interval '30 days',
                            ARRAY['POS'])"""),
            {"c": campaign_id, "name": f"it-{campaign_id[:8]}",
             "rate": Decimal(rate), "budget": Decimal(budget)},
        )
        await conn.execute(
            text("INSERT INTO campaign_categories (campaign_id, mcc_code) "
                 "VALUES (:c, :m)"),
            {"c": campaign_id, "m": mcc},
        )
    key = settings.accepted_offer_key_fmt.format(user_id=user_id, mcc_code=mcc)
    await redis.setex(key, 3600, json.dumps({
        "campaign_id": campaign_id,
        "recommendation_id": str(uuid.uuid4()),
        "accepted_at": datetime.now(UTC).isoformat(),
    }))
    return user_id, campaign_id, key


async def _wait_for_accrual(engine, transaction_id: str, campaign_id: str):
    from sqlalchemy import text

    sql = text("""SELECT user_id::text AS user_id, mcc_code, transaction_amount,
                         cashback_amount, status::text AS status
                    FROM cashback_accruals
                   WHERE transaction_id = :t AND campaign_id = :c""")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ACCRUAL_WAIT_S
    while loop.time() < deadline:
        async with engine.connect() as conn:
            row = (await conn.execute(sql, {"t": transaction_id, "c": campaign_id})).first()
        if row is not None:
            return row
        await asyncio.sleep(0.2)
    return None


async def _accrual_count(engine, transaction_id: str) -> int:
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(
            text("SELECT count(*) FROM cashback_accruals WHERE transaction_id = :t"),
            {"t": transaction_id},
        )).scalar_one()


# ---------------------------------------------------------------------------
async def test_avro_transaction_from_e2e_producer_is_accrued(
    listener, stores, kafka_bootstrap, schema_registry_url,
):
    from sqlalchemy import text

    engine, redis = stores
    user_id, campaign_id, key = await _accepted_offer(
        engine, redis, listener, mcc="5411", rate="5.00",
    )
    event = transaction_event(user_id=user_id, mcc_code="5411", amount="1500.00")
    await asyncio.to_thread(
        publish_transactions, kafka_bootstrap, schema_registry_url,
        listener.transactions_topic, [event],
    )

    row = await _wait_for_accrual(engine, event["transaction_id"], campaign_id)
    assert row is not None, (
        f"no accrual for {event['transaction_id']} within {ACCRUAL_WAIT_S:.0f} s"
    )
    assert row.user_id == user_id
    assert row.mcc_code == "5411"
    assert row.transaction_amount == Decimal("1500.00")
    assert row.cashback_amount == Decimal("75.00")   # 5 % — calculate_cashback()
    assert row.status == "PENDING"

    async with engine.connect() as conn:
        spent = (await conn.execute(
            text("SELECT budget_spent FROM cashback_campaigns WHERE campaign_id = :c"),
            {"c": campaign_id},
        )).scalar_one()
    assert spent == Decimal("75.00")
    # Offer is consumed: the hot-path key is gone after the commit.
    assert await redis.get(key) is None


async def test_json_transaction_is_ignored_by_listener(
    listener, stores, kafka_bootstrap, schema_registry_url,
):
    """The old e2e published JSON — the listener drops it (bad_payload).

    An Avro event published after it is accrued, which proves the listener
    has already processed (and discarded) the JSON message before it.
    """
    from aiokafka import AIOKafkaProducer

    engine, redis = stores
    user_id, campaign_id, _key = await _accepted_offer(
        engine, redis, listener, mcc="5812", rate="3.00",
    )
    as_json = transaction_event(user_id=user_id, mcc_code="5812", amount="1000.00")
    as_avro = transaction_event(user_id=user_id, mcc_code="5812", amount="1000.00")

    producer = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap)
    await producer.start()
    try:
        await producer.send_and_wait(listener.transactions_topic,
                                     json.dumps(as_json).encode("utf-8"))
    finally:
        await producer.stop()
    await asyncio.to_thread(
        publish_transactions, kafka_bootstrap, schema_registry_url,
        listener.transactions_topic, [as_avro],
    )

    row = await _wait_for_accrual(engine, as_avro["transaction_id"], campaign_id)
    assert row is not None, "the Avro event published after the JSON one was not accrued"
    assert row.cashback_amount == Decimal("30.00")
    assert await _accrual_count(engine, as_json["transaction_id"]) == 0
