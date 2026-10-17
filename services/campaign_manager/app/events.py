"""SSE-брокер и Kafka-мост для realtime-дашборда (фаза 19; этап B — B14, B36).

Источник событий — существующий топик ``cashback.accrued`` (его пишет
transaction_listener при каждом начислении). Наружу Kafka не выставляется,
поэтому фронт не слушает её напрямую: лёгкий consumer в campaign_manager
ретранслирует агрегаты подписчикам SSE через in-process брокер.

Поток строго односторонний (сервер → браузер), поэтому SSE, а не WebSocket:
проще, работает через nginx из коробки, авто-reconnect в браузере.

Этап B:
* у каждого процесса своя consumer-группа ``campaign-manager-sse-{host}-{pid}``:
  в общей группе Kafka делила партиции между репликами и событие видел
  только один под, а SSE-клиенты подключены ко всем (B36). Оффсеты не
  коммитятся — realtime читает только новое (``latest``);
* consumer живёт под супервизором: неудачный старт или падение → ``stop()``
  и повтор с экспоненциальной паузой 1→30 с; раньше мост умирал навсегда,
  если Kafka поднималась позже сервиса (B14);
* брокер ограничивает число подписчиков процесса и одного пользователя
  (иначе один логин с правом dashboard занимал все места реплики) и
  закрывается при остановке: открытые потоки завершаются сами, не
  дожидаясь таймаута graceful shutdown uvicorn;
* открытый поток раз в ``REVALIDATE_INTERVAL`` перепроверяет доступ
  (учётка активна, ``ver`` текущий, право dashboard) и закрывается, если
  его больше нет: раньше проверка была только при подключении.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import threading
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator

import structlog

log = structlog.get_logger("events")

# Батчинг: агрегируем дельты бюджета за окно и шлём один снапшот, чтобы
# 50 tx/сек не превратились в 50 SSE-сообщений в секунду.
FLUSH_INTERVAL = 2.0
HEARTBEAT_INTERVAL = 15.0
MAX_QUEUE = 100
# Пауза перед повторным подключением consumer'а: 1, 2, 4 … 30 с.
RECONNECT_MIN_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
# Перепроверка доступа открытого потока (запрос по PK + строка роли).
REVALIDATE_INTERVAL = 60.0

# Сигнал потокам «брокер закрыт» — кладётся в очередь каждого подписчика.
_CLOSED = object()


class SubscriptionRefused(RuntimeError):
    """Подписка невозможна: лимит подписчиков исчерпан или процесс
    останавливается (эндпоинт отвечает 503)."""


class TooManyUserSubscriptions(SubscriptionRefused):
    """У пользователя уже открыто максимум потоков (эндпоинт отвечает 429)."""


class EventBroker:
    """Fan-out очередей: одна на активное SSE-соединение."""

    def __init__(self, *, max_subscribers: int | None = None,
                 max_per_user: int | None = None) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._owners: dict[asyncio.Queue, str] = {}
        self._per_user: Counter[str] = Counter()
        self._max_subscribers = max_subscribers
        self._max_per_user = max_per_user
        self._closed = False

    def subscribe(self, owner: str | None = None) -> asyncio.Queue:
        if self._closed:
            raise SubscriptionRefused("server is shutting down")
        if (self._max_subscribers is not None
                and len(self._subscribers) >= self._max_subscribers):
            raise SubscriptionRefused("too many event stream subscribers")
        if (owner is not None and self._max_per_user is not None
                and self._per_user[owner] >= self._max_per_user):
            raise TooManyUserSubscriptions("too many event streams for this user")
        q: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subscribers.add(q)
        if owner is not None:
            self._owners[q] = owner
            self._per_user[owner] += 1
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        # Идемпотентно: отписывают и генератор, и фоновая задача ответа.
        self._subscribers.discard(q)
        owner = self._owners.pop(q, None)
        if owner is not None:
            self._per_user[owner] -= 1
            if not self._per_user[owner]:
                del self._per_user[owner]

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Медленный клиент — роняем событие, не блокируем остальных.
                pass

    def close(self) -> None:
        """Остановка процесса: каждый поток получает сигнал завершения
        (в полной очереди он вытесняет самое старое событие), новые
        подписки — отказ. Идемпотентно."""
        if self._closed:
            return
        self._closed = True
        for q in list(self._subscribers):
            if q.full():
                q.get_nowait()
            q.put_nowait(_CLOSED)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def n_subscribers(self) -> int:
        return len(self._subscribers)


def close_on_shutdown_signal(broker: EventBroker) -> Callable[[], None]:
    """SIGTERM/SIGINT сразу закрывает брокер, затем зовёт прежний обработчик
    (uvicorn). Возвращает функцию, снимающую перехват.

    uvicorn по сигналу перестаёт принимать соединения, ждёт завершения
    открытых (до ``--timeout-graceful-shutdown``) и только ПОСЛЕ этого шлёт
    приложению lifespan shutdown — бесконечный SSE-поток без такого
    перехвата держит остановку до таймаута (B36).
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None          # сигналы ставятся только из главного потока
    loop = asyncio.get_running_loop()
    previous: dict[int, Any] = {}

    def _handler(signum: int, frame: Any) -> None:
        # Обработчик сигнала прерывает цикл посреди любой операции — сам
        # брокер трогаем только из цикла.
        loop.call_soon_threadsafe(broker.close)
        prev = previous[signum]
        if callable(prev):
            prev(signum, frame)
        elif prev == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, _handler)

    def _restore() -> None:
        for sig, prev in previous.items():
            if signal.getsignal(sig) is _handler:
                signal.signal(sig, prev)

    return _restore


