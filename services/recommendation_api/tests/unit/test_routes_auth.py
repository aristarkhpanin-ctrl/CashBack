"""Маршруты recommendation_api ↔ требования к вызывающему (B19, B27).

1. Таблица покрытия: каждый маршрут приложения либо публичный (health,
   метрики, документация), либо требует service-токен РОВНО с тем списком
   издателей, что в таблице. Новый маршрут без проверки роняет тест.
2. Матрица атак по HTTP: без токена / админский access / подпись чужим
   ключом / пустой ключ → 401; чужой издатель для маршрута → 403.
"""
from __future__ import annotations

import pytest
from app.security import require_campaign_manager, require_mobile_api
from fastapi.routing import APIRoute

from tests.conftest import ADMIN_JWT_SECRET
from tests.unit.fakes import USER_ID

PUBLIC = {
    "/", "/health/live", "/health/ready", "/metrics",
    "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
}
PROTECTED = {
    ("GET", "/recommendations/{user_id}"): require_mobile_api,
    ("GET", "/recommendations/{user_id}/explain"): require_campaign_manager,
}
_AUTH_DEPENDENCIES = {require_mobile_api, require_campaign_manager}


def _calls(dependant) -> set:
    out = set()
    for dep in dependant.dependencies:
        out.add(dep.call)
        out |= _calls(dep)
    return out


def test_every_route_is_public_or_has_exactly_its_issuer_check(rec_app):
    seen = set()
    for route in rec_app.app.routes:
        path = route.path
        if not isinstance(route, APIRoute):
            assert path in PUBLIC, f"unexpected non-API route {path}"
            continue
        auth = _calls(route.dependant) & _AUTH_DEPENDENCIES
        for method in route.methods:
            key = (method, path)
            if path in PUBLIC:
                assert not auth, f"{key} is public but has {auth}"
                continue
            assert key in PROTECTED, f"route {key} is not in the auth table"
            assert auth == {PROTECTED[key]}, f"{key}: {auth}"
            seen.add(key)
    assert seen == set(PROTECTED)


# ---------------------------------------------------------------------------
SERVING = f"/recommendations/{USER_ID}"
EXPLAIN = f"/recommendations/{USER_ID}/explain"


def _token(mint, admin: str, legacy_admin: str, kind: str) -> str | None:
    return {
        "none": None,
        "mobile": mint(iss="mobile-api"),
        "campaign-manager": mint(iss="campaign-manager"),
        "admin-access": admin,
        # прежний формат (без iss/aud), JWT_SECRET — раньше проходил
        "admin-access-legacy": legacy_admin,
        # service-токен, подписанный JWT_SECRET (не тем ключом)
        "jwt-secret-signed": mint(iss="mobile-api", secret=ADMIN_JWT_SECRET),
        "jwt-secret-signed-legacy": mint(secret=ADMIN_JWT_SECRET,
                                         drop=("iss", "aud")),
        "empty-key-forged": mint(iss="mobile-api", secret=""),
        "customer": mint(iss="cashback-demo-idp", aud="cashback-mobile",
                         token_type="customer"),
    }[kind]


@pytest.mark.parametrize(
    ("path", "kind", "expected"),
    [
        (SERVING, "none", 401),
        (SERVING, "admin-access", 401),
        (SERVING, "admin-access-legacy", 401),
        (SERVING, "jwt-secret-signed", 401),
        (SERVING, "jwt-secret-signed-legacy", 401),
        (SERVING, "empty-key-forged", 401),
        (SERVING, "customer", 401),
        (SERVING, "campaign-manager", 403),
        (SERVING, "mobile", 200),
        (EXPLAIN, "none", 401),
        (EXPLAIN, "admin-access", 401),
        (EXPLAIN, "admin-access-legacy", 401),
        (EXPLAIN, "jwt-secret-signed", 401),
        (EXPLAIN, "jwt-secret-signed-legacy", 401),
        (EXPLAIN, "empty-key-forged", 401),
        (EXPLAIN, "customer", 401),
        (EXPLAIN, "mobile", 403),
        (EXPLAIN, "campaign-manager", 200),
    ],
)
async def test_route_auth_matrix(rec_app, mint, admin_access_token,
                                 legacy_admin_access_token, path, kind, expected):
    token = _token(mint, admin_access_token, legacy_admin_access_token, kind)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    resp = await rec_app.client.get(path, headers=headers)
    assert resp.status_code == expected, resp.text
    if expected == 401:
        assert resp.headers.get("WWW-Authenticate") == "Bearer"
    if expected != 200:
        # Отклонённый вызов не дошёл до конвейера: ни записи, ни события.
        assert rec_app.db.statements == []
        assert rec_app.producer.sent == []


async def test_auth_enabled_env_no_longer_disables_checks(rec_app, monkeypatch):
    """Бывший AUTH_ENABLED=false больше ничего не выключает."""
    from app.config import get_settings

    monkeypatch.setenv("AUTH_ENABLED", "false")
    get_settings.cache_clear()
    try:
        for path in (SERVING, EXPLAIN):
            assert (await rec_app.client.get(path)).status_code == 401
    finally:
        get_settings.cache_clear()


async def test_health_live_is_public(rec_app):
    assert (await rec_app.client.get("/health/live")).status_code == 200


async def test_cors_preflight_from_unknown_origin_is_not_allowed(rec_app, mint):
    """B36: по умолчанию CORS_ORIGINS пуст — чужому сайту ACAO не выдаётся."""
    resp = await rec_app.client.get(
        EXPLAIN, headers={"Origin": "https://evil.example",
                          "Authorization": f"Bearer {mint(iss='campaign-manager')}"},
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers
    pre = await rec_app.client.options(
        SERVING, headers={"Origin": "https://evil.example",
                          "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in pre.headers
