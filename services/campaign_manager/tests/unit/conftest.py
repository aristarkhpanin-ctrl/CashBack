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

import os

# Settings без секретов не создаются (B19), а app.main строит приложение при
# импорте — тестовые ключи нужны ДО импорта модулей app.
os.environ.setdefault("JWT_SECRET", "unit-test-jwt-secret-0123456789abcdef")
os.environ.setdefault("SERVICE_JWT_SECRET",
                      "unit-test-service-secret-0123456789abcdef")

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from app.events import EventBroker
from app.main import create_app
from app.models import AdminUser
from app.security import DUMMY_PASSWORD_HASH, make_access_token
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.fakes import FakeRedis, FakeSession, fake_sessionmaker

# Пользователь по умолчанию у каждой роли свой: auth("ADMIN") и
# auth("MARKETER") в одном тесте — разные строки admin_users.
DEFAULT_USER_IDS = {
    "ADMIN": uuid.UUID("00000000-0000-0000-0000-000000000001"),
    "MARKETER": uuid.UUID("00000000-0000-0000-0000-000000000002"),
    "ANALYST": uuid.UUID("00000000-0000-0000-0000-000000000003"),
}


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
def auth(db: FakeSession) -> Callable[..., dict[str, str]]:
    """``auth("MARKETER")`` → заголовок с настоящим access-токеном роли.

    ``get_current_user`` сверяет токен со строкой admin_users, поэтому
    фикстура заводит её в ``db`` (или берёт уже положенную тестом с тем же
    ``user_id`` — тогда её роль должна совпадать с запрошенной).
    """
    def _auth(role: str = "ADMIN", *, user_id: str | None = None,
              email: str | None = None) -> dict[str, str]:
        uid = uuid.UUID(user_id) if user_id else DEFAULT_USER_IDS[role]
        user = db.tables.get(AdminUser, {}).get(uid)
        if user is None:
            user = AdminUser(
                user_id=uid,
                email=email or ("admin@bank.ru" if role == "ADMIN"
                                else f"{role.lower()}@bank.ru"),
                password_hash=DUMMY_PASSWORD_HASH,
                full_name=role.title(), role=role, is_active=True,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            db.put(user)
        assert user.role == role, f"user {uid} has role {user.role}, not {role}"
        return {"Authorization": f"Bearer {make_access_token(user)}"}
    return _auth
