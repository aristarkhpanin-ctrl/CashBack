"""Матрица «маршрут → требование» (контракт этапа B §4; B35).

Статическая часть обходит ``app.routes``, раскрывает дерево зависимостей
каждого маршрута и сравнивает требование с таблицей ``EXPECTED`` — ровно с
ней: маршрут без строки в таблице или с другим требованием роняет CI, так
что новый эндпоинт без права незамеченным не уедет.

Поведенческая часть прогоняет те же маршруты через настоящее приложение:
без токена — 401; роль, у которой есть все права, кроме нужного, — 403;
роль только с нужным правом проходит проверку. Плюс правила учёток ADMIN
(право ``users`` делегируемо, роль ADMIN — нет).
"""
from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from app.api.events import require_sse_token
from app.main import create_app
from app.models import AdminUser, RolePermission
from app.rbac import PERMISSION_KEYS
from app.security import get_current_user, get_current_user_allow_pwd_change
from fastapi import Depends, FastAPI, WebSocket
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

PUBLIC: frozenset[str] = frozenset()
SSE_TOKEN = frozenset({"sse_token"})
AUTH = frozenset({"auth"})
AUTH_PWD_CHANGE = frozenset({"auth:pwd_change"})
ROLE_ADMIN = frozenset({"auth", "role:ADMIN"})


def PERM(key: str) -> frozenset[str]:  # noqa: N802 — читается как таблица
    assert key in PERMISSION_KEYS
    return frozenset({"auth", f"perm:{key}"})


# Таблица §4 контракта этапа B (+ этап C §3: budget/check убран, добавлены
# GET …/budget и POST …/archive). "/x/*" — любой путь под префиксом.
EXPECTED: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/health/live"): PUBLIC,
    ("GET", "/health/ready"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("POST", "/auth/refresh"): PUBLIC,
    ("POST", "/auth/logout"): PUBLIC,
    ("GET", "/events/stream"): SSE_TOKEN,
    ("GET", "/auth/me"): AUTH_PWD_CHANGE,
    ("POST", "/auth/change-password"): AUTH_PWD_CHANGE,
    ("GET", "/reference/*"): AUTH,
    ("POST", "/events/token"): PERM("dashboard"),
    ("GET", "/campaigns"): PERM("campaigns_view"),
    ("GET", "/campaigns/active"): PERM("campaigns_view"),
    ("GET", "/campaigns/{campaign_id}"): PERM("campaigns_view"),
    ("GET", "/campaigns/{campaign_id}/stats"): PERM("campaigns_view"),
    ("GET", "/campaigns/{campaign_id}/budget"): PERM("campaigns_view"),
    ("GET", "/campaigns/audience/estimate"): PERM("campaigns_view"),
    ("GET", "/campaigns/applicable/{user_id}"): PERM("campaigns_view"),
    ("POST", "/campaigns"): PERM("campaigns_create"),
    ("PATCH", "/campaigns/{campaign_id}"): PERM("campaigns_edit"),
    ("PATCH", "/campaigns/{campaign_id}/status"): PERM("campaigns_edit"),
    ("DELETE", "/campaigns/{campaign_id}"): PERM("campaigns_delete"),
    ("POST", "/campaigns/{campaign_id}/archive"): PERM("campaigns_delete"),
    ("GET", "/analytics/*"): PERM("analytics"),
    ("GET", "/experiments"): PERM("analytics"),
    ("GET", "/experiments/{experiment_id}"): PERM("analytics"),
    ("GET", "/experiments/{experiment_id}/results"): PERM("analytics"),
    ("GET", "/ml/customers"): PERM("analytics"),
    ("GET", "/ml/explain/{user_id}"): PERM("analytics"),
    ("GET", "/ml-limits"): PERM("analytics"),
    ("POST", "/experiments"): PERM("campaigns_edit"),
    ("PATCH", "/experiments/{experiment_id}/status"): PERM("campaigns_edit"),
    ("POST", "/experiments/{experiment_id}/assign/{user_id}"): PERM("campaigns_edit"),
    ("PUT", "/ml-limits"): ROLE_ADMIN,
    ("GET", "/auth/users"): PERM("users"),
    ("POST", "/auth/users"): PERM("users"),
    ("PATCH", "/auth/users/{user_id}"): PERM("users"),
    ("GET", "/roles/permissions"): PERM("users"),
    ("PATCH", "/roles/{role}/permissions"): ROLE_ADMIN,
}

