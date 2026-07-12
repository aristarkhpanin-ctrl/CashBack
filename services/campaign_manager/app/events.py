"""SSE-брокер и Kafka-мост для realtime-дашборда (фаза 19).

Источник событий — существующий топик ``cashback.accrued`` (его пишет
transaction_listener при каждом начислении). Наружу Kafka не выставляется,
поэтому фронт не слушает её напрямую: лёгкий consumer в campaign_manager
ретранслирует агрегаты подписчикам SSE через in-process брокер.

Поток строго односторонний (сервер → браузер), поэтому SSE, а не WebSocket:
проще, работает через nginx из коробки, авто-reconnect в браузере.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator

import structlog

log = structlog.get_logger("events")

# Батчинг: агрегируем дельты бюджета за окно и шлём один снапшот, чтобы
# 50 tx/сек не превратились в 50 SSE-сообщений в секунду.
FLUSH_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 15.0
MAX_QUEUE = 100


class EventBroker:
    """Fan-out очередей: одна на активное SSE-соединение."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Медленный клиент — роняем событие, не блокируем остальных.
                pass

    @property
    def n_subscribers(self) -> int:
        return len(self._subscribers)


class AccrualStreamBridge:
    """Kafka consumer ``cashback.accrued`` → агрегатор → брокер."""

    def __init__(self, broker: EventBroker, *, bootstrap: str,
                 topic: str = "cashback.accrued",
                 group_id: str = "campaign-manager-sse") -> None:
        self._broker = broker
        self._bootstrap = bootstrap
        self._topic = topic
        self._group_id = group_id
        self._consumer_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        # campaign_id -> накопленная дельта расхода за окно
        self._pending: dict[str, float] = {}
        self._accepted_delta: dict[str, int] = {}

    async def start(self) -> None:
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        for task in (self._consumer_task, self._flush_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _consume_loop(self) -> None:
        try:
            from aiokafka import AIOKafkaConsumer
        except Exception as exc:  # noqa: BLE001
            log.warning("aiokafka_unavailable_sse_disabled", error=str(exc))
            return

        consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap,
            group_id=self._group_id,
            auto_offset_reset="latest",  # realtime — прошлое не интересно
            enable_auto_commit=True,
        )
        try:
            await consumer.start()
        except Exception as exc:  # noqa: BLE001 — нет Kafka → SSE молчит, сервис жив
            log.warning("sse_consumer_start_failed", error=str(exc))
            return

        log.info("sse_consumer_started", topic=self._topic)
        try:
            async for msg in consumer:
                try:
                    event = json.loads(msg.value.decode("utf-8"))
                    cid = str(event.get("campaign_id"))
                    amount = float(event.get("cashback_amount") or 0)
                    self._pending[cid] = self._pending.get(cid, 0.0) + amount
                    self._accepted_delta[cid] = self._accepted_delta.get(cid, 0) + 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("sse_event_parse_failed", error=str(exc))
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await consumer.stop()

    async def _flush_loop(self) -> None:
        """Раз в FLUSH_INTERVAL шлём накопленные дельты одним батчем."""
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            if not self._pending:
                continue
            for cid, spent_delta in self._pending.items():
                self._broker.publish({
                    "type": "stats",
                    "campaign_id": cid,
                    "spent_delta": round(spent_delta, 2),
                    "accepted_delta": self._accepted_delta.get(cid, 0),
                })
            self._pending.clear()
            self._accepted_delta.clear()


async def sse_response_stream(broker: EventBroker) -> AsyncIterator[bytes]:
    """Генератор тела SSE: события брокера + heartbeat, чтобы nginx/прокси
    не рвали idle-соединение."""
    q = broker.subscribe()
    try:
        # Первое сообщение — приглашение, чтобы EventSource считался открытым.
        yield b": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_INTERVAL)
                data = json.dumps(event, separators=(",", ":"))
                yield f"data: {data}\n\n".encode()
            except TimeoutError:
                yield b": heartbeat\n\n"
    finally:
        broker.unsubscribe(q)
