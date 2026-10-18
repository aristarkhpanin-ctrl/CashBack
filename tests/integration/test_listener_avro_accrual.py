"""Integration: TransactionEvent → transaction_listener → начисление (B48, B45).

Денежный путь от Kafka до Postgres на настоящей инфраструктуре:
Kafka (KRaft) + Schema Registry + Postgres (миграции до head, боевые
``eligible_campaigns`` / ``calculate_cashback``) + Redis. Слушатель —
настоящий ``TransactionListener`` с настоящим ``AccrualEngine``.

Транзакции публикует тот же код, что и e2e-тест
(``tests/e2e/avro_producer.py``: AvroSerializer + Schema Registry, как
tx_simulator). Поэтому здесь, в CI, доказывается, что e2e-тест шлёт
сообщения, которые слушатель действительно декодирует; прежний e2e слал
JSON, слушатель отбрасывал его как ``bad_payload``, и тест денежного пути
навсегда уходил в skip. Сейчас такое сообщение уходит в DLQ
(``transactions.accrual.dlq``) — его видно, а партиция идёт дальше.

Оффер — как его оставляет mobile_api после ``respond``: строка
``recommendations`` в ACCEPTED у клиента с согласием ``personalised_cashback``
и ключ ``accepted_offers:{user}:{mcc}`` с её ``recommendation_id``. Здесь же —
проверки денежного пути e2e (контракт этапа C, §6), которые nightly гоняет
на живом стеке: вторая покупка по тому же офферу не платит даже при
потерянном DEL ключа, покупка ниже минимума категории не платит и оффер
не тратит. Ретраи, сбой Postgres посреди пачки и гонки — в
``services/transaction_listener/tests/integration``.
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
from types import SimpleNamespace

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
CONSENT = "personalised_cashback"


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


async def _create_topic(bootstrap: str, topic: str) -> None:
    """Create ``topic`` before the listener subscribes. Its group has no
    committed offset yet and starts at the beginning (``auto_offset_reset=
    "earliest"``, B45), whether it joins before or after the test publishes."""
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    futures = admin.create_topics([NewTopic(topic, num_partitions=1, replication_factor=1)])
    await asyncio.to_thread(futures[topic].result, 30)


@pytest.fixture()
async def listener(kafka_bootstrap, schema_registry_url, migrated_postgres_dsn,
                   redis_url, stores):
    """The real listener + accrual engine on a fresh topic / consumer group / DLQ."""
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
        dlq_topic=f"transactions.accrual.dlq.it-{suffix}",
        consumer_group_accrual=f"cashback-accrual-group-it-{suffix}",
        postgres_dsn=migrated_postgres_dsn,
        redis_url=redis_url,
    )
    await _create_topic(kafka_bootstrap, settings.transactions_topic)

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
                          budget: str = "100000",
                          category_min: str | None = None) -> SimpleNamespace:
    """What the accrual path reads after a customer accepted an offer.

    A consenting user, an ACTIVE campaign (minimum 100 ₽, per-category
    ``category_min`` if given), the offer's ``recommendations`` row in
    ACCEPTED and the hot-path key with the payload mobile_api writes on
    respond.
    """
    from sqlalchemy import text

    user_id, campaign_id, rec_id = (str(uuid.uuid4()) for _ in range(3))
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (user_id, external_id, segment_id) "
                 "VALUES (:u, :ext, 5)"),
            {"u": user_id, "ext": f"it-{user_id[:8]}"},
        )
        await conn.execute(
            text("INSERT INTO user_consents (user_id, consent_type, status, "
                 "document_version) VALUES (:u, :t, 'GRANTED', 'v1')"),
            {"u": user_id, "t": CONSENT},
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
            text("INSERT INTO campaign_categories (campaign_id, mcc_code, "
                 "min_transaction_amount) VALUES (:c, :m, :min)"),
            {"c": campaign_id, "m": mcc,
             "min": None if category_min is None else Decimal(category_min)},
        )
        await conn.execute(
            text("""INSERT INTO recommendations
                       (recommendation_id, user_id, campaign_id, mcc_code,
                        model_score, response_status, responded_at, expires_at)
                    VALUES (:r, :u, :c, :m, 0.9, 'ACCEPTED', now(),
                            now() + interval '1 day')"""),
            {"r": rec_id, "u": user_id, "c": campaign_id, "m": mcc},
        )
    key = settings.accepted_offer_key_fmt.format(user_id=user_id, mcc_code=mcc)
    payload = json.dumps({
        "campaign_id": campaign_id,
        "recommendation_id": rec_id,
        "accepted_at": datetime.now(UTC).isoformat(),
        "activation_id": uuid.uuid4().hex,
    })
    await redis.setex(key, 3600, payload)
    return SimpleNamespace(user_id=user_id, campaign_id=campaign_id,
                           recommendation_id=rec_id, key=key, payload=payload)


async def _wait_for(probe, timeout: float = ACCRUAL_WAIT_S):
    """Poll the coroutine ``probe()`` until it returns something truthy."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = await probe()
        if result or loop.time() >= deadline:
            return result
        await asyncio.sleep(0.2)


