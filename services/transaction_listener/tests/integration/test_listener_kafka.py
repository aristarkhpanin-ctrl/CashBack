"""TransactionListener on real Kafka + Schema Registry + Postgres + Redis (B45).

The whole money path of the service: Avro TransactionEvent → the listener
(the production class and AccrualEngine) → the ledger in Postgres at alembic
head. Proves, live:

* ``auto_offset_reset='earliest'``: a purchase published before the group
  ever committed is accrued (with 'latest' it was silently skipped);
* single use: a second purchase on the same offer, and the whole topic
  re-read by a new consumer group, pay nothing more;
* permanent failures (not Avro, unparsable ``transaction_date``) go to the
  DLQ topic with the raw bytes and the error, the partition moves on;
* a Postgres outage in the middle of a batch loses nothing: the done prefix
  is committed, the failed offset is re-read with backoff and every
  purchase is paid exactly once after recovery;
* 20 purchases consumed concurrently by 4 listeners never exceed the
  campaign's daily limit.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from app.accrual.engine import AccrualEngine
from app.config import Settings
from app.listener import RETRIES, TransactionListener

from tests.integration.kafka_io import (
    committed,
    create_topic,
    publish_avro,
    publish_raw,
    read_all,
    transaction_event,
)
from tests.integration.seed import Seed

pytestmark = pytest.mark.integration

WAIT_S = 60.0


async def _eventually(probe, *, timeout: float = WAIT_S, interval: float = 0.2) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await probe()
        if value or loop.time() >= deadline:
            return value
        await asyncio.sleep(interval)


class Stack:
    """Settings of one test (own topics / group) + running listeners."""

    def __init__(self, kafka_stack, pg_dsn: str, redis_url: str, seed: Seed) -> None:
        self.bootstrap, self.registry = kafka_stack
        suffix = uuid.uuid4().hex[:10]
        self.settings = Settings(
            kafka_bootstrap_servers=self.bootstrap,
            schema_registry_url=self.registry,
            transactions_topic=f"transactions.raw.it-{suffix}",
            accruals_topic=f"cashback.accrued.it-{suffix}",
            consumer_group_accrual=f"cashback-accrual-it-{suffix}",
            dlq_topic=f"transactions.accrual.dlq.it-{suffix}",
            postgres_dsn=pg_dsn,
            redis_url=redis_url,
            retry_backoff_initial_s=0.2,
            retry_backoff_max_s=1.0,
        )
        self.seed = seed
        self.listeners: list[TransactionListener] = []
        self._tasks: list[asyncio.Task] = []
        self._closers: list[Any] = []

    async def start_listener(self, *, group: str | None = None,
                             wrap=None) -> TransactionListener:
        from aiokafka import AIOKafkaProducer
        from redis.asyncio import Redis
        from sqlalchemy.ext.asyncio import create_async_engine

        settings = self.settings
        if group is not None:
            settings = settings.model_copy(update={"consumer_group_accrual": group})
        # As app.main builds them.
        db = create_async_engine(
            settings.postgres_dsn, pool_pre_ping=True,
            connect_args={"command_timeout": settings.postgres_command_timeout_s})
        redis = Redis.from_url(settings.redis_url, decode_responses=True,
                               socket_timeout=settings.redis_socket_timeout_s)
        producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap,
                                    compression_type="zstd")
        await producer.start()
        engine = AccrualEngine(db, producer, redis,
                               accruals_topic=settings.accruals_topic,
                               accepted_offer_key_fmt=settings.accepted_offer_key_fmt)
        listener = TransactionListener(settings, wrap(engine) if wrap else engine, redis)
        self.listeners.append(listener)
        self._tasks.append(asyncio.create_task(listener.run()))
        self._closers += [producer.stop, redis.aclose, db.dispose]
        return listener

    async def publish(self, *events: dict, partition: int | None = None) -> None:
        await publish_avro(self.bootstrap, self.registry,
                           self.settings.transactions_topic, events, partition=partition)

    async def committed(self, group: str | None = None, partitions: int = 1) -> int:
        offsets = await committed(self.bootstrap,
                                  group or self.settings.consumer_group_accrual,
                                  self.settings.transactions_topic, partitions)
        return sum(o or 0 for o in offsets.values())

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for close in self._closers:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture()
async def stack(kafka_stack, pg_dsn, redis_url, db, redis):
    s = Stack(kafka_stack, pg_dsn, redis_url, Seed(db, redis))
    await create_topic(s.bootstrap, s.settings.transactions_topic)
    try:
        yield s
    finally:
        await s.close()


async def _wait_accruals(seed: Seed, campaign_id: str, n: int, timeout: float = WAIT_S):
    async def probe():
        rows = await seed.accruals(campaign_id=campaign_id)
        return rows if len(rows) >= n else None
    return await _eventually(probe, timeout=timeout) or await seed.accruals(
        campaign_id=campaign_id)


async def _key_gone(seed: Seed, key: str) -> bool:
    """The key is deleted right after the commit, not atomically with it."""
    async def probe():
        return await seed.redis.get(key) is None
    return await _eventually(probe, timeout=10.0)


async def _wait_committed(stack: Stack, n: int, *, group: str | None = None,
                          partitions: int = 1) -> int:
    async def probe():
        return await stack.committed(group, partitions) >= n
    await _eventually(probe)
    return await stack.committed(group, partitions)


# ---------------------------------------------------------------------------
async def test_purchase_published_before_the_group_existed_is_accrued(stack):
    """earliest: the event is on the topic before the listener ever ran."""
    seed = stack.seed
    cid = await seed.campaign(rate="5.00")
    offer = await seed.accepted_offer(cid)
    event = transaction_event(user_id=offer.user_id, amount="1500.00")
    await stack.publish(event)

    await stack.start_listener()

    [row] = await _wait_accruals(seed, cid, 1)
    assert row.transaction_id == event["transaction_id"]
    assert row.recommendation_id == offer.recommendation_id
    assert row.cashback_amount == Decimal("75.00")
    assert await seed.budget_spent(cid) == Decimal("75.00")
    assert await _key_gone(seed, offer.key)
    [published] = await read_all(stack.bootstrap, stack.settings.accruals_topic, expect=1)
    body = json.loads(published.value)
    assert body["recommendation_id"] == offer.recommendation_id
    assert body["transaction_at"] == event["transaction_date"]
    assert await _wait_committed(stack, 1) == 1


async def test_same_offer_is_paid_once_even_when_the_topic_is_replayed(stack):
    seed = stack.seed
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)
    first = transaction_event(user_id=offer.user_id)
    await stack.start_listener()
    await stack.publish(first)
    assert len(await _wait_accruals(seed, cid, 1)) == 1

    # The DEL after the commit "was lost": the key says "accepted" again.
    await seed.activate(offer.user_id, offer.mcc, cid, offer.recommendation_id)
    second = transaction_event(user_id=offer.user_id, amount="5000.00")
    await stack.publish(second)
    assert await _wait_committed(stack, 2) == 2
    # … and every message is redelivered to a brand-new group.
    replay_group = f"{stack.settings.consumer_group_accrual}-replay"
    await stack.start_listener(group=replay_group)
    assert await _wait_committed(stack, 2, group=replay_group) == 2

    rows = await seed.accruals(campaign_id=cid)
    assert [r.transaction_id for r in rows] == [first["transaction_id"]]
    assert await seed.budget_spent(cid) == Decimal("75.00")
    assert await _key_gone(seed, offer.key)


async def test_permanent_failures_go_to_the_dlq_and_the_partition_moves_on(stack):
    seed = stack.seed
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)
    as_json = json.dumps(transaction_event(user_id=offer.user_id)).encode("utf-8")
    bad_date = transaction_event(user_id=offer.user_id, transaction_date="yesterday")
    good = transaction_event(user_id=offer.user_id)
    await publish_raw(stack.bootstrap, stack.settings.transactions_topic, as_json,
                      key=b"k-json")
    await stack.publish(bad_date)
    await stack.publish(good)

    await stack.start_listener()

    [row] = await _wait_accruals(seed, cid, 1)
    assert row.transaction_id == good["transaction_id"]
    assert await _wait_committed(stack, 3) == 3
    dead = await read_all(stack.bootstrap, stack.settings.dlq_topic, expect=2)
    assert len(dead) == 2
    assert dead[0].value == as_json and dead[0].key == b"k-json"
    headers = [dict(d.headers) for d in dead]
    assert b"wire format" in headers[0]["dlq.error"]
    assert b"transaction_date" in headers[1]["dlq.error"]
    assert [h["dlq.source.offset"] for h in headers] == [b"0", b"1"]
    assert {h["dlq.source.topic"] for h in headers} == {
        stack.settings.transactions_topic.encode()}


async def test_legacy_offer_without_recommendation_id_is_not_paid(stack):
    seed = stack.seed
    cid = await seed.campaign()
    user_id = await seed.user()
    rec_id = await seed.recommendation(user_id, cid)
    key = await seed.activate(user_id, "5411", cid, None)   # pre-stage-C payload
    await stack.start_listener()
    await stack.publish(transaction_event(user_id=user_id))

    assert await _wait_committed(stack, 1) == 1
    assert await seed.accruals(campaign_id=cid) == []
    assert await seed.accruals(recommendation_id=rec_id) == []
    assert await seed.redis.get(key) is not None


async def test_postgres_outage_mid_batch_loses_no_accrual(stack, pg_outage):
    seed = stack.seed
    cid = await seed.campaign()
    offers = [await seed.accepted_offer(cid) for _ in range(6)]
    events = [transaction_event(user_id=o.user_id) for o in offers]
    await stack.publish(*events)              # one partition, one batch
    retries_before = RETRIES._value.get()
    cut = asyncio.Event()

    class CutBeforeThird:
        def __init__(self, engine: AccrualEngine) -> None:
            self.engine, self.calls = engine, 0

        async def accrue(self, **kwargs):
            self.calls += 1
            if self.calls == 3:
                await asyncio.to_thread(pg_outage.cut)
                cut.set()
            return await self.engine.accrue(**kwargs)

    await stack.start_listener(wrap=CutBeforeThird)
    await asyncio.wait_for(cut.wait(), WAIT_S)

    # Mid-batch: the done prefix is committed, the rest is not.
    assert await _wait_committed(stack, 2) == 2
    async def retried_twice():
        return RETRIES._value.get() >= retries_before + 2
    assert await _eventually(retried_twice)
    await asyncio.sleep(2)
    assert await stack.committed() == 2
    pg_outage.restore()

    rows = await _wait_accruals(seed, cid, 6)
    assert sorted(r.transaction_id for r in rows) == sorted(
        e["transaction_id"] for e in events)
    assert await _wait_committed(stack, 6) == 6
    assert await seed.budget_spent(cid) == Decimal("450.00")
    for offer in offers:
        assert await _key_gone(seed, offer.key)


async def test_four_listeners_twenty_purchases_never_exceed_the_daily_limit(stack):
    seed = stack.seed
    topic = f"{stack.settings.transactions_topic}-x4"
    stack.settings = stack.settings.model_copy(update={"transactions_topic": topic})
    await create_topic(stack.bootstrap, topic, partitions=4)
    cid = await seed.campaign(daily_limit="290")      # 3 × 75 fit, a 4th does not
    offers = [await seed.accepted_offer(cid) for _ in range(20)]
    listeners = [await stack.start_listener() for _ in range(4)]

    async def all_assigned():
        return all(lst._consumer is not None and len(lst._consumer.assignment()) == 1
                   for lst in listeners)
    assert await _eventually(all_assigned), "the group did not settle on 4 x 1 partition"
    for i, offer in enumerate(offers):
        await stack.publish(transaction_event(user_id=offer.user_id), partition=i % 4)

    assert await _wait_committed(stack, 20, partitions=4) == 20
    rows = await seed.accruals(campaign_id=cid)
    assert len(rows) == 3
    assert sum(r.cashback_amount for r in rows) == Decimal("225.00")
    assert await seed.budget_spent(cid) == Decimal("225.00")
