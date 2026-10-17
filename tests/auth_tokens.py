"""Токены для корневых тестов: integration, e2e, locust (stage B, ADR-0013).

Своего эндпоинта выдачи токенов у стека нет, тесты выпускают их так же,
как оператор демо-стенда:

* :func:`customer_token` — customer-токен для mobile_api тем же демо-IdP,
  что ``python3 scripts/mint_customer_token.py --user-id …`` (функция
  берётся из самого скрипта, а не копируется сюда);
* :func:`service_token` — service-токен в recommendation_api с клеймами,
  которые подписывают mobile_api (``iss mobile-api``, боевая выдача) и
  campaign_manager (``iss campaign-manager``, read-only ``/explain``);
* :func:`env_value` — секреты и пароли: окружение, иначе ``.env`` в корне
  репозитория (его создаёт ``make env``) — как у docker compose.

Только стандартная библиотека: locustfile грузит модуль по пути файла, а
e2e и сидер работают в окружении без пакетов сервисов.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

# Клеймы service-токена — контракт stage B, §2 (их же проверяет
# services/recommendation_api/app/security.py).
SERVICE_AUDIENCE = "recommendation-api"
SERVICE_TOKEN_TTL_SECONDS = 300
ISSUER_MOBILE_API = "mobile-api"
ISSUER_CAMPAIGN_MANAGER = "campaign-manager"


@lru_cache(maxsize=1)
def _demo_idp() -> ModuleType:
    """``scripts/mint_customer_token.py`` как модуль (scripts/ — не пакет)."""
    path = REPO_ROOT / "scripts" / "mint_customer_token.py"
    spec = importlib.util.spec_from_file_location("_cashback_demo_idp", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    return _demo_idp().read_env_file(ENV_FILE) if ENV_FILE.is_file() else {}


def env_value(name: str, default: str = "") -> str:
    """Непустое значение из окружения, иначе из ``.env``, иначе ``default``."""
    return os.environ.get(name) or _dotenv().get(name) or default


def require_env(name: str) -> str:
    """:func:`env_value`, но без значения — понятная ошибка вместо 401."""
    value = env_value(name)
    if not value:
        raise RuntimeError(f"{name} is not set: export it or run `make env` "
                           f"to create {ENV_FILE}")
    return value


def customer_token(user_id: uuid.UUID | str, *, ttl_seconds: int = 3600,
                   secret: str | None = None, issuer: str | None = None) -> str:
    """Customer-токен клиента ``user_id`` (``sub``) для ``/v1/mobile/*``."""
    idp = _demo_idp()
    return idp.mint_customer_token(
        user_id,
        secret=secret or require_env("MOBILE_JWT_SECRET"),
        issuer=issuer or env_value("MOBILE_JWT_ISSUER", idp.DEFAULT_ISSUER),
        ttl_seconds=ttl_seconds,
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hs256(claims: dict, secret: str) -> str:
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in ({"alg": "HS256", "typ": "JWT"}, claims)
    )
    signature = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"),
                         hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def service_token(issuer: str = ISSUER_MOBILE_API, *, secret: str | None = None,
                  ttl_seconds: int = SERVICE_TOKEN_TTL_SECONDS,
                  audience: str = SERVICE_AUDIENCE, token_type: str = "service",
                  now: int | None = None) -> str:
    """Service-токен вызывающего сервиса ``issuer`` в recommendation_api.

    ``audience`` / ``token_type`` меняются только в негативных тестах.
    """
    issued = int(time.time()) if now is None else now
    return _hs256({
        "iss": issuer,
        "sub": issuer,
        "aud": audience,
        "type": token_type,
        "iat": issued,
        "exp": issued + ttl_seconds,
    }, secret or require_env("SERVICE_JWT_SECRET"))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
