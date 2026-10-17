"""Unit tests for service-to-service auth on recommendation_api (B19, B27).

rec_api принимает только service-токены (SERVICE_JWT_SECRET, aud
recommendation-api, type service) и сверяет издателя со списком маршрута:
401 — нет/недействительный токен, 403 — чужой издатель для маршрута.

Атакующий сам выбирает формат токена, поэтому атаки проверяются и в
«старом» формате без ``aud``/``iss``: прежняя проверка не требовала ни
``aud``, ни ``exp`` и принимала такой токен.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from app.security import (
    ISSUER_CAMPAIGN_MANAGER,
    ISSUER_MOBILE_API,
    require_campaign_manager,
    require_mobile_api,
)
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from tests.conftest import ADMIN_JWT_SECRET

LEGACY = ("aud", "iss")  # чего не было в токенах до stage B


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


async def _status(dependency, token: str | None) -> int:
    creds = None if token is None else _creds(token)
    try:
        await dependency(creds=creds)
    except HTTPException as exc:
        return exc.status_code
    return 200


# ── допуск ───────────────────────────────────────────────────────────────────
async def test_mobile_api_token_accepted_on_serving(mint):
    payload = await require_mobile_api(creds=_creds(mint(iss=ISSUER_MOBILE_API)))
    assert payload["iss"] == "mobile-api"
    assert payload["type"] == "service"
    assert payload["aud"] == "recommendation-api"


async def test_campaign_manager_token_accepted_on_explain(mint):
    token = mint(iss=ISSUER_CAMPAIGN_MANAGER)
    payload = await require_campaign_manager(creds=_creds(token))
    assert payload["iss"] == "campaign-manager"


async def test_audience_list_containing_rec_api_accepted(mint):
    token = mint(aud=["recommendation-api", "other"])
    assert await _status(require_mobile_api, token) == 200


# ── издатель не из списка маршрута → 403 ─────────────────────────────────────
@pytest.mark.parametrize(
    ("dependency", "issuer"),
    [
        (require_mobile_api, ISSUER_CAMPAIGN_MANAGER),
        (require_mobile_api, "evil-service"),
        (require_campaign_manager, ISSUER_MOBILE_API),
        (require_campaign_manager, "evil-service"),
    ],
)
async def test_issuer_not_allowed_for_route_is_403(mint, dependency, issuer):
    with pytest.raises(HTTPException) as exc:
        await dependency(creds=_creds(mint(iss=issuer)))
    assert exc.value.status_code == 403


# ── недействительный токен → 401 ─────────────────────────────────────────────
async def test_missing_credentials_rejected():
    for dependency in (require_mobile_api, require_campaign_manager):
        with pytest.raises(HTTPException) as exc:
            await dependency(creds=None)
        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


async def test_admin_access_token_rejected_everywhere(admin_access_token,
                                                     legacy_admin_access_token):
    """B27: админский access-токен (JWT_SECRET) не принимается нигде —
    ни в формате stage B, ни в прежнем."""
    for dependency in (require_mobile_api, require_campaign_manager):
        assert await _status(dependency, admin_access_token) == 401
        assert await _status(dependency, legacy_admin_access_token) == 401


async def test_access_token_signed_with_service_secret_rejected(mint):
    """Даже при ошибке оператора (JWT_SECRET == SERVICE_JWT_SECRET)
    админский токен не проходит: aud и type не те."""
    tokens = [
        mint(iss="cashback-campaign-manager", aud="cashback-admin",
             token_type="access", role="ADMIN"),
        mint(token_type="access", role="ADMIN", drop=LEGACY),
    ]
    for dependency in (require_mobile_api, require_campaign_manager):
        for token in tokens:
            assert await _status(dependency, token) == 401


@pytest.mark.parametrize("token_type", ["access", "refresh", "sse", "customer", None])
async def test_non_service_type_rejected(mint, token_type):
    for token in (mint(token_type=token_type),
                  mint(token_type=token_type, drop=LEGACY)):
        assert await _status(require_mobile_api, token) == 401


@pytest.mark.parametrize("aud", ["cashback-admin", "cashback-mobile", "cashback-sse"])
async def test_wrong_audience_rejected(mint, aud):
    assert await _status(require_mobile_api, mint(aud=aud)) == 401


@pytest.mark.parametrize("claim", ["aud", "exp", "iat", "iss"])
async def test_missing_required_claim_rejected(mint, claim):
    """python-jose без require_* пропускает токен без aud/exp/iat/iss
    (без exp — вечный токен)."""
    for token in (mint(drop=(claim,)), mint(drop=(claim, *LEGACY))):
        assert await _status(require_mobile_api, token) == 401


async def test_non_string_issuer_rejected(mint):
    """iss-список не должен ронять сравнение (500) — это 401."""
    token = mint(iss=["mobile-api"], sub="mobile-api")
    assert await _status(require_mobile_api, token) == 401


async def test_expired_token_rejected(mint):
    assert await _status(require_mobile_api, mint(ttl=-5)) == 401


async def test_service_token_signed_with_jwt_secret_rejected(mint):
    """JWT_SECRET есть в окружении (tests/conftest.py), но rec_api его не
    читает: service-токен, подписанный им, — 401."""
    for token in (mint(secret=ADMIN_JWT_SECRET),
                  mint(secret=ADMIN_JWT_SECRET, drop=LEGACY)):
        assert await _status(require_mobile_api, token) == 401


async def test_token_signed_with_other_secret_rejected(mint):
    token = mint(secret="some-other-service-secret-0123456789abcdef")
    assert await _status(require_mobile_api, token) == 401


async def test_empty_key_forged_token_rejected(mint):
    """B19: python-jose подписывает пустым ключом — такая подделка не проходит."""
    assert await _status(require_mobile_api, mint(secret="")) == 401


@pytest.mark.parametrize("default", ["dev-secret-change-me", "please-change-me"])
async def test_token_signed_with_public_default_rejected(mint, default):
    assert await _status(require_mobile_api, mint(secret=default)) == 401


async def test_other_algorithm_rejected(mint):
    """Только HS256: тот же ключ, но HS512 — 401."""
    assert await _status(require_mobile_api, mint(algorithm="HS512")) == 401


def _b64(obj: dict) -> str:
    raw = json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


async def test_unsigned_alg_none_token_rejected():
    now = int(time.time())
    claims = {"iss": "mobile-api", "sub": "mobile-api",
              "aud": "recommendation-api", "type": "service",
              "iat": now, "exp": now + 300}
    token = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."
    assert await _status(require_mobile_api, token) == 401


async def test_garbage_token_rejected():
    assert await _status(require_mobile_api, "not-a-jwt") == 401
