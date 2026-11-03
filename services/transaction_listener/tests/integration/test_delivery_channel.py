"""Delivery channel of an offer on real Postgres / Redis / Kafka (stage D, B29).

``recommendations.channel`` is the channel that DELIVERED the offer: the
first NotificationPipeline adapter that dispatched its
``recommendations.created`` event, or the app showing it (mobile_api sets
IN_APP). Proves, live, at alembic head:

* every adapter maps to a value of the ``delivery_channel`` enum;
* only the first delivery is recorded (``channel IS NULL``): a redelivery
  through another adapter, or a delivery after the app showed the offer,
  keeps the recorded channel;
* the production NotificationConsumer (real Kafka topic, real ChannelSelector
  with the stub push adapter and the Redis in-app adapter) writes the channel
  of the adapter that delivered the event into the row the event names; an
  event without ``recommendation_id`` (publisher before B29) is delivered and
  changes no row.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from app.config import Settings
from app.main import NotificationConsumer
from app.notification.delivery import CHANNEL_BY_ADAPTER, DeliveryChannelRecorder
from app.notification.ost import DeliveryScheduler
from app.notification.pipeline import ChannelSelector, InAppAdapter, PushAdapter

from tests.integration.kafka_io import create_topic, publish_raw
from tests.integration.seed import Seed

pytestmark = pytest.mark.integration

WAIT_S = 60.0


async def _offer(seed: Seed, *, channel: str | None = None) -> tuple[str, str]:
    campaign_id = await seed.campaign()
    user_id = await seed.user()
    rec_id = await seed.recommendation(user_id, campaign_id, status="PENDING",
                                       channel=channel)
    return user_id, rec_id


# ---------------------------------------------------------------------------
async def test_every_adapter_maps_to_a_delivery_channel_value(db, redis):
    seed = Seed(db, redis)
    recorder = DeliveryChannelRecorder(db)
    for adapter, channel in CHANNEL_BY_ADAPTER.items():
        _, rec_id = await _offer(seed)
        assert await recorder.record(rec_id, adapter) is True, adapter
        assert await seed.channel(rec_id) == channel


async def test_only_the_first_delivery_channel_is_recorded(db, redis):
    seed = Seed(db, redis)
    recorder = DeliveryChannelRecorder(db)

    _, pushed = await _offer(seed)
    assert await recorder.record(pushed, "push") is True
    assert await recorder.record(pushed, "in_app") is False   # redelivery
    assert await seed.channel(pushed) == "PUSH"

    # The app showed the offer before the pipeline delivered it.
    _, shown = await _offer(seed, channel="IN_APP")
    assert await recorder.record(shown, "email") is False
    assert await seed.channel(shown) == "IN_APP"

    assert await recorder.record(str(uuid.uuid4()), "push") is False   # no such row
    assert await recorder.record(None, "push") is False


# ---------------------------------------------------------------------------
class _Consumer:
    """The production NotificationConsumer on its own topic / group."""

    def __init__(self, bootstrap: str, pg_dsn: str, redis_url: str) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.settings = Settings(
            kafka_bootstrap_servers=bootstrap,
            recommendations_topic=f"recommendations.created.it-{suffix}",
            consumer_group_notify=f"cashback-notify-it-{suffix}",
            postgres_dsn=pg_dsn,
            redis_url=redis_url,
            # Every hour is active: OST never narrows the chain to in-app.
            ost_default_active_hours=list(range(24)),
        )
        self.bootstrap = bootstrap
        self._closers: list[Any] = []
        self._task: asyncio.Task | None = None
        self.notif: NotificationConsumer | None = None

    async def start(self) -> None:
        from redis.asyncio import Redis
        from sqlalchemy.ext.asyncio import create_async_engine

        settings = self.settings
        await create_topic(self.bootstrap, settings.recommendations_topic)
        db = create_async_engine(settings.postgres_dsn, pool_pre_ping=True)
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self._closers += [redis.aclose, db.dispose]
        # As app.main wires them (push without an FCM endpoint = stub mode).
        selector = ChannelSelector([PushAdapter(), InAppAdapter(redis_client=redis)])
        scheduler = DeliveryScheduler(
            redis_client=redis, key_fmt=settings.ost_key_fmt,
            default_active_hours=settings.ost_default_active_hours,
            max_defer_hours=settings.ost_max_defer_hours)
        self.notif = NotificationConsumer(settings, selector, scheduler,
                                          DeliveryChannelRecorder(db))
        self._task = asyncio.create_task(self.notif.run())
        # auto_offset_reset='latest': publish only once the group owns the partition.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + WAIT_S
        while loop.time() < deadline:
            consumer = self.notif._consumer
            if consumer is not None and consumer.assignment():
                for tp in consumer.assignment():
                    await consumer.position(tp)   # the 'latest' offset is resolved
                return
            await asyncio.sleep(0.2)
        raise AssertionError("notification consumer got no partition")

    async def publish(self, payload: dict) -> None:
        await publish_raw(self.bootstrap, self.settings.recommendations_topic,
                          json.dumps(payload).encode("utf-8"),
                          key=payload["user_id"].encode("utf-8"))

    async def close(self) -> None:
        if self.notif is not None:
            await self.notif.stop()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        for close in self._closers:
            try:
                await close()
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture()
async def consumer(kafka_stack, pg_dsn, redis_url):
    c = _Consumer(kafka_stack[0], pg_dsn, redis_url)
    try:
        await c.start()
        yield c
    finally:
        await c.close()


def _event(user_id: str, rec_id: str | None, *, push_token: str | None) -> dict:
    """``recommendations.created`` as recommendation_api publishes it (B29)."""
    payload = {
        "user_id": user_id, "mcc_code": "5411", "campaign_id": str(uuid.uuid4()),
        "score": 0.8, "cashback_rate": 5.0, "campaign_name": "Продукты",
        "model_version": "1",
        "preferences": {"allowed_channels": ["push", "in_app", "email"]},
    }
    if rec_id is not None:
        payload["recommendation_id"] = rec_id
    if push_token is not None:
        payload["preferences"]["push_token"] = push_token
    return payload


async def _wait_channel(seed: Seed, rec_id: str) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_S
    while loop.time() < deadline:
        channel = await seed.channel(rec_id)
        if channel is not None:
            return channel
        await asyncio.sleep(0.2)
    return None


async def test_notification_consumer_records_the_delivering_channel(consumer, db, redis):
    seed = Seed(db, redis)
    push_user, pushed = await _offer(seed)
    app_user, in_app = await _offer(seed)

    # A push token: the push adapter (first in the chain) delivers.
    await consumer.publish(_event(push_user, pushed, push_token="fcm-token"))
    # No push token: push cannot handle it, the in-app adapter delivers.
    await consumer.publish(_event(app_user, in_app, push_token=None))

    assert await _wait_channel(seed, pushed) == "PUSH"
    assert await _wait_channel(seed, in_app) == "IN_APP"
    assert await redis.llen(f"in_app_queue:{app_user}") == 1


async def test_event_without_recommendation_id_changes_no_row(consumer, db, redis):
    seed = Seed(db, redis)
    user_id, rec_id = await _offer(seed)
    marker_user, marker = await _offer(seed)

    await consumer.publish(_event(user_id, None, push_token=None))
    # A later event on the same partition: once it is recorded, the first
    # one has been processed too.
    await consumer.publish(_event(marker_user, marker, push_token=None))

    assert await _wait_channel(seed, marker) == "IN_APP"
    assert await redis.llen(f"in_app_queue:{user_id}") == 1   # it was delivered
    assert await seed.channel(rec_id) is None