# Служебные маршруты вне таблицы контракта — публичны сознательно: метаданные
# сервиса, метрики Prometheus и схема/документация OpenAPI.
INFRA_PUBLIC = {
    ("GET", "/"), ("GET", "/metrics"), ("GET", "/openapi.json"),
    ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"),
}
# Единственные маршруты не-APIRoute — схема и документация FastAPI.
# WebSocket, Mount, Host и т.п. зависимости-права не проверяют: такой маршрут
# прошёл бы матрицу молча, поэтому он сам по себе нарушение.
DOCS_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _expected(method: str, path: str) -> frozenset[str] | None:
    if (method, path) in INFRA_PUBLIC:
        return PUBLIC
    for (m, pattern), req in EXPECTED.items():
        if m == method and (pattern == path or (
                pattern.endswith("/*") and path.startswith(pattern[:-1]))):
            return req
    return None


def _calls(dependant: Dependant) -> Iterator[object]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _requirement(route: Route) -> frozenset[str]:
    """Что проверяют зависимости маршрута (роутера и самого маршрута)."""
    if not isinstance(route, APIRoute):          # /docs, /openapi.json …
        return PUBLIC
    marks: set[str] = set()
    for call in _calls(route.dependant):
        if call is get_current_user:
            marks.add("auth")
        elif call is get_current_user_allow_pwd_change:
            marks.add("auth:pwd_change")
        elif call is require_sse_token:
            marks.add("sse_token")
        elif getattr(call, "__qualname__", "") == "require_permission.<locals>._dep":
            marks.add(f"perm:{inspect.getclosurevars(call).nonlocals['key']}")
        elif getattr(call, "__qualname__", "") == "require_role.<locals>._dep":
            allowed = inspect.getclosurevars(call).nonlocals["allowed"]
            marks.add("role:" + ",".join(sorted(allowed)))
    return frozenset(marks)


def _endpoints(app: FastAPI) -> Iterator[tuple[str, str, Route]]:
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method != "HEAD":
                yield method, route.path, route


def _violations(app: FastAPI) -> list[str]:
    out, seen = [], set()
    for method, path, route in _endpoints(app):
        expected, actual = _expected(method, path), _requirement(route)
        if expected is None:
            out.append(f"{method} {path}: not in the permission table "
                       f"(has {sorted(actual)})")
        elif actual != expected:
            out.append(f"{method} {path}: expected {sorted(expected)}, "
                       f"got {sorted(actual)}")
        seen.add((method, path))
    for (method, pattern) in EXPECTED:
        if not any(m == method and (p == pattern or (
                pattern.endswith("/*") and p.startswith(pattern[:-1])))
                for m, p in seen):
            out.append(f"{method} {pattern}: in the table but no such route")
    for route in app.routes:
        if isinstance(route, APIRoute) or (
                type(route) is Route and route.path in DOCS_ROUTES):
            continue
        where = getattr(route, "path", None) or getattr(route, "host", "?")
        out.append(f"{type(route).__name__} {where}: not an HTTP API route "
                   f"(permissions are not checked)")
    return out


# ── статическая матрица ───────────────────────────────────────────────────────
def test_every_route_matches_the_permission_table_exactly():
    assert _violations(create_app()) == []


