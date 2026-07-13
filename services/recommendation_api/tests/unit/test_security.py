"""Unit tests for service-to-service auth on recommendation_api (beyond-plan)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.config import get_settings
from app.security import ALGORITHM, require_caller
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt


def _token(secret: str, *, ttype: str = "service", sub: str = "mobile-api",
           ttl: int = 300) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {"sub": sub, "type": ttype, "iat": int(now.timestamp()),
         "exp": int((now + timedelta(seconds=ttl)).timestamp())},
        secret, algorithm=ALGORITHM,
    )


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture()
def secret():
    return get_settings().jwt_secret


@pytest.mark.asyncio
async def test_missing_credentials_rejected():
    with pytest.raises(HTTPException) as exc:
        await require_caller(creds=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_service_token_accepted(secret):
    payload = await require_caller(creds=_creds(_token(secret, ttype="service")))
    assert payload["type"] == "service"
    assert payload["sub"] == "mobile-api"


@pytest.mark.asyncio
async def test_access_token_accepted(secret):
    """Админ/фронтенд-токен (type=access) тоже проходит — для live-SHAP."""
    payload = await require_caller(creds=_creds(_token(secret, ttype="access")))
    assert payload["type"] == "access"


@pytest.mark.asyncio
async def test_refresh_token_type_rejected(secret):
    with pytest.raises(HTTPException) as exc:
        await require_caller(creds=_creds(_token(secret, ttype="refresh")))
    assert exc.value.status_code == 401
    assert "not accepted" in exc.value.detail


@pytest.mark.asyncio
async def test_wrong_secret_rejected(secret):
    with pytest.raises(HTTPException) as exc:
        await require_caller(creds=_creds(_token("some-other-secret")))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_rejected(secret):
    with pytest.raises(HTTPException) as exc:
        await require_caller(creds=_creds(_token(secret, ttl=-5)))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_auth_disabled_bypasses_check(monkeypatch, secret):
    """auth_enabled=False → проверка отключена (демо/локальный режим)."""
    cfg = get_settings()
    monkeypatch.setattr(cfg, "auth_enabled", False)
    payload = await require_caller(creds=None)
    assert payload == {"type": "disabled"}
