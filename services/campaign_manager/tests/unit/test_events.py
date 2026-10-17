"""Unit tests for the SSE broker / stream (phase 19, stage B — B36)."""
from __future__ import annotations

import asyncio
import json

import pytest
from app import events as events_module
from app.events import (
    EventBroker,
    SubscriptionRefused,
    TooManyUserSubscriptions,
    sse_response_stream,
)


def test_subscribe_unsubscribe_tracks_count():
    broker = EventBroker()
    assert broker.n_subscribers == 0
    q1 = broker.subscribe()
    q2 = broker.subscribe()
    assert broker.n_subscribers == 2
    broker.unsubscribe(q1)
    assert broker.n_subscribers == 1
    broker.unsubscribe(q2)
    assert broker.n_subscribers == 0


def test_publish_fans_out_to_all_subscribers():
    broker = EventBroker()
    q1, q2 = broker.subscribe(), broker.subscribe()
    broker.publish({"type": "stats", "campaign_id": "c1", "spent_delta": 10})
    assert q1.get_nowait()["campaign_id"] == "c1"
    assert q2.get_nowait()["campaign_id"] == "c1"


def test_publish_drops_for_slow_full_queue_without_blocking():
    broker = EventBroker()
    q = broker.subscribe()
    # Забиваем очередь под завязку — publish не должен кинуть исключение.
    for i in range(1000):
        broker.publish({"type": "stats", "n": i})
    # Очередь ограничена MAX_QUEUE; лишнее молча отброшено, а не зависло.
    assert q.qsize() <= 100


def test_subscriber_cap_refuses_extra_subscribers():
    broker = EventBroker(max_subscribers=2)
    q1, _ = broker.subscribe(), broker.subscribe()
    with pytest.raises(SubscriptionRefused, match="too many"):
        broker.subscribe()
    broker.unsubscribe(q1)                       # место освободилось
    broker.subscribe()
    assert broker.n_subscribers == 2


def test_closed_broker_refuses_new_subscribers():
    broker = EventBroker()
    broker.close()
    broker.close()                               # идемпотентно
    assert broker.closed
    with pytest.raises(SubscriptionRefused, match="shutting down"):
        broker.subscribe()


@pytest.mark.asyncio
async def test_stream_emits_connected_then_event():
    broker = EventBroker()
    gen = sse_response_stream(broker, broker.subscribe())

    first = await gen.__anext__()
    assert first == b": connected\n\n"

    broker.publish({"type": "stats", "campaign_id": "c9", "spent_delta": 42})
    chunk = await asyncio.wait_for(gen.__anext__(), timeout=2)
    assert chunk.startswith(b"data: ")
    payload = json.loads(chunk.decode().removeprefix("data: ").strip())
    assert payload == {"type": "stats", "campaign_id": "c9", "spent_delta": 42}

    # Закрываем генератор — подписчик должен отписаться.
    await gen.aclose()
    assert broker.n_subscribers == 0


@pytest.mark.asyncio
async def test_close_ends_open_streams_even_with_full_queue():
    """Graceful shutdown (B36): закрытие брокера завершает потоки сразу,
    а не через таймаут uvicorn; полная очередь медленного клиента — тоже."""
    broker = EventBroker()
    idle, slow = broker.subscribe(), broker.subscribe()
    for i in range(1000):
        broker.publish({"n": i})
    idle.get_nowait()                            # у idle есть место, у slow — нет
    idle_gen, slow_gen = (sse_response_stream(broker, idle),
                          sse_response_stream(broker, slow))
    assert await idle_gen.__anext__() == b": connected\n\n"

    broker.close()
    tail = [c async for c in idle_gen]           # дочитывает очередь и выходит
    assert len(tail) == 99 and all(c.startswith(b"data: ") for c in tail)
    slow_chunks = [c async for c in slow_gen]
    assert slow_chunks[0] == b": connected\n\n"
    assert len(slow_chunks) == 1 + 99            # самое старое событие вытеснено
    assert broker.n_subscribers == 0


def test_per_user_cap_counts_each_owner_separately():
    broker = EventBroker(max_per_user=2)
    a1, _ = broker.subscribe(owner="a"), broker.subscribe(owner="a")
    with pytest.raises(TooManyUserSubscriptions, match="for this user"):
        broker.subscribe(owner="a")
    broker.subscribe(owner="b")                  # у другого пользователя свой счёт
    broker.unsubscribe(a1)
    broker.unsubscribe(a1)                       # повторная отписка не «дарит» место
    broker.subscribe(owner="a")
    with pytest.raises(TooManyUserSubscriptions):
        broker.subscribe(owner="a")
    assert broker.n_subscribers == 3


@pytest.mark.asyncio
async def test_stream_ends_when_revalidation_fails(monkeypatch):
    monkeypatch.setattr(events_module, "REVALIDATE_INTERVAL", 0.0)
    monkeypatch.setattr(events_module, "HEARTBEAT_INTERVAL", 0.01)
    verdicts = [True, True, False]
    checks = []

    async def revalidate() -> bool:
        checks.append(1)
        return verdicts[len(checks) - 1]

    broker = EventBroker()
    chunks = [c async for c in sse_response_stream(broker, broker.subscribe(),
                                                   revalidate=revalidate)]
    assert chunks[0] == b": connected\n\n"
    assert len(checks) == 3 and chunks[1:] == [b": heartbeat\n\n"] * 2
    assert broker.n_subscribers == 0
