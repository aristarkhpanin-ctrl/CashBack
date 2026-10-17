"""Unit tests for the mobile→rec service-token provider (beyond-plan, B19).

Токен проверяется так же, как его проверяет recommendation_api
(app/security.py там): SERVICE_JWT_SECRET, aud ``recommendation-api``,
обязательные exp/iat/iss/aud, ``type = service``, издатель ``mobile-api``.
"""
from __future__ import annotations

import pytest
from app.service_token import ALGORITHM, ServiceTokenProvider
from jose import JWTError, jwt

SECRET = "service-token-tests-secret-0123456789abcdef"

_REC_API_OPTIONS = {"require_exp": True, "require_iat": True,
                    "require_iss": True, "require_aud": True}


def _rec_api_decode(token: str, secret: str = SECRET) -> dict:
    return jwt.decode(token, secret, algorithms=["HS256"],
                      audience="recommendation-api", options=_REC_API_OPTIONS)


def test_mints_service_token_accepted_by_rec_api():
    p = ServiceTokenProvider(SECRET, ttl_seconds=300)
    claims = _rec_api_decode(p.token())
    assert ALGORITHM == "HS256"
    assert claims["type"] == "service"
    assert claims["iss"] == "mobile-api"
    assert claims["sub"] == "mobile-api"
    assert claims["aud"] == "recommendation-api"
    assert claims["exp"] - claims["iat"] == 300


@pytest.mark.parametrize("other", [
    "another-secret-0123456789abcdef-0123",  # MOBILE_JWT_SECRET / JWT_SECRET
    "dev-secret-change-me",
    "",
])
def test_token_does_not_verify_with_another_key(other):
    token = ServiceTokenProvider(SECRET).token()
    with pytest.raises(JWTError):
        _rec_api_decode(token, other)


def test_token_is_not_a_customer_or_admin_token():
    token = ServiceTokenProvider(SECRET).token()
    for audience in ("cashback-mobile", "cashback-admin"):
        with pytest.raises(JWTError):
            jwt.decode(token, SECRET, algorithms=["HS256"], audience=audience)


def test_token_is_cached_between_calls():
    p = ServiceTokenProvider(SECRET, ttl_seconds=300)
    assert p.token() == p.token()


def test_reissues_when_near_expiry():
    p = ServiceTokenProvider(SECRET, ttl_seconds=300)
    p.token()
    # Симулируем истёкший кэш: следующий вызов обязан перевыпустить токен
    # (обновить _expires_at), а не вернуть протухший из кэша.
    p._expires_at = 0.0
    p.token()
    assert p._expires_at > 0.0


def test_auth_header_shape():
    p = ServiceTokenProvider(SECRET)
    header = p.auth_header()
    assert header["Authorization"].startswith("Bearer ")
    token = header["Authorization"].removeprefix("Bearer ")
    _rec_api_decode(token)