def test_new_route_without_permission_breaks_the_check():
    app = create_app()

    @app.get("/campaigns/export/csv", dependencies=[Depends(get_current_user)])
    async def _export() -> None: ...

    @app.post("/unlisted")
    async def _unlisted() -> None: ...

    @app.websocket("/events/ws")
    async def _ws(websocket: WebSocket) -> None: ...

    app.mount("/internal", FastAPI())

    assert _violations(app) == [
        "GET /campaigns/export/csv: not in the permission table (has ['auth'])",
        "POST /unlisted: not in the permission table (has [])",
        "APIWebSocketRoute /events/ws: not an HTTP API route (permissions are not checked)",
        "Mount /internal: not an HTTP API route (permissions are not checked)",
    ]


def test_route_with_weaker_requirement_breaks_the_check():
    app = create_app()
    (route,) = (r for r in app.routes
                if isinstance(r, APIRoute) and r.path == "/analytics/kpis")
    app.router.routes.remove(route)

    @app.get("/analytics/kpis", dependencies=[Depends(get_current_user)])
    async def _kpis() -> None: ...

    assert _violations(app) == [
        "GET /analytics/kpis: expected ['auth', 'perm:analytics'], got ['auth']",
    ]


# ── поведение тех же маршрутов ────────────────────────────────────────────────
_PATH_VALUES = {"campaign_id": str(uuid.uuid4()), "experiment_id": str(uuid.uuid4()),
                "user_id": str(uuid.uuid4()), "role": "ANALYST"}
_GUARDED = [
    (method, path, _expected(method, path))
    for method, path, _ in _endpoints(create_app())
    if _expected(method, path) not in (None, PUBLIC)
]


def _url(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: _PATH_VALUES[m.group(1)], path)


def _grant(db, role: str, perms: dict[str, bool]) -> None:
    db.put(RolePermission(role=role, permissions=perms))


@pytest.fixture
def raw_client(app) -> TestClient:
    # Обработчик за пройденной проверкой может упасть на фейковой БД — это
    # 500, а не исключение в тесте: здесь важен только статус проверки.
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("method", "path", "req"), _GUARDED,
                         ids=[f"{m} {p}" for m, p, _ in _GUARDED])
def test_guarded_route_without_token_is_401(raw_client, method, path, req):
    assert raw_client.request(method, _url(path)).status_code == 401


_PERM_ROUTES = [(m, p, mark.removeprefix("perm:"))
                for m, p, r in _GUARDED for mark in r if mark.startswith("perm:")]


@pytest.mark.parametrize(("method", "path", "key"), _PERM_ROUTES,
                         ids=[f"{m} {p}" for m, p, _ in _PERM_ROUTES])
def test_permission_route_checks_exactly_its_permission(raw_client, db, auth,
                                                         method, path, key):
    headers = auth("MARKETER")
    _grant(db, "MARKETER", {k: k != key for k in PERMISSION_KEYS})
    r = raw_client.request(method, _url(path), headers=headers)
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == f"role MARKETER lacks permission '{key}'"

    _grant(db, "MARKETER", {k: k == key for k in PERMISSION_KEYS})
    r = raw_client.request(method, _url(path), headers=headers)
    assert r.status_code not in (401, 403), r.text      # других прав не нужно


_ADMIN_ROUTES = [(m, p) for m, p, r in _GUARDED if r == ROLE_ADMIN]


@pytest.mark.parametrize(("method", "path"), _ADMIN_ROUTES,
                         ids=[f"{m} {p}" for m, p in _ADMIN_ROUTES])
def test_admin_only_route_is_not_delegable(raw_client, db, auth, method, path):
    _grant(db, "MARKETER", {k: True for k in PERMISSION_KEYS})
    r = raw_client.request(method, _url(path), headers=auth("MARKETER"))
    assert r.status_code == 403, r.text
    r = raw_client.request(method, _url(path), headers=auth("ADMIN"))
    assert r.status_code not in (401, 403), r.text


@pytest.mark.parametrize("path", ["/analytics/kpis", "/ml/customers",
                                  f"/ml/explain/{uuid.uuid4()}", "/experiments",
                                  "/ml-limits"])
