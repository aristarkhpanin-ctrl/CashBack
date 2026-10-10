"""Lifespan приложения: деградированный старт и аккуратный shutdown.

Стенд без ClickHouse/Kafka (или со сбоем планировщика) должен подниматься:
API работает, недоступные компоненты логируются и пропускаются. Engine и
sessionmaker — настоящие (``make_engine`` ленивый, к БД не подключается),
именно из ``app.state.sessionmaker`` берёт сессию ``get_session_dep``.
"""
from __future__ import annotations

import clickhouse_connect
import pytest
import redis.asyncio
from app import events, main
from app.config import get_settings
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession


@pytest.mark.parametrize("shutdown_fails", [False, True])
def test_lifespan_starts_without_clickhouse_and_kafka(monkeypatch, shutdown_fails):
    calls: list[str] = []

    def _maybe_fail(name: str) -> None:
        calls.append(name)
        if shutdown_fails:
            raise RuntimeError(f"{name} failed")

    class _Redis:
        async def aclose(self):
            _maybe_fail("redis.aclose")

    class _Scheduler:
        def __init__(self, db_engine, ch_client, **kwargs):
            calls.append("scheduler.init")
            self.ch_client = ch_client

        def start(self):
            raise RuntimeError("apscheduler broken")   # сбой старта не роняет API

        def shutdown(self):
            _maybe_fail("scheduler.shutdown")

    class _Bridge:
        def __init__(self, broker, *, bootstrap):
            assert bootstrap == get_settings().kafka_bootstrap_servers

        async def start(self):
            raise ConnectionError("kafka unreachable")

        async def stop(self):
            _maybe_fail("bridge.stop")

    def _no_clickhouse(**_kw):
        raise ConnectionError("clickhouse unreachable")

    monkeypatch.setattr(redis.asyncio.Redis, "from_url",
                        classmethod(lambda cls, url, **kw: _Redis()))
    monkeypatch.setattr(clickhouse_connect, "get_client", _no_clickhouse)
    monkeypatch.setattr(main, "CampaignScheduler", _Scheduler)
    monkeypatch.setattr(events, "AccrualStreamBridge", _Bridge)

    app = main.create_app()
    with TestClient(app) as client:                     # запускает lifespan
        r = client.get("/", headers={"X-Request-ID": "rid-42"})
        assert r.status_code == 200
        assert r.json()["request_id"] == "rid-42"
        assert r.headers["x-request-id"] == "rid-42"

        state = app.state
        assert state.ch_client is None                  # ClickHouse недоступен
        assert state.scheduler.ch_client is None
        assert isinstance(state.db_engine, AsyncEngine)
        assert state.db_engine.url == make_url(get_settings().postgres_dsn)
        assert state.sessionmaker.class_ is AsyncSession
        assert state.sessionmaker.kw["bind"] is state.db_engine
        assert state.sessionmaker.kw["expire_on_commit"] is False
        assert state.event_broker.n_subscribers == 0

    # shutdown закрывает всё, даже если отдельные шаги падают
    assert calls == ["scheduler.init", "bridge.stop", "scheduler.shutdown",
                     "redis.aclose"]
