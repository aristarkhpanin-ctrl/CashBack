"""Entry point — the supervisor and the notification consumer (stage C §2, B45).

* a malformed ``recommendations.created`` message is logged and skipped:
  before, a JSON ``value_deserializer`` raised inside ``getmany()`` and the
  notification task died — and ``asyncio.wait(FIRST_COMPLETED)`` then shut
  the accrual listener down with it;
* each loop runs under :func:`supervise`: a crash restarts it with backoff
  (1 s doubling to 30 s), the other loop keeps running.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from app import main as mod
from app.config import Settings
from app.main import NotificationConsumer, supervise


# ---------------------------------------------------------------------------
# supervise
# ---------------------------------------------------------------------------
@pytest.fixture()
def pauses(monkeypatch) -> list[float]:
    recorded: list[float] = []

    async def pause(stop: asyncio.Event, seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(mod, "_pause", pause)
    return recorded


async def test_crashed_loop_is_restarted_with_exponential_backoff(pauses):
    stop = asyncio.Event()
    runs = 0

    async def run() -> None:
        nonlocal runs
        runs += 1
        if runs < 8:
            raise ConnectionError("kafka down")
        stop.set()

    before = mod.LOOP_RESTARTS.labels(loop="t1")._value.get()
    await supervise("t1", run, stop, initial_delay=1.0, max_delay=30.0)

    assert runs == 8
    assert pauses == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert mod.LOOP_RESTARTS.labels(loop="t1")._value.get() == before + 7


async def test_loop_that_returns_without_a_stop_is_restarted(pauses):
    stop = asyncio.Event()
    runs = 0

    async def run() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            stop.set()

    await supervise("t2", run, stop)

    assert runs == 3
    assert pauses == [1.0, 2.0]


async def test_backoff_resets_after_a_healthy_run(pauses):
    stop = asyncio.Event()
    runs = 0

    async def run() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            await asyncio.sleep(0.05)       # ran longer than max_delay → healthy
        if runs == 5:
            stop.set()
            return
        raise RuntimeError("crash")

    await supervise("t3", run, stop, initial_delay=0.01, max_delay=0.04)

    assert pauses == [0.01, 0.02, 0.01, 0.02]


async def test_cancellation_is_not_swallowed(pauses):
    async def run() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(supervise("t4", run, asyncio.Event()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pauses == []


async def test_pause_ends_early_on_stop():
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(mod._pause(stop, 30.0), timeout=1.0)
    await mod._pause(asyncio.Event(), 0.0)


# ---------------------------------------------------------------------------
# NotificationConsumer
# ---------------------------------------------------------------------------
@dataclass
class Msg:
    value: Optional[bytes]
    offset: int = 0


class FakeSelector:
    def __init__(self, channel: Optional[str] = "push") -> None:
        self.channel = channel
        self.delivered: list[tuple] = []

    async def deliver(self, rec: Any, prefs: Any) -> Optional[str]:
        self.delivered.append((rec, prefs))
        return self.channel


class FakeScheduler:
    def __init__(self, now: bool = True, error: Optional[Exception] = None) -> None:
        self.now = now
        self.error = error

    async def should_deliver_now(self, user_id: str) -> bool:
        if self.error is not None:
            raise self.error
        return self.now


class FakeConsumer:
    def __init__(self, batches: list[list[Msg]], on_drained: Any) -> None:
        self.batches = batches
        self.on_drained = on_drained
        self.stopped = False
        self.kwargs: dict[str, Any] = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped = True

    async def getmany(self, timeout_ms: int = 0, max_records: int = 0):
        if not self.batches:
            self.on_drained()
            return {}
        return {"tp": self.batches.pop(0)}


def payload(**over: Any) -> bytes:
    body = {"user_id": "u-1", "mcc_code": "5411", "score": 0.7, "campaign_id": "c-1",
            "cashback_rate": 5.0, "preferences": {"allowed_channels": ["push", "in_app"],
                                                  "push_token": "t"}}
    body.update(over)
    return json.dumps(body).encode()


def outcomes() -> dict[str, float]:
    return {s.labels["outcome"]: s.value for metric in mod.NOTIF_EVENTS.collect()
            for s in metric.samples if s.name.endswith("_total")}


def consumer_for(batches: list[list[Msg]], *, selector: Optional[FakeSelector] = None,
                 scheduler: Optional[FakeScheduler] = None):
    notif = NotificationConsumer(Settings(), selector or FakeSelector(),
                                 scheduler or FakeScheduler())
    fake = FakeConsumer(batches, on_drained=notif._stop.set)

    async def start() -> None:
        notif._consumer = fake

    notif.start = start
    return notif, fake


async def test_malformed_messages_are_skipped_and_the_loop_goes_on():
    selector = FakeSelector()
    batch = [Msg(b"{not json"), Msg(b"[1, 2]"), Msg(None), Msg(b"\xff\xfe"),
             Msg(json.dumps({"mcc_code": "5411"}).encode()),
             Msg(payload(score="high")), Msg(payload(preferences="nope")),
             Msg(payload(user_id="u-good"))]
    notif, fake = consumer_for([batch], selector=selector)
    before = outcomes()

    await notif.run()

    [(rec, prefs)] = selector.delivered
    assert rec.user_id == "u-good" and prefs.allowed_channels == ["push", "in_app"]
    after = outcomes()
    assert after["bad_payload"] - before.get("bad_payload", 0) == 6
    assert after["missing_fields"] - before.get("missing_fields", 0) == 1
    assert fake.stopped


async def test_a_failing_delivery_does_not_stop_the_consumer():
    selector = FakeSelector()
    notif, _ = consumer_for([[Msg(payload())], [Msg(payload(user_id="u-2"))]],
                            selector=selector,
                            scheduler=FakeScheduler(error=RuntimeError("redis down")))
    before = outcomes()

    await notif.run()

    assert selector.delivered == []
    assert outcomes()["error"] - before.get("error", 0) == 2


async def test_deferred_by_ost_goes_in_app_only_and_no_channel_is_counted():
    selector = FakeSelector(channel=None)
    notif, _ = consumer_for([[Msg(payload())]], selector=selector,
                            scheduler=FakeScheduler(now=False))
    before = outcomes()

    await notif.run()

    [(_, prefs)] = selector.delivered
    assert prefs.allowed_channels == ["in_app"]
    assert outcomes()["no_channel"] - before.get("no_channel", 0) == 1


async def test_consumer_reads_raw_bytes(monkeypatch):
    created: dict[str, Any] = {}

    class Consumer:
        def __init__(self, *topics: str, **kwargs: Any) -> None:
            created.update(kwargs, topics=topics)

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            created["stopped"] = True

    monkeypatch.setattr(mod, "AIOKafkaConsumer", Consumer)
    notif = NotificationConsumer(Settings(), FakeSelector(), FakeScheduler())

    await notif.start()
    await notif.stop()

    assert "value_deserializer" not in created
    assert created["topics"] == ("recommendations.created",)
    assert created["group_id"] == "cashback-notify-group"
    assert created["stopped"] and notif._stop.is_set()


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
class Closable:
    def __init__(self, name: str, log: list[str], fail: bool = False) -> None:
        self.name, self.log, self.fail = name, log, fail

    async def _close(self) -> None:
        self.log.append(self.name)
        if self.fail:
            raise RuntimeError(f"{self.name} close failed")


async def test_build_components_wires_timeouts_and_closes_everything(monkeypatch,
                                                                      tmp_path):
    closed: list[str] = []
    seen: dict[str, Any] = {}

    def engine(dsn: str, **kwargs: Any):
        seen["engine"] = kwargs
        db = Closable("db", closed)
        db.dispose = db._close
        return db

    class Producer(Closable):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__("producer", closed, fail=True)
            seen["producer"] = kwargs

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            await self._close()

    class Http(Closable):
        def __init__(self) -> None:
            super().__init__("http", closed)

        async def aclose(self) -> None:
            await self._close()

    class Redis(Closable):
        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> Redis:
            seen["redis"] = kwargs
            return cls("redis", closed)

        async def aclose(self) -> None:
            await self._close()

    import redis.asyncio
    monkeypatch.setattr(mod, "create_async_engine", engine)
    monkeypatch.setattr(mod, "AIOKafkaProducer", Producer)
    monkeypatch.setattr(mod.httpx, "AsyncClient", Http)
    monkeypatch.setattr(redis.asyncio, "Redis", Redis)
    settings = Settings(postgres_command_timeout_s=12.0, redis_socket_timeout_s=3.0,
                        sent_emails_dir=str(tmp_path / "sent"))

    listener, notif, shutdown = await mod._build_components(settings)

    assert seen["engine"]["connect_args"] == {"command_timeout": 12.0}
    assert seen["engine"]["pool_pre_ping"] is True
    assert seen["redis"]["socket_timeout"] == 3.0
    assert seen["redis"]["socket_connect_timeout"] == 3.0
    assert seen["producer"]["compression_type"] == "zstd"
    assert listener._owns_dlq_producer          # its own acks=all DLQ producer

    await shutdown()                            # a failing close does not stop the rest

    assert closed == ["producer", "http", "redis", "db"]
    assert listener._stop.is_set() and notif._stop.is_set()


async def test_a_crashing_loop_does_not_take_the_other_one_down(pauses):
    stop = asyncio.Event()
    listener_runs = 0
    shut: list[bool] = []

    class Listener:
        async def run(self) -> None:
            nonlocal listener_runs
            listener_runs += 1
            await asyncio.sleep(3600)

    class Notif:
        runs = 0

        async def run(self) -> None:
            Notif.runs += 1
            if Notif.runs == 3:
                stop.set()
            raise ValueError("malformed")

    async def shutdown() -> None:
        shut.append(True)

    await asyncio.wait_for(mod._serve(Settings(), Listener(), Notif(), shutdown, stop),
                           timeout=5)

    assert Notif.runs == 3 and listener_runs == 1   # the listener ran throughout
    assert shut == [True]


async def test_amain_serves_until_a_signal(monkeypatch):
    calls: dict[str, Any] = {}

    async def build(settings: Settings):
        return "listener", "notif", "shutdown"

    async def serve(settings, listener, notif, shutdown, stop_event):
        calls["serve"] = (listener, notif, shutdown)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop_event.wait(), timeout=5)
        calls["stopped"] = True

    monkeypatch.setattr(mod, "get_settings", lambda: Settings(metrics_port=0))
    monkeypatch.setattr(mod, "start_http_server", lambda port: calls.setdefault("port", port))
    monkeypatch.setattr(mod, "_build_components", build)
    monkeypatch.setattr(mod, "_serve", serve)

    await mod.amain()

    assert calls == {"port": 0, "serve": ("listener", "notif", "shutdown"),
                     "stopped": True}
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.remove_signal_handler(sig)


def test_main_runs_amain(monkeypatch):
    ran: list[bool] = []

    async def amain() -> None:
        ran.append(True)

    monkeypatch.setattr(mod, "amain", amain)
    mod.main()
    assert ran == [True]