def test_marketer_with_default_permissions_gets_no_analytics(client, auth, path):
    """Атака из B35: MARKETER (analytics=false по умолчанию) читал отчёты и
    UUID клиентов — роутеры требовали только логин."""
    r = client.get(path, headers=auth("MARKETER"))
    assert r.status_code == 403
    assert r.json()["detail"] == "role MARKETER lacks permission 'analytics'"


# ── учётки ADMIN: право users делегируемо, роль ADMIN — нет ──────────────────
_PASSWORD = "s3cret-pass"


@pytest.fixture
def user_manager(db, auth) -> dict[str, str]:
    """MARKETER, которому админ выдал право users."""
    _grant(db, "MARKETER", {"users": True})
    return auth("MARKETER")


def _put_user(db, role: str) -> AdminUser:
    user = AdminUser(user_id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@bank.ru",
                     password_hash="x", full_name=role.title(), role=role,
                     created_at=datetime(2026, 1, 1, tzinfo=UTC))
    db.put(user)
    return user


def test_users_permission_lets_non_admin_manage_non_admin_accounts(
        client, db, user_manager):
    assert client.get("/auth/users", headers=user_manager).status_code == 200
    r = client.post("/auth/users", headers=user_manager, json={
        "email": "new@bank.ru", "password": _PASSWORD, "full_name": "N",
        "role": "ANALYST"})
    assert r.status_code == 201, r.text
    target = _put_user(db, "ANALYST")
    r = client.patch(f"/auth/users/{target.user_id}", headers=user_manager,
                     json={"role": "MARKETER", "full_name": "Повышен"})
    assert r.status_code == 200, r.text
    assert target.role == "MARKETER"


def test_non_admin_cannot_create_admin(client, db, user_manager):
    r = client.post("/auth/users", headers=user_manager, json={
        "email": "evil@bank.ru", "password": _PASSWORD, "full_name": "E",
        "role": "ADMIN"})
    assert r.status_code == 403
    assert all(u.email != "evil@bank.ru" for u in db.rows(AdminUser))


@pytest.mark.parametrize("target_role", ["ANALYST", "MARKETER"])
def test_non_admin_cannot_promote_to_admin(client, db, user_manager, target_role):
    target = _put_user(db, target_role)
    r = client.patch(f"/auth/users/{target.user_id}", headers=user_manager,
                     json={"role": "ADMIN"})
    assert r.status_code == 403
    assert target.role == target_role and target.token_version == 0


def test_non_admin_cannot_promote_self(client, db, user_manager):
    me = db.tables[AdminUser][uuid.UUID("00000000-0000-0000-0000-000000000002")]
    r = client.patch(f"/auth/users/{me.user_id}", headers=user_manager,
                     json={"role": "ADMIN"})
    assert r.status_code == 403 and me.role == "MARKETER"


@pytest.mark.parametrize("patch", [{"is_active": False}, {"password": _PASSWORD},
                                   {"full_name": "x"}, {"role": "ANALYST"}])
def test_non_admin_cannot_change_admin_account(client, db, user_manager, patch):
    admin = _put_user(db, "ADMIN")
    r = client.patch(f"/auth/users/{admin.user_id}", headers=user_manager, json=patch)
    assert r.status_code == 403
    assert admin.role == "ADMIN" and admin.is_active is True
    assert admin.password_hash == "x" and admin.token_version == 0


def test_admin_can_create_and_promote_admins(client, db, auth):
    headers = auth("ADMIN")
    r = client.post("/auth/users", headers=headers, json={
        "email": "second@bank.ru", "password": _PASSWORD, "full_name": "S",
        "role": "ADMIN"})
    assert r.status_code == 201, r.text
    target = _put_user(db, "ANALYST")
    r = client.patch(f"/auth/users/{target.user_id}", headers=headers,
                     json={"role": "ADMIN"})
    assert r.status_code == 200 and target.role == "ADMIN"
