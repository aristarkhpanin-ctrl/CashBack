"""Unit tests for the SSE broker / stream (phase 19)."""
from __future__ import annotations

import asyncio
import json

import pytest
from app.events import EventBroker, sse_response_stream


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


@pytest.mark.asyncio
async def test_stream_emits_connected_then_event():
    broker = EventBroker()
    gen = sse_response_stream(broker)

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
