"""Общие фикстуры тестов recommendation_api.

Settings требует сильный ``SERVICE_JWT_SECRET`` (stage B, B19): без него
``app.main`` не импортируется. Тестовый секрет задаётся ДО импорта модулей
приложения (обхода проверки в production-коде нет). Токены подписываются
тем секретом, которым приложение реально проверяет (``get_settings()``),
— так тесты работают и с секретом, заданным снаружи (CI).
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

import pytest

os.environ.setdefault(
    "SERVICE_JWT_SECRET", "rec-api-tests-service-secret-0123456789abcdef",
)

# Секрет админских токенов campaign_manager. Он нарочно лежит и в окружении
# rec_api (как раньше, когда ключ был общим): сервис обязан его
# игнорировать, а тесты подписывают им «чужие» токены.
ADMIN_JWT_SECRET = "rec-api-tests-admin-secret-fedcba9876543210"
os.environ.setdefault("JWT_SECRET", ADMIN_JWT_SECRET)


@pytest.fixture()
def mint() -> Callable[..., str]:
    """Фабрика HS256-токенов; по умолчанию — валидный service-токен mobile-api.

    ``drop`` — клеймы, которые нужно убрать; ``secret`` — ключ подписи
    (по умолчанию SERVICE_JWT_SECRET приложения)."""
    from app.config import get_settings
    from jose import jwt

    def _mint(*, iss: str | None = "mobile-api",
              aud: str | None = "recommendation-api",
              token_type: str | None = "service", ttl: int = 300,
              secret: str | None = None, algorithm: str = "HS256",
              drop: tuple[str, ...] = (), **extra) -> str:
        now = int(time.time())
        claims = {"iss": iss, "sub": iss, "aud": aud, "type": token_type,
                  "iat": now, "exp": now + ttl, **extra}
        for name in drop:
            claims.pop(name, None)
        key = (secret if secret is not None
               else get_settings().service_jwt_secret.get_secret_value())
        return jwt.encode(claims, key, algorithm=algorithm)

    return _mint


@pytest.fixture()
def admin_access_token(mint) -> str:
    """Админский access-токен в формате campaign_manager (контракт §2)."""
    return mint(iss="cashback-campaign-manager", aud="cashback-admin",
                token_type="access", ttl=900, secret=ADMIN_JWT_SECRET,
                sub="00000000-0000-0000-0000-000000000001",
                email="admin@bank.ru", role="ADMIN", ver=0, jti="j1",
                pwd_change=False)


@pytest.fixture()
def legacy_admin_access_token(mint) -> str:
    """Админский access-токен в прежнем формате (без iss/aud) — его старая
    проверка rec_api принимала: ключ JWT_SECRET был общим."""
    return mint(token_type="access", ttl=900, secret=ADMIN_JWT_SECRET,
                drop=("iss", "aud"), sub="00000000-0000-0000-0000-000000000001",
                email="admin@bank.ru", role="ADMIN", jti="j1")