async def _accrual(engine, transaction_id: str, campaign_id: str):
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(
            text("""SELECT user_id::text AS user_id, mcc_code, transaction_amount,
                           cashback_amount, status::text AS status,
                           recommendation_id::text AS recommendation_id
                      FROM cashback_accruals
                     WHERE transaction_id = :t AND campaign_id = :c"""),
            {"t": transaction_id, "c": campaign_id},
        )).first()


async def _scalar(engine, sql: str, **params):
    from sqlalchemy import text

    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def _publish(kafka_bootstrap, schema_registry_url, topic, *events) -> None:
    await asyncio.to_thread(publish_transactions, kafka_bootstrap,
                            schema_registry_url, topic, list(events))


# ---------------------------------------------------------------------------
async def test_avro_transaction_from_e2e_producer_is_accrued_once(
    listener, stores, kafka_bootstrap, schema_registry_url,
):
    """The e2e money path: the purchase accrues 5 %, the offer is consumed.

    Then a lost ``DEL`` (the listener died between the PG commit and the
    Redis delete; the key still holds the offer) and a SECOND qualifying
    purchase on the same offer: Postgres knows the offer is paid (UNIQUE
    ``recommendation_id``), so nothing is paid again, and the listener
    removes the leftover key.
    """
    engine, redis = stores
    offer = await _accepted_offer(engine, redis, listener, mcc="5411", rate="5.00")
    first = transaction_event(user_id=offer.user_id, mcc_code="5411", amount="1500.00")
    await _publish(kafka_bootstrap, schema_registry_url, listener.transactions_topic, first)

    row = await _wait_for(lambda: _accrual(engine, first["transaction_id"], offer.campaign_id))
    assert row is not None, (
        f"no accrual for {first['transaction_id']} within {ACCRUAL_WAIT_S:.0f} s"
    )
    assert row.user_id == offer.user_id
    assert row.mcc_code == "5411"
    assert row.transaction_amount == Decimal("1500.00")
    assert row.cashback_amount == Decimal("75.00")   # 5 % — calculate_cashback()
    assert row.status == "PENDING"
    assert row.recommendation_id == offer.recommendation_id
    spent_sql = "SELECT budget_spent FROM cashback_campaigns WHERE campaign_id = :c"
    assert await _scalar(engine, spent_sql, c=offer.campaign_id) == Decimal("75.00")
    # Offer is consumed: the hot-path key is gone after the commit.
    assert await redis.get(offer.key) is None

    # ---- the DEL was lost: the key is back with the same offer ----------
    await redis.setex(offer.key, 3600, offer.payload)
    second = transaction_event(user_id=offer.user_id, mcc_code="5411", amount="1500.00")
    await _publish(kafka_bootstrap, schema_registry_url, listener.transactions_topic, second)

    async def _key_gone() -> bool:
        return await redis.exists(offer.key) == 0

    assert await _wait_for(_key_gone), "the listener did not process the second purchase"
    assert await _accrual(engine, second["transaction_id"], offer.campaign_id) is None
    assert await _scalar(engine, "SELECT count(*) FROM cashback_accruals "
                                 "WHERE recommendation_id = :r",
                         r=offer.recommendation_id) == 1
    assert await _scalar(engine, spent_sql, c=offer.campaign_id) == Decimal("75.00")


