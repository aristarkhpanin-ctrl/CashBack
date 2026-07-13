"""Unit tests for the mobile→rec service-token provider (beyond-plan)."""
from __future__ import annotations

from app.service_token import ALGORITHM, ServiceTokenProvider
from jose import jwt


def test_mints_valid_service_token():
    p = ServiceTokenProvider("sekret", ttl_seconds=300)
    token = p.token()
    claims = jwt.decode(token, "sekret", algorithms=[ALGORITHM])
    assert claims["type"] == "service"
    assert claims["sub"] == "mobile-api"
    assert claims["exp"] > claims["iat"]


def test_token_is_cached_between_calls():
    p = ServiceTokenProvider("sekret", ttl_seconds=300)
    assert p.token() == p.token()


def test_reissues_when_near_expiry():
    p = ServiceTokenProvider("sekret", ttl_seconds=300)
    p.token()
    # Симулируем истёкший кэш: следующий вызов обязан перевыпустить токен
    # (обновить _expires_at), а не вернуть протухший из кэша.
    p._expires_at = 0.0
    p.token()
    assert p._expires_at > 0.0


def test_auth_header_shape():
    p = ServiceTokenProvider("sekret")
    header = p.auth_header()
    assert header["Authorization"].startswith("Bearer ")
    token = header["Authorization"].removeprefix("Bearer ")
    jwt.decode(token, "sekret", algorithms=[ALGORITHM])