def default_group_id() -> str:
    """Consumer-группа процесса: каждая реплика получает все события."""
    return f"campaign-manager-sse-{socket.gethostname()}-{os.getpid()}"


class AccrualStreamBridge:
    """Kafka consumer ``cashback.accrued`` → агрегатор → брокер."""

    def __init__(self, broker: EventBroker, *, bootstrap: str,
                 topic: str = "cashback.accrued",
                 group_id: str | None = None) -> None:
        self._broker = broker
        self._bootstrap = bootstrap
        self._topic = topic
        self._group_id = group_id or default_group_id()
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
        """Супервизор: (пере)подключение consumer'а с экспоненциальной паузой."""
        try:
            from aiokafka import AIOKafkaConsumer
        except Exception as exc:  # noqa: BLE001 — без aiokafka SSE молчит, сервис жив
            log.warning("aiokafka_unavailable_sse_disabled", error=str(exc))
            return

        delay = RECONNECT_MIN_DELAY
        while True:
            consumer = AIOKafkaConsumer(
                self._topic,
                bootstrap_servers=self._bootstrap,
                group_id=self._group_id,
                auto_offset_reset="latest",  # realtime — прошлое не интересно
                enable_auto_commit=False,
            )
            started = False
            try:
                await consumer.start()
                started = True
                delay = RECONNECT_MIN_DELAY
                log.info("sse_consumer_started", topic=self._topic,
                         group_id=self._group_id)
                async for msg in consumer:
                    self._ingest(msg.value)
                log.warning("sse_consumer_stopped")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — нет Kafka → SSE молчит, сервис жив
                log.warning("sse_consumer_failed" if started
                            else "sse_consumer_start_failed",
                            error=str(exc), retry_in=delay)
            finally:
                # И после неудачного start(): иначе «Unclosed AIOKafkaConsumer».
                with contextlib.suppress(Exception):
                    await consumer.stop()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    def _ingest(self, raw: bytes) -> None:
        try:
            event = json.loads(raw.decode("utf-8"))
            cid = str(event.get("campaign_id"))
            amount = float(event.get("cashback_amount") or 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("sse_event_parse_failed", error=str(exc))
            return
        self._pending[cid] = self._pending.get(cid, 0.0) + amount
        self._accepted_delta[cid] = self._accepted_delta.get(cid, 0) + 1

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


async def sse_response_stream(
    broker: EventBroker, q: asyncio.Queue, *,
    revalidate: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[bytes]:
    """Тело SSE для подписки ``q``: события брокера + heartbeat, чтобы
    nginx/прокси не рвали idle-соединение. Завершается, когда брокер
    закрыт или ``revalidate()`` (раз в ``REVALIDATE_INTERVAL``, не позже
    следующего heartbeat) вернул ``False``; отписка — при любом выходе."""
    loop = asyncio.get_running_loop()
    check_at = loop.time() + REVALIDATE_INTERVAL
    try:
        # Первое сообщение — приглашение, чтобы EventSource считался открытым.
        yield b": connected\n\n"
        while True:
            if revalidate is not None and loop.time() >= check_at:
                if not await revalidate():
                    return
                check_at = loop.time() + REVALIDATE_INTERVAL
            try:
                event = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_INTERVAL)
            except TimeoutError:
                yield b": heartbeat\n\n"
                continue
            if event is _CLOSED:
                return
            data = json.dumps(event, separators=(",", ":"))
            yield f"data: {data}\n\n".encode()
    finally:
        broker.unsubscribe(q)
