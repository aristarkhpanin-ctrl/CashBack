"""Регрессия B7: приложение в сборке как в проде принимает запросы.

``get_session_dep(request)`` без аннотации типа превращал ``request`` в
обязательный query-параметр → 36 эндпоинтов (включая ``POST /auth/login``)
отвечали 422. Unit-тесты этого не видели, потому что подменяли зависимость
через ``dependency_overrides``. Здесь — ни одной подмены: модульный
``app.main.app`` (его запускает uvicorn в Dockerfile) и ``create_app()``,
фейковый sessionmaker кладётся в ``app.state``, откуда его читает боевая
зависимость.
"""
from __future__ import annotations

import app.main as main_module
from app.main import create_app
from fastapi.testclient import TestClient

from tests.unit.fakes import FakeSession, fake_sessionmaker


def test_login_on_production_app_object_is_401_not_422():
    prod_app = main_module.app             # ровно то, что запускает uvicorn
    assert prod_app.dependency_overrides == {}
    had_sm = hasattr(prod_app.state, "sessionmaker")
    prod_app.state.sessionmaker = fake_sessionmaker(FakeSession())
    try:
        r = TestClient(prod_app).post(
            "/auth/login", json={"email": "nobody@bank.ru", "password": "wrong"},
        )
    finally:
        if not had_sm:
            del prod_app.state.sessionmaker
    # 422 {"loc": ["query", "request"]} — симптом B7; правильный ответ — 401.
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "invalid email or password"}


def test_openapi_has_no_bogus_request_parameter():
    spec = create_app().openapi()
    operations = [
        (method.upper(), path, op)
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
    ]
    # защита от «пустого» прохода: схема действительно собрана целиком
    assert len(operations) >= 40
    assert "/auth/login" in spec["paths"]

    offenders = [
        f"{method} {path}"
        for method, path, op in operations
        if any(p["name"] == "request" for p in op.get("parameters", []))
    ]
    assert offenders == []
