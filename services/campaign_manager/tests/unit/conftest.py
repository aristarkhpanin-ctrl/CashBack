"""Общие фейки для HTTP-тестов campaign_manager (B7, B48).

HTTP-тесты гоняют НАСТОЯЩЕЕ приложение из ``app.main.create_app()`` через
``TestClient`` — с теми же роутерами, middleware и зависимостями, что и в
проде. ``get_session_dep`` сознательно НЕ подменяется через
``dependency_overrides``: фейковый sessionmaker кладётся туда, откуда его
читает боевая зависимость (``app.state.sessionmaker``). Именно подмена
зависимости спрятала B7 — сломанная сигнатура ``get_session_dep`` давала 422
на каждом эндпоинте с БД, а unit-тесты оставались зелёными.

Lifespan не запускается (``TestClient`` без ``with``): он поднимает реальные
Postgres/Redis/ClickHouse/Kafka-клиенты; вместо этого ``app.state``
заполняется фейками.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from app import rbac
from app.events import EventBroker
from app.main import create_app
from app.security import issue_token_pair
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.fakes import FakeRedis, FakeSession, fake_sessionmaker


@pytest.fixture(autouse=True)
def _fresh_rbac_cache():
    # Кэш прав rbac — модульный глобал: без сброса тесты влияют друг на друга.
    rbac.invalidate_cache()
    yield
    rbac.invalidate_cache()


@pytest.fixture
def db() -> FakeSession:
    return FakeSession()


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def app(db: FakeSession, redis: FakeRedis) -> FastAPI:
    application = create_app()
    application.state.sessionmaker = fake_sessionmaker(db)
    application.state.redis = redis
    application.state.event_broker = EventBroker()
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth() -> Callable[..., dict[str, str]]:
    """``auth("MARKETER")`` → заголовок с настоящим access-токеном роли."""
    def _auth(role: str = "ADMIN", *, user_id: str | None = None,
              email: str = "admin@bank.ru") -> dict[str, str]:
        pair = issue_token_pair(
            user_id=user_id or "00000000-0000-0000-0000-000000000001",
            email=email, role=role,
        )
        return {"Authorization": f"Bearer {pair['access_token']}"}
    return _auth
