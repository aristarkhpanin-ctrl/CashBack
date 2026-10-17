"""Общие фикстуры тестов mobile_api.

Settings требует сильные ``MOBILE_JWT_SECRET`` и ``SERVICE_JWT_SECRET``
(stage B, B19): без них ``app.main`` не импортируется. Тестовые секреты
задаются ДО импорта модулей приложения (обхода проверки в production-коде
нет); заданные снаружи (CI) значения не перетираются. Токены подписываются
значением из окружения — тем же, что читает Settings.
"""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable

import pytest

os.environ.setdefault(
    "MOBILE_JWT_SECRET", "mobile-api-tests-customer-secret-0123456789abcdef",
)
os.environ.setdefault(
    "SERVICE_JWT_SECRET", "mobile-api-tests-service-secret-0123456789abcdef",
)

# Секрет админских токенов campaign_manager. mobile_api его не читает
# (B19); тесты подписывают им «чужие» токены.
ADMIN_JWT_SECRET = "mobile-api-tests-admin-secret-fedcba9876543210"


def customer_secret() -> str:
    return os.environ["MOBILE_JWT_SECRET"]


def service_secret() -> str:
    return os.environ["SERVICE_JWT_SECRET"]


def make_token(*, sub: str | None, iss: str | None = None,
               aud: str | None = "cashback-mobile",
               token_type: str | None = "customer", ttl: int = 3600,
               secret: str | None = None, algorithm: str = "HS256",
               drop: tuple[str, ...] = (), **extra) -> str:
    """HS256-токен; по умолчанию — валидный customer-токен демо-IdP.

    ``drop`` — клеймы, которые нужно убрать; ``secret`` — ключ подписи
    (по умолчанию MOBILE_JWT_SECRET)."""
    from jose import jwt

    now = int(time.time())
    claims = {
        "iss": iss if iss is not None else os.environ.get(
            "MOBILE_JWT_ISSUER", "cashback-demo-idp"),
        "sub": sub, "aud": aud, "type": token_type,
        "iat": now, "exp": now + ttl, **extra,
    }
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, secret if secret is not None else customer_secret(),
                      algorithm=algorithm)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def mint() -> Callable[..., str]:
    return make_token


@pytest.fixture()
def customer_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture()
def customer_headers(customer_id) -> dict[str, str]:
    return bearer(make_token(sub=str(customer_id)))
