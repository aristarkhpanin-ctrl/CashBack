"""Авторизация маршрутов mobile_api (B12, B36) и service-токен в rec_api.

* Таблица маршрутов: каждый маршрут либо публичный (health, /metrics, /,
  документация), либо требует customer-токен, а маршрут с ``{user_id}`` —
  ещё и совпадение ``{user_id}`` с ``sub``. Новый маршрут без проверки
  роняет тест.
* Чужой ``{user_id}`` — 403 ``forbidden`` до любого обращения к данным.
* CORS: только явные origin из CORS_ORIGINS, по умолчанию ни одного.
* Lifespan подписывает вызовы recommendation_api service-токеном, который
  принимает rec_api (SERVICE_JWT_SECRET, iss mobile-api, aud
  recommendation-api).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.routing import APIRoute
from jose import jwt

from tests.conftest import bearer, make_token, service_secret
from tests.unit.fakes import build_app

PUBLIC = {
    ("GET", "/"),
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("GET", "/metrics"),
}
# Маршруты документации FastAPI (не APIRoute): /openapi.json, /docs, /redoc.
DOCS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

CUSTOMER = "customer"
OWN_USER_ID = "own user_id"
PROTECTED = {
    ("GET", "/v1/mobile/recommendations/{user_id}"): {CUSTOMER, OWN_USER_ID},
    # Владелец рекомендации проверяется в SQL отклика (чужая — 404).
    ("POST", "/v1/mobile/recommendations/{recommendation_id}/respond"): {CUSTOMER},
    ("GET", "/v1/mobile/cashback/history/{user_id}"): {CUSTOMER, OWN_USER_ID},
    ("GET", "/v1/mobile/cashback/balance/{user_id}"): {CUSTOMER, OWN_USER_ID},
}


def _calls(dependant) -> set:
    out = set()
    for dep in dependant.dependencies:
        out.add(dep.call)
        out |= _calls(dep)
    return out


def test_every_route_is_public_or_requires_its_checks():
    from app.main import create_app
    from app.security import require_customer, require_own_user_id

    names = {require_customer: CUSTOMER, require_own_user_id: OWN_USER_ID}
    seen: dict[tuple[str, str], set[str]] = {}
    for route in create_app().routes:
        if not isinstance(route, APIRoute):
            assert route.path in DOCS, f"unexpected non-API route {route.path}"
            continue
        checks = {names[c] for c in _calls(route.dependant) if c in names}
        for method in route.methods:
            seen[(method, route.path)] = checks

    public = {key for key, checks in seen.items() if not checks}
    assert public == PUBLIC
    assert {k: v for k, v in seen.items() if v} == PROTECTED


@pytest.fixture()
async def mobile():
    built = build_app()
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        built.client = client
        yield built


USER_ROUTES = [
    "/v1/mobile/recommendations/{uid}",
    "/v1/mobile/cashback/history/{uid}",
    "/v1/mobile/cashback/balance/{uid}",
]


@pytest.mark.parametrize("template", USER_ROUTES)
async def test_no_token_is_401(mobile, template):
    resp = await mobile.client.get(template.format(uid=uuid.uuid4()))

    assert resp.status_code == 401, resp.text
    assert mobile.untouched()


async def test_respond_without_token_is_401(mobile):
    resp = await mobile.client.post(
        f"/v1/mobile/recommendations/{uuid.uuid4()}/respond", json={"action": "ACCEPTED"},
    )

    assert resp.status_code == 401, resp.text
    assert mobile.untouched()


@pytest.mark.parametrize("template", USER_ROUTES)
async def test_other_users_id_is_403_before_any_data_access(mobile, template):
    me, victim = uuid.uuid4(), uuid.uuid4()

    resp = await mobile.client.get(template.format(uid=victim),
                                   headers=bearer(make_token(sub=str(me))))

    assert resp.status_code == 403, resp.text
    assert resp.json() == {"detail": "forbidden"}
    assert mobile.untouched()


async def test_own_balance_and_history_query_only_own_rows(mobile):
    me = uuid.uuid4()
    headers = bearer(make_token(sub=str(me)))

    balance = await mobile.client.get(f"/v1/mobile/cashback/balance/{me}", headers=headers)
    history = await mobile.client.get(f"/v1/mobile/cashback/history/{me}", headers=headers)

    assert balance.status_code == 200, balance.text
    assert history.status_code == 200, history.text
    assert [p["uid"] for p in mobile.db.params_of("SELECT")] == [me, me]


async def test_own_recommendations_are_fetched_and_stored_for_self(mobile):
    me = uuid.uuid4()

    resp = await mobile.client.get(f"/v1/mobile/recommendations/{me}",
                                   headers=bearer(make_token(sub=str(me))))

    assert resp.status_code == 200, resp.text
    assert mobile.upstream.calls == [str(me)]
    [insert] = mobile.db.params_of("INSERT INTO recommendations")
    assert insert["uid"] == me


async def test_malformed_user_id_with_valid_token_is_422(mobile):
    resp = await mobile.client.get("/v1/mobile/cashback/balance/not-a-uuid",
                                   headers=bearer(make_token(sub=str(uuid.uuid4()))))

    assert resp.status_code == 422, resp.text
    assert mobile.untouched()


async def test_health_and_metrics_stay_public(mobile):
    assert (await mobile.client.get("/health/live")).status_code == 200
    assert (await mobile.client.get("/metrics")).status_code == 200


# ---------------------------------------------------------------------------
# CORS (B36)
# ---------------------------------------------------------------------------
@pytest.fixture()
def cors_app(monkeypatch):
    from app.config import get_settings

    def _build(origins: str | None):
        if origins is None:
            monkeypatch.delenv("CORS_ORIGINS", raising=False)
        else:
            monkeypatch.setenv("CORS_ORIGINS", origins)
        get_settings.cache_clear()
        return build_app().app

    yield _build
    get_settings.cache_clear()


async def _preflight(app, origin: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.options(
            f"/v1/mobile/cashback/balance/{uuid.uuid4()}",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                     "Access-Control-Request-Headers": "authorization"},
        )


async def test_cors_default_allows_no_origin(cors_app):
    resp = await _preflight(cors_app(None), "https://evil.example")

    assert "access-control-allow-origin" not in resp.headers


async def test_cors_allows_only_listed_origins(cors_app):
    app = cors_app("http://localhost:3000,http://localhost:5173")

    ok = await _preflight(app, "http://localhost:5173")
    evil = await _preflight(app, "https://evil.example")

    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert ok.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in evil.headers


# ---------------------------------------------------------------------------
# mobile → recommendation_api: service-токен из lifespan
# ---------------------------------------------------------------------------
class _NoKafka:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def start(self) -> None:
        raise ConnectionError("no kafka in unit tests")


def _rec_api_accepts(authorization: str) -> dict:
    """Проверка recommendation_api (app/security.py там) для боевой выдачи."""
    scheme, _, token = authorization.partition(" ")
    assert scheme == "Bearer"
    claims = jwt.decode(
        token, service_secret(), algorithms=["HS256"], audience="recommendation-api",
        options={"require_exp": True, "require_iat": True,
                 "require_iss": True, "require_aud": True},
    )
    assert claims["type"] == "service"
    assert claims["iss"] == "mobile-api"
    return claims


async def test_lifespan_signs_upstream_calls_with_service_token(monkeypatch):
    import aiokafka
    from app import main

    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"recommendations": [], "model_version": "7"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(main, "httpx", SimpleNamespace(
        AsyncClient=lambda **kw: real_client(transport=httpx.MockTransport(upstream), **kw),
    ))
    monkeypatch.setattr(aiokafka, "AIOKafkaProducer", _NoKafka)

    app = main.create_app()
    me = uuid.uuid4()
    async with app.router.lifespan_context(app), real_client(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get(f"/v1/mobile/recommendations/{me}",
                                headers=bearer(make_token(sub=str(me))))

    assert resp.status_code == 200, resp.text
    [request] = seen
    assert request.url.path == f"/recommendations/{me}"
    claims = _rec_api_accepts(request.headers["Authorization"])
    assert claims["sub"] == "mobile-api"
    assert 0 < claims["exp"] - claims["iat"] <= 300
