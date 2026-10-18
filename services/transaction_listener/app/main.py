"""Entry point — launches the transaction listener AND the notification
consumer as two long-running asyncio tasks.

Lifecycle:
    * loads settings + structured logging
    * connects to Postgres / Redis / Kafka producer
    * starts a Prometheus metrics endpoint on ``METRICS_PORT``
    * runs ``listener.run()`` and ``notification_consumer.run()`` concurrently,
      each under :func:`supervise`: a loop that crashes (Kafka unreachable at
      start, a broker error …) is restarted with a pause of 1 s doubling up
      to 30 s, and never takes the other loop down with it (B45).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from typing import Any, Awaitable, Callable, Optional

import httpx
import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from prometheus_client import Counter, start_http_server
from sqlalchemy.ext.asyncio import create_async_engine

from app.accrual.engine import AccrualEngine
from app.config import Settings, get_settings
from app.listener import TransactionListener
from app.notification.ost import DeliveryScheduler
from app.notification.pipeline import (
    ChannelSelector,
    EmailAdapter,
    InAppAdapter,
    PushAdapter,
    Recommendation,
    SmsAdapter,
    UserPreferences,
)

log = structlog.get_logger("main")


NOTIF_EVENTS = Counter(
    "notification_events_total",
    "Notification events consumed from Kafka",
    ["outcome"],
)
LOOP_RESTARTS = Counter(
    "transaction_listener_loop_restarts_total",
    "Consumer loops restarted by the supervisor",
    ["loop"],
)


# ---------------------------------------------------------------------------
async def _pause(stop: asyncio.Event, seconds: float) -> None:
    """Sleep ``seconds`` or until ``stop`` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def supervise(
    name: str,
    run: Callable[[], Awaitable[None]],
    stop: asyncio.Event,
    *,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
) -> None:
    """Run ``run()`` until ``stop`` is set; restart it with backoff when it
    crashes or returns. The pause resets once a run lasted ``max_delay``."""
    loop = asyncio.get_running_loop()
    delay = initial_delay
    while not stop.is_set():
        started = loop.time()
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — restart, never die silently
            log.error("loop_crashed", loop=name, error=f"{type(exc).__name__}: {exc}",
                      retry_in=delay)
        else:
            if stop.is_set():
                return
            log.warning("loop_exited", loop=name, retry_in=delay)
        LOOP_RESTARTS.labels(loop=name).inc()
        if loop.time() - started >= max_delay:
            delay = initial_delay
        await _pause(stop, delay)
        delay = min(delay * 2, max_delay)


