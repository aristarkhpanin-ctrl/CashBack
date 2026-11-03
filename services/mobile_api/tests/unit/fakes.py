"""Подделки внешних клиентов для HTTP-тестов настоящего приложения.

``create_app()`` без lifespan: маршруты, зависимости и middleware —
настоящие, а Postgres и recommendation_api подменены записывающими
подделками. По записям видно, дошёл ли запрос до данных: отвергнутый
запрос (401/403) не должен ни читать БД, ни звать upstream.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

CAMPAIGN_ID = "11111111-2222-3333-4444-555555555555"


class _Result:
    def __init__(self, sql: str) -> None:
        self._sql = sql

    def one(self) -> Any:  # баланс
        return SimpleNamespace(pending_amount=Decimal("12.50"), paid_amount=Decimal("7.00"))

    def first(self) -> Any:
        return None

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[Any]:  # история, контекст кампаний
        return []


class _Conn:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def execute(self, clause: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(clause).split())
        self._engine.calls.append((sql, dict(params or {})))
        for prefix, exc in self._engine.errors.items():
            if sql.startswith(prefix):
                raise exc
        return _Result(sql)


class _Ctx:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> _Conn:
        return _Conn(self._engine)

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeEngine:
    """AsyncEngine, записывающий каждый SQL с параметрами; ``errors`` —
    исключение для SQL с данным префиксом (сбой Postgres)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, Exception] = {}

    def connect(self) -> _Ctx:
        return _Ctx(self)

    def begin(self) -> _Ctx:
        return _Ctx(self)

    def params_of(self, prefix: str) -> list[dict[str, Any]]:
        return [p for sql, p in self.calls if sql.startswith(prefix)]


def upstream_offer(mcc: str = "5411", **overrides: Any) -> dict:
    """Оффер боевой выдачи recommendation_api: строку recommendations он уже
    записал (B29) — её id и срок в ответе."""
    offer = {
        "mcc_code": mcc, "score": 0.8, "campaign_id": CAMPAIGN_ID,
        "recommendation_id": str(uuid.uuid4()),
        "expires_at": "2026-11-01T12:00:00+00:00",
        "top_factors": {"recency_days": 0.12},
    }
    offer.update(overrides)
    return offer


class FakeRecommendationClient:
    """recommendation_api: запоминает, за кого спросили рекомендации;
    ``offers`` — его ответ (по умолчанию один оффер)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.breaker = SimpleNamespace(state="closed")
        self.offers: list[dict] = [upstream_offer()]

    async def get_recommendations(self, user_id: str, **kwargs: Any) -> dict:
        self.calls.append(user_id)
        return {
            "user_id": user_id,
            "recommendations": list(self.offers),
            "model_version": "7",
        }


@dataclass
class MobileApp:
    app: FastAPI
    db: FakeEngine
    upstream: FakeRecommendationClient
    redis_calls: list[Any] = field(default_factory=list)

    def untouched(self) -> bool:
        return not self.db.calls and not self.upstream.calls and not self.redis_calls


class _RecordingRedis:
    def __init__(self, calls: list[Any]) -> None:
        self._calls = calls

    def __getattr__(self, name: str) -> Any:
        async def _record(*args: Any, **kwargs: Any) -> Any:
            self._calls.append((name, args))
            return 1
        return _record


def build_app() -> MobileApp:
    from app.config import get_settings
    from app.main import create_app

    app = create_app()
    db = FakeEngine()
    upstream = FakeRecommendationClient()
    redis_calls: list[Any] = []
    st = app.state
    st.settings = get_settings()
    st.db_engine = db
    st.redis = _RecordingRedis(redis_calls)
    st.recommendation_client = upstream
    st.kafka_producer = None
    st.snooze_scheduler = SimpleNamespace(schedule=lambda *a, **k: None)
    return MobileApp(app=app, db=db, upstream=upstream, redis_calls=redis_calls)


def other_user() -> str:
    return str(uuid.uuid4())
