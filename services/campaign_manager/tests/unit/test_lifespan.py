"""Lifespan приложения: деградированный старт и аккуратный shutdown.

Стенд без ClickHouse/Kafka (или со сбоем планировщика) должен подниматься:
API работает, недоступные компоненты логируются и пропускаются. Engine и
sessionmaker — настоящие (``make_engine`` ленивый, к БД не подключается),
именно из ``app.state.sessionmaker`` берёт сессию ``get_session_dep``.
"""
from __future__ import annotations

import clickhouse_connect
import httpx
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
        def __init__(self, db_engine, **kwargs):
            calls.append("scheduler.init")
            self.db_engine, self.kwargs = db_engine, kwargs

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
        # планировщику ClickHouse не нужен (B22): только PG, Redis и интервал
        assert state.scheduler.db_engine is state.db_engine
        assert state.scheduler.kwargs == {
            "redis_client": state.redis,
            "interval_minutes": get_settings().scheduling_interval_minutes,
        }
        assert isinstance(state.db_engine, AsyncEngine)
        assert state.db_engine.url == make_url(get_settings().postgres_dsn)
        assert state.sessionmaker.class_ is AsyncSession
        assert state.sessionmaker.kw["bind"] is state.db_engine
        assert state.sessionmaker.kw["expire_on_commit"] is False
        assert state.event_broker.n_subscribers == 0

        # SSE: лимит подписчиков процесса из SSE_MAX_SUBSCRIBERS (B36)
        # и одного пользователя из SSE_MAX_SUBSCRIBERS_PER_USER
        per_user = get_settings().sse_max_subscribers_per_user
        for _ in range(per_user):
            state.event_broker.subscribe(owner="u1")
        with pytest.raises(events.TooManyUserSubscriptions):
            state.event_broker.subscribe(owner="u1")
        cap = get_settings().sse_max_subscribers
        for _ in range(cap - per_user):
            state.event_broker.subscribe()
        with pytest.raises(events.SubscriptionRefused):
            state.event_broker.subscribe()

        # клиент recommendation_api для /ml/explain (B27)
        rec_api = state.rec_api_client
        assert isinstance(rec_api, httpx.AsyncClient)
        assert rec_api.base_url == get_settings().recommendation_api_url
        assert rec_api.timeout == httpx.Timeout(
            get_settings().recommendation_api_timeout_seconds)

    # shutdown закрывает SSE-потоки и клиента rec_api
    assert app.state.event_broker.closed
    assert app.state.rec_api_client.is_closed

    # shutdown закрывает всё, даже если отдельные шаги падают
    assert calls == ["scheduler.init", "bridge.stop", "scheduler.shutdown",
                     "redis.aclose"]
