"""Customer-токен на маршрутах /v1/mobile (B12, B19): матрица атак.

Запросы идут в настоящее приложение (``create_app()``, настоящие
зависимости), цель — СВОЙ баланс (``{user_id}`` = ``sub``), чтобы 401 не
путался с 403. Любой недействительный токен — 401 с
``WWW-Authenticate: Bearer``, и запрос не доходит ни до БД, ни до Redis,
ни до recommendation_api.
"""
from __future__ import annotations

import base64
import json
import time
import uuid

import httpx
import pytest

from tests.conftest import ADMIN_JWT_SECRET, bearer, make_token, service_secret
from tests.unit.fakes import build_app


@pytest.fixture()
async def mobile():
    built = build_app()
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        built.client = client
        yield built


@pytest.fixture()
def fresh_settings(monkeypatch):
    """get_settings() кэширован на процесс: сбросить до и после теста."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _b64(part: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(part).encode()).rstrip(b"=").decode()


def _alg_none(sub: str) -> str:
    now = int(time.time())
    claims = {"iss": "cashback-demo-idp", "sub": sub, "aud": "cashback-mobile",
              "type": "customer", "iat": now, "exp": now + 600}
    return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}."


def _service_token() -> str:
    from app.service_token import ServiceTokenProvider

    return ServiceTokenProvider(service_secret()).token()


# (имя, фабрика заголовков по sub)
ATTACKS = [
    ("no header", lambda sub: {}),
    ("basic scheme", lambda sub: {"Authorization": "Basic YWRtaW46YWRtaW4="}),
    ("garbage", lambda sub: bearer("not-a-jwt")),
    ("expired", lambda sub: bearer(make_token(sub=sub, ttl=-10))),
    ("aud cashback-admin", lambda sub: bearer(make_token(sub=sub, aud="cashback-admin"))),
    ("aud recommendation-api",
     lambda sub: bearer(make_token(sub=sub, aud="recommendation-api"))),
    ("foreign issuer", lambda sub: bearer(make_token(sub=sub, iss="evil-idp"))),
    ("issuer as list",
     lambda sub: bearer(make_token(sub=sub, iss=["cashback-demo-idp"]))),  # type: ignore[arg-type]
    ("type access", lambda sub: bearer(make_token(sub=sub, token_type="access"))),
    ("type service", lambda sub: bearer(make_token(sub=sub, token_type="service"))),
    ("no type", lambda sub: bearer(make_token(sub=sub, drop=("type",)))),
    ("signed with SERVICE_JWT_SECRET",
     lambda sub: bearer(make_token(sub=sub, secret=service_secret()))),
    ("signed with JWT_SECRET", lambda sub: bearer(make_token(sub=sub, secret=ADMIN_JWT_SECRET))),
    ("empty-key forgery", lambda sub: bearer(make_token(sub=sub, secret=""))),
    ("public default key",
     lambda sub: bearer(make_token(sub=sub, secret="dev-secret-change-me"))),
    ("HS512", lambda sub: bearer(make_token(sub=sub, algorithm="HS512"))),
    ("alg none", lambda sub: bearer(_alg_none(sub))),
    ("no exp", lambda sub: bearer(make_token(sub=sub, drop=("exp",)))),
    ("no iat", lambda sub: bearer(make_token(sub=sub, drop=("iat",)))),
    ("no iss", lambda sub: bearer(make_token(sub=sub, drop=("iss",)))),
    ("no aud", lambda sub: bearer(make_token(sub=sub, drop=("aud",)))),
    ("no sub", lambda sub: bearer(make_token(sub=sub, drop=("sub",)))),
    ("sub not a uuid", lambda sub: bearer(make_token(sub="admin"))),
    ("sub not a string", lambda sub: bearer(make_token(sub=12345))),  # type: ignore[arg-type]
    # Админский access-токен campaign_manager — в формате stage B и в прежнем
    # (без iss/aud, общий ключ по умолчанию).
    ("admin access token", lambda sub: bearer(make_token(
        sub=sub, iss="cashback-campaign-manager", aud="cashback-admin",
        token_type="access", secret=ADMIN_JWT_SECRET, role="ADMIN", ver=0, jti="j1"))),
    ("legacy admin token", lambda sub: bearer(make_token(
        sub=sub, token_type="access", secret="dev-secret-change-me",
        drop=("iss", "aud"), role="ADMIN"))),
    # Собственный service-токен mobile_api для rec_api — не клиентский.
    ("mobile-api service token", lambda sub: bearer(_service_token())),
]


@pytest.mark.parametrize(("name", "headers"), ATTACKS, ids=[a[0] for a in ATTACKS])
async def test_invalid_customer_token_is_401_and_touches_nothing(mobile, name, headers):
    sub = str(uuid.uuid4())

    resp = await mobile.client.get(f"/v1/mobile/cashback/balance/{sub}", headers=headers(sub))

    assert resp.status_code == 401, resp.text
    assert resp.headers["www-authenticate"] == "Bearer"
    assert mobile.untouched()


async def test_valid_customer_token_reads_own_balance(mobile):
    sub = str(uuid.uuid4())

    resp = await mobile.client.get(f"/v1/mobile/cashback/balance/{sub}",
                                   headers=bearer(make_token(sub=sub)))

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == sub
    [params] = mobile.db.params_of("SELECT")
    assert params == {"uid": uuid.UUID(sub)}


def test_decode_returns_canonical_customer_id():
    from app.security import Customer, decode_customer_token

    sub = uuid.uuid4()
    token = make_token(sub=str(sub).upper())
    assert decode_customer_token(token) == Customer(user_id=sub)


async def test_issuer_is_taken_from_settings(fresh_settings):
    """MOBILE_JWT_ISSUER — издатель боевого IdP банка; токены демо-IdP с ним
    не проходят."""
    from app.security import decode_customer_token
    from fastapi import HTTPException

    fresh_settings.setenv("MOBILE_JWT_ISSUER", "bank-idp")
    sub = uuid.uuid4()

    assert decode_customer_token(make_token(sub=str(sub), iss="bank-idp")).user_id == sub
    with pytest.raises(HTTPException) as err:
        decode_customer_token(make_token(sub=str(sub), iss="cashback-demo-idp"))
    assert err.value.status_code == 401


async def test_secret_is_taken_from_settings(fresh_settings):
    """Ротация MOBILE_JWT_SECRET: токены, подписанные старым ключом, — 401."""
    from app.security import decode_customer_token
    from fastapi import HTTPException

    old = make_token(sub=str(uuid.uuid4()))
    fresh_settings.setenv("MOBILE_JWT_SECRET", "rotated-mobile-secret-" + "0" * 16)

    with pytest.raises(HTTPException) as err:
        decode_customer_token(old)
    assert err.value.status_code == 401
