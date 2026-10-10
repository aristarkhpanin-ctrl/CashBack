"""Kafka-мост SSE и эндпоинт /events/stream (фаза 19).

aiokafka подменяется фейковым модулем в ``sys.modules`` — мост импортирует
его лениво внутри ``_consume_loop``. Эндпоинт гоняется через настоящий ASGI
стек приложения (все middleware), с ручным ``receive``: ``TestClient`` не
умеет бесконечные стримы — он ждёт конца ответа до отдачи первого байта.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from app import events
from app.events import AccrualStreamBridge, EventBroker, sse_response_stream


class _Msg:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _FakeConsumer:
    """Повторяет нужный мосту контракт AIOKafkaConsumer."""

    def __init__(self, messages, *, fail_start=False):
        self._messages = messages
        self._fail_start = fail_start
        self.args: tuple = ()
        self.kwargs: dict = {}
        self.started = self.stopped = False

    def __call__(self, *args, **kwargs):            # AIOKafkaConsumer(...)
        self.args, self.kwargs = args, kwargs
        return self

    async def start(self):
        if self._fail_start:
            raise ConnectionError("kafka unreachable")
        self.started = True

    async def stop(self):
        self.stopped = True

    async def _iter(self):
        for m in self._messages:
            yield m
        await asyncio.Event().wait()                # как живой consumer: ждём новых

    def __aiter__(self):
        return self._iter()


def _install_fake_kafka(monkeypatch, consumer):
    mod = types.ModuleType("aiokafka")
    mod.AIOKafkaConsumer = consumer
    monkeypatch.setitem(sys.modules, "aiokafka", mod)


def _accrual(cid, amount):
    return _Msg(json.dumps({"campaign_id": cid, "cashback_amount": amount}).encode())


async def _drain(q: asyncio.Queue, timeout: float) -> list[dict]:
    out = []
    try:
        while True:
            out.append(await asyncio.wait_for(q.get(), timeout=timeout))
    except TimeoutError:
        return out


# ── AccrualStreamBridge ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bridge_aggregates_accruals_into_batched_stats(monkeypatch):
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    consumer = _FakeConsumer([
        _accrual("c1", 10.5), _accrual("c2", 1), _Msg(b"{broken json"),
        _accrual("c1", 4.25),
    ])
    _install_fake_kafka(monkeypatch, consumer)
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")

    await bridge.start()
    got = await _drain(q, timeout=0.3)
    await bridge.stop()

    assert consumer.args == ("cashback.accrued",)
    assert consumer.kwargs["bootstrap_servers"] == "kafka:9092"
    assert consumer.kwargs["group_id"] == "campaign-manager-sse"
    assert consumer.kwargs["auto_offset_reset"] == "latest"
    assert consumer.started and consumer.stopped      # stop() закрыл consumer

    assert got and all(e["type"] == "stats" for e in got)
    spent, accepted = {}, {}
    for e in got:                                     # окна могли разбить батч
        spent[e["campaign_id"]] = spent.get(e["campaign_id"], 0) + e["spent_delta"]
        accepted[e["campaign_id"]] = (accepted.get(e["campaign_id"], 0)
                                      + e["accepted_delta"])
    # битое сообщение пропущено, остальные посчитаны
    assert spent == {"c1": pytest.approx(14.75), "c2": pytest.approx(1.0)}
    assert accepted == {"c1": 2, "c2": 1}


@pytest.mark.asyncio
async def test_bridge_flush_is_silent_without_events(monkeypatch):
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    _install_fake_kafka(monkeypatch, _FakeConsumer([]))
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")
    await bridge.start()
    await asyncio.sleep(0.05)                         # несколько пустых окон
    await bridge.stop()
    assert q.empty()


@pytest.mark.asyncio
async def test_bridge_tolerates_unreachable_kafka(monkeypatch):
    consumer = _FakeConsumer([], fail_start=True)
    _install_fake_kafka(monkeypatch, consumer)
    bridge = AccrualStreamBridge(EventBroker(), bootstrap="nowhere:9092")
    # нет Kafka → SSE молчит, но сервис жив: цикл завершается без исключения
    await asyncio.wait_for(bridge._consume_loop(), timeout=1)
    assert not consumer.started


@pytest.mark.asyncio
async def test_bridge_tolerates_missing_aiokafka(monkeypatch):
    monkeypatch.setitem(sys.modules, "aiokafka", None)   # import → ImportError
    bridge = AccrualStreamBridge(EventBroker(), bootstrap="kafka:9092")
    await asyncio.wait_for(bridge._consume_loop(), timeout=1)


@pytest.mark.asyncio
async def test_bridge_stop_before_start_is_noop():
    await AccrualStreamBridge(EventBroker(), bootstrap="kafka:9092").stop()


# ── sse_response_stream ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_stream_sends_heartbeat_when_idle(monkeypatch):
    monkeypatch.setattr(events, "HEARTBEAT_INTERVAL", 0.01)
    broker = EventBroker()
    gen = sse_response_stream(broker)
    assert await gen.__anext__() == b": connected\n\n"
    assert await asyncio.wait_for(gen.__anext__(), timeout=1) == b": heartbeat\n\n"
    await gen.aclose()
    assert broker.n_subscribers == 0


# ── GET /events/stream через ASGI-стек приложения ────────────────────────────
@pytest.mark.asyncio
async def test_events_endpoint_streams_uncompressed_sse(app):
    broker: EventBroker = app.state.event_broker
    sent: list[dict] = []
    got_event = asyncio.Event()
    request_delivered = False

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await got_event.wait()                        # клиент «уходит» после события
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and b"data:" in message.get("body", b""):
            got_event.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/events/stream",
        "raw_path": b"/events/stream", "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept-encoding", b"gzip")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    for _ in range(200):
        if broker.n_subscribers:
            break
        await asyncio.sleep(0.01)
    assert broker.n_subscribers == 1
    broker.publish({"type": "stats", "campaign_id": "c7", "spent_delta": 3.5})
    await asyncio.wait_for(task, timeout=5)

    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in headers          # gzip не буферизует SSE
    assert "x-request-id" in headers
    body = b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body")
    assert body.startswith(b": connected\n\n")
    assert b'data: {"type":"stats","campaign_id":"c7","spent_delta":3.5}\n\n' in body
    assert broker.n_subscribers == 0                  # отписка после разрыва