async def test_purchase_below_the_category_minimum_is_not_accrued(
    listener, stores, kafka_bootstrap, schema_registry_url,
):
    """B21: the category's minimum (1000 ₽) beats the campaign's (100 ₽).

    A 500 ₽ purchase pays nothing and does not spend the offer: the next
    purchase of 1500 ₽ on the same partition (key = user_id) still pays.
    """
    engine, redis = stores
    offer = await _accepted_offer(engine, redis, listener, mcc="5912", rate="4.00",
                                  category_min="1000")
    small = transaction_event(user_id=offer.user_id, mcc_code="5912", amount="500.00")
    big = transaction_event(user_id=offer.user_id, mcc_code="5912", amount="1500.00")
    await _publish(kafka_bootstrap, schema_registry_url, listener.transactions_topic,
                   small, big)

    row = await _wait_for(lambda: _accrual(engine, big["transaction_id"], offer.campaign_id))
    assert row is not None, "the purchase above the category minimum was not accrued"
    assert row.cashback_amount == Decimal("60.00")
    assert row.recommendation_id == offer.recommendation_id
    assert await _accrual(engine, small["transaction_id"], offer.campaign_id) is None
    assert await _scalar(engine, "SELECT count(*) FROM cashback_accruals "
                                 "WHERE campaign_id = :c", c=offer.campaign_id) == 1
    assert await redis.get(offer.key) is None


async def test_json_transaction_is_dead_lettered_and_the_partition_moves_on(
    listener, stores, kafka_bootstrap, schema_registry_url,
):
    """The old e2e published JSON: it can never be decoded as Avro.

    The listener publishes it, unchanged, to the DLQ (the error in the
    headers) and commits past it: an Avro event published after it is
    accrued.
    """
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

    engine, redis = stores
    offer = await _accepted_offer(engine, redis, listener, mcc="5812", rate="3.00")
    as_json = transaction_event(user_id=offer.user_id, mcc_code="5812", amount="1000.00")
    as_avro = transaction_event(user_id=offer.user_id, mcc_code="5812", amount="1000.00")
    raw_json = json.dumps(as_json).encode("utf-8")

    producer = AIOKafkaProducer(bootstrap_servers=kafka_bootstrap)
    await producer.start()
    try:
        await producer.send_and_wait(listener.transactions_topic, raw_json,
                                     key=offer.user_id.encode("utf-8"))
    finally:
        await producer.stop()
    await _publish(kafka_bootstrap, schema_registry_url, listener.transactions_topic,
                   as_avro)

    row = await _wait_for(lambda: _accrual(engine, as_avro["transaction_id"],
                                           offer.campaign_id))
    assert row is not None, "the Avro event published after the JSON one was not accrued"
    assert row.cashback_amount == Decimal("30.00")
    assert await _scalar(engine, "SELECT count(*) FROM cashback_accruals "
                                 "WHERE transaction_id = :t",
                         t=as_json["transaction_id"]) == 0

    consumer = AIOKafkaConsumer(listener.dlq_topic, bootstrap_servers=kafka_bootstrap,
                                group_id=f"it-dlq-reader-{uuid.uuid4().hex[:8]}",
                                auto_offset_reset="earliest", enable_auto_commit=False)
    await consumer.start()
    try:
        batch = await consumer.getmany(timeout_ms=int(ACCRUAL_WAIT_S * 1000))
    finally:
        await consumer.stop()
    dead = [msg for msgs in batch.values() for msg in msgs]
    assert [msg.value for msg in dead] == [raw_json]
    headers = dict(dead[0].headers)
    assert headers["dlq.source.topic"].decode() == listener.transactions_topic
    assert headers["dlq.error"].decode().startswith("PermanentMessageError")