# ---------------------------------------------------------------------------
class NotificationConsumer:
    """Consumes ``recommendations.created`` and dispatches via ChannelSelector."""

    def __init__(
        self,
        settings: Settings,
        selector: ChannelSelector,
        scheduler: DeliveryScheduler,
    ) -> None:
        self._settings = settings
        self._selector = selector
        self._scheduler = scheduler
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._settings.recommendations_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.consumer_group_notify,
            enable_auto_commit=True,
            auto_offset_reset="latest",
            # Raw bytes: a value_deserializer that raises would fail getmany()
            # for the whole batch — a malformed message is skipped in _process.
        )
        await self._consumer.start()
        log.info(
            "notification_consumer_started",
            topic=self._settings.recommendations_topic,
            group=self._settings.consumer_group_notify,
        )

    async def stop(self) -> None:
        """Shutdown: the loop does not restart."""
        self._stop.set()
        await self._close()

    async def _close(self) -> None:
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            with contextlib.suppress(Exception):
                await consumer.stop()

    @staticmethod
    def _to_recommendation(payload: dict) -> Recommendation:
        return Recommendation(
            user_id=str(payload["user_id"]),
            mcc_code=str(payload.get("mcc_code", "")),
            score=float(payload.get("score", 0)),
            campaign_id=payload.get("campaign_id"),
            cashback_rate=payload.get("cashback_rate"),
            campaign_name=payload.get("campaign_name", "Cashback offer"),
            metadata=payload.get("metadata") or {},
        )

    @staticmethod
    def _to_prefs(payload: dict) -> UserPreferences:
        prefs = payload.get("preferences") or {}
        return UserPreferences(
            user_id=str(payload["user_id"]),
            allowed_channels=list(prefs.get("allowed_channels",
                                            ["push", "in_app", "email", "sms"])),
            email=prefs.get("email"),
            phone=prefs.get("phone"),
            push_token=prefs.get("push_token"),
            locale=prefs.get("locale", "ru"),
            name=prefs.get("name"),
        )

    async def _process(self, msg: Any) -> None:
        try:
            payload = json.loads(msg.value.decode("utf-8")) if msg.value else None
        except (AttributeError, UnicodeDecodeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            log.warning("notification_bad_payload", offset=msg.offset)
            NOTIF_EVENTS.labels(outcome="bad_payload").inc()
            return
        try:
            rec = self._to_recommendation(payload)
            prefs = self._to_prefs(payload)
        except KeyError:
            NOTIF_EVENTS.labels(outcome="missing_fields").inc()
            return
        except (TypeError, ValueError, AttributeError):
            log.warning("notification_bad_payload", offset=msg.offset)
            NOTIF_EVENTS.labels(outcome="bad_payload").inc()
            return

        if not await self._scheduler.should_deliver_now(rec.user_id):
            # OST window deferred — drop for now (a Beat/Airflow follow-up
            # would re-pick deferred items). For demo purposes we still
            # deliver via in-app (no user-facing intrusion).
            log.info("deferred_by_ost", user_id=rec.user_id)
            prefs.allowed_channels = [c for c in prefs.allowed_channels
                                      if c == "in_app"]

        chan = await self._selector.deliver(rec, prefs)
        if chan is None:
            NOTIF_EVENTS.labels(outcome="no_channel").inc()
            log.info("no_channel_matched", user_id=rec.user_id)
        else:
            NOTIF_EVENTS.labels(outcome="delivered").inc()
            log.info("delivered", user_id=rec.user_id, channel=chan)

    async def run(self) -> None:
        await self.start()
        try:
            while not self._stop.is_set():
                batch = await self._consumer.getmany(timeout_ms=200, max_records=200)
                for _, msgs in batch.items():
                    for msg in msgs:
                        try:
                            await self._process(msg)
                        except Exception as exc:  # noqa: BLE001 — best effort, per message
                            NOTIF_EVENTS.labels(outcome="error").inc()
                            log.warning("notification_failed", offset=msg.offset,
                                        error=f"{type(exc).__name__}: {exc}")
        finally:
            await self._close()


# ---------------------------------------------------------------------------
async def _build_components(settings: Settings):
    db_engine = create_async_engine(
        settings.postgres_dsn,
        pool_pre_ping=True, pool_size=10, max_overflow=20,
        connect_args={"command_timeout": settings.postgres_command_timeout_s},
    )

    from redis.asyncio import Redis
    redis = Redis.from_url(
        settings.redis_url, decode_responses=True,
        socket_timeout=settings.redis_socket_timeout_s,
        socket_connect_timeout=settings.redis_socket_timeout_s,
    )

    producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        compression_type="zstd",
        linger_ms=20,
        acks=1,
    )
    await producer.start()

    accrual_engine = AccrualEngine(
        db_engine=db_engine,
        kafka_producer=producer,
        redis_client=redis,
        accruals_topic=settings.accruals_topic,
        accepted_offer_key_fmt=settings.accepted_offer_key_fmt,
    )

    http_client = httpx.AsyncClient()
    selector = ChannelSelector([
        PushAdapter(
            http_client=http_client,
            endpoint=settings.fcm_endpoint,
            token=settings.fcm_token,
        ),
        InAppAdapter(redis_client=redis),
        EmailAdapter(
            template_dir=settings.template_dir,
            sent_dir=settings.sent_emails_dir,
        ),
        SmsAdapter(
            http_client=http_client,
            endpoint=settings.sms_endpoint,
        ),
    ])
    scheduler = DeliveryScheduler(
        redis_client=redis,
        key_fmt=settings.ost_key_fmt,
        default_active_hours=settings.ost_default_active_hours,
        max_defer_hours=settings.ost_max_defer_hours,
    )

    # The listener owns its DLQ producer (acks=all): once a dead letter is
    # committed past, the DLQ holds the only copy of that transaction.
    listener = TransactionListener(settings, accrual_engine, redis)
    notif = NotificationConsumer(settings, selector, scheduler)

    async def shutdown():
        log.info("shutting_down")
        await listener.stop()
        await notif.stop()
        for close in (producer.stop, http_client.aclose, redis.aclose,
                      db_engine.dispose):
            with contextlib.suppress(Exception):
                await close()

    return listener, notif, shutdown


async def amain() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    log.info("starting", metrics_port=settings.metrics_port)

    # Prometheus metrics endpoint on /metrics (port 9100).
    start_http_server(settings.metrics_port)

    listener, notif, shutdown = await _build_components(settings)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(*_):
        log.info("signal_received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    await _serve(settings, listener, notif, shutdown, stop_event)


async def _serve(settings: Settings, listener: Any, notif: Any,
                 shutdown: Callable[[], Awaitable[None]], stop_event: asyncio.Event) -> None:
    """Both loops under the supervisor until ``stop_event``; then shut down."""
    backoff = {"initial_delay": settings.retry_backoff_initial_s,
               "max_delay": settings.retry_backoff_max_s}
    listener_task = asyncio.create_task(
        supervise("listener", listener.run, stop_event, **backoff), name="listener")
    notif_task = asyncio.create_task(
        supervise("notification", notif.run, stop_event, **backoff), name="notification")

    done, pending = await asyncio.wait(
        {listener_task, notif_task,
         asyncio.create_task(stop_event.wait(), name="stop")},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()
    await shutdown()
    await asyncio.gather(*pending, return_exceptions=True)
    log.info("stopped")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
