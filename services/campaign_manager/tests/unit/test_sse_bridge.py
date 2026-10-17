"""Kafka-мост SSE и эндпоинты /events/* (фаза 19; этап B — B14, B36).

aiokafka подменяется фейковым модулем в ``sys.modules`` — мост импортирует
его лениво внутри ``_consume_loop``. Эндпоинт гоняется через настоящий ASGI
стек приложения (все middleware), с ручным ``receive``: ``TestClient`` не
умеет бесконечные стримы — он ждёт конца ответа до отдачи первого байта.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import threading
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app import events
from app.api.events import stream
from app.config import get_settings
from app.events import (
    AccrualStreamBridge,
    EventBroker,
    close_on_shutdown_signal,
)
from app.models import AdminUser, RolePermission
from app.security import SSE_AUDIENCE, AuthUser, issue_sse_token, make_access_token
from jose import jwt
from starlette.requests import Request
from structlog.testing import capture_logs

from tests.unit.conftest import DEFAULT_USER_IDS


class _Msg:
    def __init__(self, value: bytes) -> None:
        self.value = value


class _FakeConsumer:
    """Повторяет нужный мосту контракт AIOKafkaConsumer."""

    def __init__(self, messages, *, fail_start=False, fail_after=False, ends=False):
        self._messages = messages
        self._fail_start = fail_start
        self._fail_after = fail_after
        self._ends = ends
        self.args: tuple = ()
        self.kwargs: dict = {}
        self.started = self.stopped = False

    async def start(self):
        if self._fail_start:
            raise ConnectionError("kafka unreachable")
        self.started = True

    async def stop(self):
        self.stopped = True

    async def _iter(self):
        for m in self._messages:
            yield m
        if self._fail_after:
            raise RuntimeError("broker connection lost")
        if self._ends:                              # consumer закрыли извне
            return
        await asyncio.Event().wait()                # как живой consumer: ждём новых

    def __aiter__(self):
        return self._iter()


class _ConsumerFactory:
    """``AIOKafkaConsumer(...)``: каждый вызов — новый consumer по сценарию.
    Элемент сценария — ``"fail"`` (start() падает), ``"drop"`` (падает после
    старта), ``"end"`` (итерация закончилась) или список сообщений."""

    def __init__(self, *script):
        self._script = list(script)
        self.created: list[_FakeConsumer] = []

    def __call__(self, *args, **kwargs):
        step = self._script.pop(0) if self._script else []
        consumer = _FakeConsumer(step if isinstance(step, list) else [],
                                 fail_start=step == "fail", fail_after=step == "drop",
                                 ends=step == "end")
        consumer.args, consumer.kwargs = args, kwargs
        self.created.append(consumer)
        return consumer


def _install_fake_kafka(monkeypatch, factory):
    mod = types.ModuleType("aiokafka")
    mod.AIOKafkaConsumer = factory
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


async def _until(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# ── AccrualStreamBridge ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bridge_aggregates_accruals_into_batched_stats(monkeypatch):
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    factory = _ConsumerFactory([
        _accrual("c1", 10.5), _accrual("c2", 1), _Msg(b"{broken json"),
        _accrual("c1", 4.25),
    ])
    _install_fake_kafka(monkeypatch, factory)
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")

    await bridge.start()
    got = await _drain(q, timeout=0.3)
    await bridge.stop()

    (consumer,) = factory.created
    assert consumer.args == ("cashback.accrued",)
    assert consumer.kwargs["bootstrap_servers"] == "kafka:9092"
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
async def test_bridge_uses_own_consumer_group_per_process(monkeypatch):
    """B36: с общей группой Kafka делила партиции между репликами и событие
    видел SSE-клиент только одного пода. Оффсеты не коммитятся."""
    factory = _ConsumerFactory([])
    _install_fake_kafka(monkeypatch, factory)
    bridge = AccrualStreamBridge(EventBroker(), bootstrap="kafka:9092")
    await bridge.start()
    await _until(lambda: factory.created and factory.created[0].started)
    await bridge.stop()

    kwargs = factory.created[0].kwargs
    assert kwargs["group_id"] == (
        f"campaign-manager-sse-{socket.gethostname()}-{os.getpid()}")
    assert kwargs["enable_auto_commit"] is False
    assert kwargs["auto_offset_reset"] == "latest"


def test_group_id_differs_between_processes(monkeypatch):
    first, other_pid = events.default_group_id(), os.getpid() + 1
    monkeypatch.setattr(events.os, "getpid", lambda: other_pid)
    assert events.default_group_id() != first


@pytest.mark.asyncio
async def test_bridge_flush_is_silent_without_events(monkeypatch):
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    _install_fake_kafka(monkeypatch, _ConsumerFactory([]))
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")
    await bridge.start()
    await asyncio.sleep(0.05)                         # несколько пустых окон
    await bridge.stop()
    assert q.empty()


def test_reconnect_backoff_defaults():
    assert (events.RECONNECT_MIN_DELAY, events.RECONNECT_MAX_DELAY) == (1.0, 30.0)


@pytest.mark.asyncio
async def test_bridge_retries_start_with_exponential_backoff(monkeypatch):
    """B14: раньше первый же неудачный start() навсегда выключал мост (Kafka
    поднялась позже сервиса → дашборд без realtime до рестарта), а consumer
    не закрывался."""
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    monkeypatch.setattr(events, "RECONNECT_MIN_DELAY", 0.001)
    monkeypatch.setattr(events, "RECONNECT_MAX_DELAY", 0.008)
    factory = _ConsumerFactory(*["fail"] * 6, [_accrual("c1", 5)])
    _install_fake_kafka(monkeypatch, factory)
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")

    with capture_logs() as logs:
        await bridge.start()
        event = await asyncio.wait_for(q.get(), timeout=2)
        await bridge.stop()

    assert event["campaign_id"] == "c1" and event["spent_delta"] == 5
    *failed, live = factory.created
    assert len(failed) == 6
    assert all(c.stopped and not c.started for c in failed)   # stop() после сбоя
    assert live.started and live.stopped
    delays = [e["retry_in"] for e in logs if e["event"] == "sse_consumer_start_failed"]
    assert delays == [0.001, 0.002, 0.004, 0.008, 0.008, 0.008]


@pytest.mark.asyncio
async def test_bridge_reconnects_after_consumer_failure(monkeypatch):
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    monkeypatch.setattr(events, "RECONNECT_MIN_DELAY", 0.001)
    factory = _ConsumerFactory("drop", [_accrual("c2", 1)])
    _install_fake_kafka(monkeypatch, factory)
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")
    with capture_logs() as logs:
        await bridge.start()
        event = await asyncio.wait_for(q.get(), timeout=2)
        await bridge.stop()
    assert event["campaign_id"] == "c2"
    dropped, live = factory.created
    assert dropped.started and dropped.stopped and live.started
    assert [e["error"] for e in logs if e["event"] == "sse_consumer_failed"] == [
        "broker connection lost"]


@pytest.mark.asyncio
async def test_bridge_reconnects_when_consumer_iteration_ends(monkeypatch):
    monkeypatch.setattr(events, "FLUSH_INTERVAL", 0.01)
    monkeypatch.setattr(events, "RECONNECT_MIN_DELAY", 0.001)
    factory = _ConsumerFactory("end", [_accrual("c3", 2)])
    _install_fake_kafka(monkeypatch, factory)
    broker = EventBroker()
    q = broker.subscribe()
    bridge = AccrualStreamBridge(broker, bootstrap="kafka:9092")
    await bridge.start()
    assert (await asyncio.wait_for(q.get(), timeout=2))["campaign_id"] == "c3"
    await bridge.stop()
    assert [c.stopped for c in factory.created] == [True, True]


@pytest.mark.asyncio
async def test_bridge_stop_interrupts_backoff(monkeypatch):
    monkeypatch.setattr(events, "RECONNECT_MIN_DELAY", 60.0)
    factory = _ConsumerFactory("fail")
    _install_fake_kafka(monkeypatch, factory)
    bridge = AccrualStreamBridge(EventBroker(), bootstrap="nowhere:9092")
    await bridge.start()
    await _until(lambda: factory.created and factory.created[0].stopped)
    await asyncio.wait_for(bridge.stop(), timeout=1)  # не ждёт 60 с паузы
    assert len(factory.created) == 1


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
    gen = events.sse_response_stream(broker, broker.subscribe())
    assert await gen.__anext__() == b": connected\n\n"
    assert await asyncio.wait_for(gen.__anext__(), timeout=1) == b": heartbeat\n\n"
    assert await asyncio.wait_for(gen.__anext__(), timeout=1) == b": heartbeat\n\n"
    await gen.aclose()
    assert broker.n_subscribers == 0


# ── SIGTERM закрывает брокер раньше, чем uvicorn дождётся соединений ─────────
@pytest.fixture
def term_handler():
    """Свой обработчик SIGTERM на время теста (как у uvicorn)."""
    received: list[int] = []
    original = signal.signal(signal.SIGTERM, lambda signum, _f: received.append(signum))
    yield received
    signal.signal(signal.SIGTERM, original)


@pytest.mark.asyncio
async def test_shutdown_signal_closes_broker_and_chains_previous_handler(term_handler):
    broker = EventBroker()
    q = broker.subscribe()
    previous = signal.getsignal(signal.SIGTERM)
    restore = close_on_shutdown_signal(broker)
    gen = events.sse_response_stream(broker, q)
    assert await gen.__anext__() == b": connected\n\n"

    signal.raise_signal(signal.SIGTERM)
    assert term_handler == [signal.SIGTERM]       # uvicorn тоже получил сигнал
    assert [c async for c in gen] == []           # поток завершился сам
    assert broker.closed and broker.n_subscribers == 0

    restore()
    assert signal.getsignal(signal.SIGTERM) is previous


@pytest.mark.asyncio
async def test_shutdown_signal_with_default_disposition_still_terminates(
        term_handler, monkeypatch):
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raised: list[int] = []
    monkeypatch.setattr(signal, "raise_signal", raised.append)
    broker = EventBroker()
    restore = close_on_shutdown_signal(broker)
    handler = signal.getsignal(signal.SIGTERM)
    handler(signal.SIGTERM, None)
    # прежнее поведение (завершение процесса) восстановлено и вызвано
    assert raised == [signal.SIGTERM]
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    await asyncio.sleep(0)
    assert broker.closed
    restore()                                     # чужой обработчик не трогает
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL


def test_shutdown_signal_hook_is_noop_outside_main_thread():
    before = signal.getsignal(signal.SIGTERM)
    restores = []
    t = threading.Thread(target=lambda: restores.append(
        close_on_shutdown_signal(EventBroker())))
    t.start()
    t.join()
    restores[0]()
    assert signal.getsignal(signal.SIGTERM) is before


# ── POST /events/token ────────────────────────────────────────────────────────
def test_token_endpoint_issues_short_lived_sse_token(client, auth):
    r = client.post("/events/token", headers=auth("ANALYST"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["expires_in"] == 60
    claims = jwt.decode(body["token"], get_settings().jwt_secret.get_secret_value(),
                        algorithms=["HS256"], audience=SSE_AUDIENCE)
    assert claims["type"] == "sse" and claims["iss"] == "cashback-campaign-manager"
    assert claims["sub"] == str(DEFAULT_USER_IDS["ANALYST"]) and claims["ver"] == 0
    assert claims["exp"] - claims["iat"] == 60


def test_token_endpoint_needs_dashboard_permission(client, db, auth):
    assert client.post("/events/token").status_code == 401
    db.put(RolePermission(role="MARKETER", permissions={"dashboard": False}))
    r = client.post("/events/token", headers=auth("MARKETER"))
    assert r.status_code == 403
    assert r.json()["detail"] == "role MARKETER lacks permission 'dashboard'"


# ── GET /events/stream через ASGI-стек приложения ────────────────────────────
def _scope(query: bytes) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "http", "path": "/events/stream",
        "raw_path": b"/events/stream", "query_string": query, "root_path": "",
        "headers": [(b"host", b"testserver"), (b"accept-encoding", b"gzip")],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
    }


def _sse_token(client, auth, role="ADMIN") -> str:
    return client.post("/events/token", headers=auth(role)).json()["token"]


async def _open_stream(app, token: str, *, leave: asyncio.Event):
    """Запускает GET /events/stream; клиент «уходит», когда ``leave`` взведён."""
    sent: list[dict] = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await leave.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    task = asyncio.create_task(app(_scope(f"token={token}".encode()), receive, send))
    return task, sent


def _body(sent: list[dict]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


@pytest.mark.asyncio
async def test_events_endpoint_streams_uncompressed_sse(app, client, auth):
    broker: EventBroker = app.state.event_broker
    token = _sse_token(client, auth)
    leave = asyncio.Event()
    task, sent = await _open_stream(app, token, leave=leave)
    await _until(lambda: broker.n_subscribers == 1)
    broker.publish({"type": "stats", "campaign_id": "c7", "spent_delta": 3.5})
    await _until(lambda: b"data:" in _body(sent))
    leave.set()                                       # клиент закрыл вкладку
    await asyncio.wait_for(task, timeout=5)

    start = sent[0]
    assert start["type"] == "http.response.start" and start["status"] == 200
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in headers          # gzip не буферизует SSE
    assert "x-request-id" in headers
    body = _body(sent)
    assert body.startswith(b": connected\n\n")
    assert b'data: {"type":"stats","campaign_id":"c7","spent_delta":3.5}\n\n' in body
    assert broker.n_subscribers == 0                  # отписка после разрыва


def _forged(**claims) -> str:
    now = datetime.now(UTC)
    payload = {"iss": "cashback-campaign-manager", "aud": SSE_AUDIENCE, "type": "sse",
               "sub": str(DEFAULT_USER_IDS["ADMIN"]), "ver": 0,
               "jti": uuid.uuid4().hex, "iat": int(now.timestamp()),
               "exp": int((now + timedelta(seconds=60)).timestamp()), **claims}
    return jwt.encode(payload, get_settings().jwt_secret.get_secret_value(),
                      algorithm="HS256")


@pytest.mark.parametrize("query", [
    "", "?token=", "?token=garbage",
    "?token=" + _forged(exp=int(datetime.now(UTC).timestamp()) - 1),
    "?token=" + _forged(aud="cashback-admin"),
    "?token=" + _forged(type="access"),
])
def test_stream_without_valid_sse_token_is_401(client, auth, query):
    """B36: канал был открыт любому, CORS * с credentials отдавал его любому сайту."""
    auth("ADMIN")                                     # пользователь существует
    r = client.get("/events/stream" + query)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_stream_rejects_access_token(client, db, auth):
    auth("ADMIN")
    admin = db.tables[AdminUser][DEFAULT_USER_IDS["ADMIN"]]
    r = client.get("/events/stream", params={"token": make_access_token(admin)})
    assert r.status_code == 401


@pytest.mark.parametrize("change", ["deactivate", "bump_version", "delete"])
def test_stream_token_of_revoked_user_is_401(client, db, auth, change):
    token = _sse_token(client, auth)
    admin = db.tables[AdminUser][DEFAULT_USER_IDS["ADMIN"]]
    if change == "deactivate":
        admin.is_active = False
    elif change == "bump_version":
        admin.token_version += 1                      # смена роли/пароля
    else:
        del db.tables[AdminUser][admin.user_id]
    r = client.get("/events/stream", params={"token": token})
    assert r.status_code == 401


def test_stream_over_subscriber_cap_is_503(app, client, auth):
    app.state.event_broker = broker = EventBroker(max_subscribers=1)
    broker.subscribe()                                # место занято
    r = client.get("/events/stream", params={"token": _sse_token(client, auth)})
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert broker.n_subscribers == 1


@pytest.mark.asyncio
async def test_shutdown_ends_open_stream_and_refuses_new_ones(app, client, auth):
    broker: EventBroker = app.state.event_broker
    token = _sse_token(client, auth)
    task, sent = await _open_stream(app, token, leave=asyncio.Event())
    await _until(lambda: broker.n_subscribers == 1)

    broker.close()                                    # SIGTERM / lifespan shutdown
    await asyncio.wait_for(task, timeout=2)           # клиент не уходил — ответ закончился сам
    assert sent[0]["status"] == 200
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert broker.n_subscribers == 0
    r = client.get("/events/stream", params={"token": _sse_token(client, auth)})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_unstarted_stream_still_unsubscribes(app):
    """Клиент ушёл до первой итерации тела: генератор так и не стартовал —
    отписку делает фоновая задача ответа, иначе «призрак» занял бы место."""
    broker: EventBroker = app.state.event_broker
    request = Request({"type": "http", "app": app, "method": "GET", "headers": [],
                       "path": "/events/stream", "query_string": b""})
    response = await stream(request, AuthUser(user_id=str(DEFAULT_USER_IDS["ADMIN"]),
                                              email="admin@bank.ru", role="ADMIN"))
    assert broker.n_subscribers == 1
    await response.background()
    assert broker.n_subscribers == 0


def test_sse_token_cannot_be_used_as_access_token(client, auth):
    token = issue_sse_token(AuthUser(user_id=str(DEFAULT_USER_IDS["ADMIN"]),
                                     email="admin@bank.ru", role="ADMIN"))
    auth("ADMIN")
    r = client.get("/campaigns", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


# ── доступ к потоку: право dashboard, одноразовый токен, лимит на пользователя ─
async def _connect(app, token: str) -> tuple[int, dict]:
    """Статус и JSON-тело ответа GET /events/stream (у открытого потока тело
    пустое — он сразу закрывается). Не ``TestClient``: тот на открывшемся
    потоке ждал бы конца ответа вечно."""
    leave = asyncio.Event()
    task, sent = await _open_stream(app, token, leave=leave)
    await _until(lambda: any(m["type"] == "http.response.start" for m in sent))
    status = sent[0]["status"]
    if status == 200:
        leave.set()
    await asyncio.wait_for(task, timeout=5)
    return status, ({} if status == 200 else json.loads(_body(sent)))


async def _connect_status(app, token: str) -> int:
    return (await _connect(app, token))[0]


@pytest.mark.asyncio
async def test_stream_rechecks_dashboard_permission_at_connect(app, client, db, auth):
    """Право dashboard сняли после выдачи sse-токена (PATCH /roles не трогает
    token_version): токен, выданный до этого, поток уже не открывает."""
    token = _sse_token(client, auth, role="ANALYST")
    db.put(RolePermission(role="ANALYST", permissions={"dashboard": False}))
    assert await _connect(app, token) == (
        403, {"detail": "role ANALYST lacks permission 'dashboard'"})


@pytest.mark.asyncio
async def test_sse_token_opens_one_stream_only(app, client, redis, auth):
    """Токен из ?token= оседает в логах прокси: повтор из лога — 401."""
    token = _sse_token(client, auth)
    assert await _connect_status(app, token) == 200
    assert await _connect(app, token) == (401, {"detail": "sse token already used"})
    assert await _connect_status(app, _sse_token(client, auth)) == 200   # свежий — можно
    assert {ttl for key, (ttl, _) in redis.data.items()
            if key.startswith("auth:sse_used:")} == {60}


@pytest.mark.asyncio
async def test_stream_fails_closed_without_session_store(app, client, redis, auth):
    token = _sse_token(client, auth)
    redis.fail = True
    assert await _connect(app, token) == (503, {"detail": "session store unavailable"})


@pytest.mark.asyncio
async def test_one_user_cannot_take_every_stream_slot(app, client, auth):
    """Пользователь с dashboard занимал все места процесса — остальные,
    включая ADMIN, получали 503."""
    app.state.event_broker = broker = EventBroker(max_subscribers=10, max_per_user=2)
    analyst = str(DEFAULT_USER_IDS["ANALYST"])
    broker.subscribe(owner=analyst)
    broker.subscribe(owner=analyst)
    r = client.get("/events/stream", params={"token": _sse_token(client, auth, "ANALYST")})
    assert r.status_code == 429                       # отказ — без открытого потока
    assert r.json()["detail"] == "too many event streams for this user"
    assert r.headers["retry-after"] == "5"
    assert await _connect_status(app, _sse_token(client, auth, "ADMIN")) == 200
    assert broker.n_subscribers == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deactivate", "revoke_dashboard", "bump_version"])
async def test_open_stream_ends_when_access_is_lost(app, client, db, auth,
                                                    monkeypatch, change):
    """Открытый поток перепроверяется: отключение учётки, снятие права
    dashboard или смена роли/пароля закрывают его, а не через час."""
    monkeypatch.setattr(events, "REVALIDATE_INTERVAL", 0.05)
    monkeypatch.setattr(events, "HEARTBEAT_INTERVAL", 0.01)
    broker: EventBroker = app.state.event_broker
    token = _sse_token(client, auth, role="ANALYST")
    task, sent = await _open_stream(app, token, leave=asyncio.Event())
    await _until(lambda: broker.n_subscribers == 1)
    await asyncio.sleep(0.15)
    assert not task.done()                            # пока доступ есть — поток жив

    analyst = db.tables[AdminUser][DEFAULT_USER_IDS["ANALYST"]]
    if change == "deactivate":
        analyst.is_active = False
    elif change == "revoke_dashboard":
        db.put(RolePermission(role="ANALYST", permissions={"dashboard": False}))
    else:
        analyst.token_version += 1
    await asyncio.wait_for(task, timeout=2)           # сервер закончил ответ сам
    assert sent[0]["status"] == 200
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert broker.n_subscribers == 0


@pytest.mark.asyncio
async def test_sse_token_without_jti_is_401(app, auth):
    auth("ADMIN")
    assert await _connect(app, _forged(jti="")) == (401, {"detail": "invalid or expired token"})


@pytest.mark.asyncio
async def test_stream_without_redis_client_is_503(app, client, auth):
    token = _sse_token(client, auth)
    app.state.redis = None                            # lifespan не поднял клиента
    assert await _connect(app, token) == (503, {"detail": "session store unavailable"})


@pytest.mark.asyncio
async def test_open_stream_ends_when_the_recheck_cannot_reach_the_db(
        app, client, db, auth, monkeypatch):
    monkeypatch.setattr(events, "REVALIDATE_INTERVAL", 0.05)
    monkeypatch.setattr(events, "HEARTBEAT_INTERVAL", 0.01)
    token = _sse_token(client, auth)
    task, sent = await _open_stream(app, token, leave=asyncio.Event())
    await _until(lambda: app.state.event_broker.n_subscribers == 1)

    async def db_down(*_a, **_k):
        raise ConnectionRefusedError("postgres is down")

    monkeypatch.setattr(db, "get", db_down)
    with capture_logs() as logs:
        await asyncio.wait_for(task, timeout=2)      # поток закрыт, клиент переподключится
    assert sent[0]["status"] == 200
    assert any(e["event"] == "sse_revalidation_failed" for e in logs)
    assert app.state.event_broker.n_subscribers == 0
