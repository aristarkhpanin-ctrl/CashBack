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

import json
from pathlib import Path

import app.main as main_module
import pytest
from app.config import get_settings
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


# ── CORS (B36) ────────────────────────────────────────────────────────────────
def _preflight(client: TestClient, origin: str):
    return client.options("/campaigns", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })


@pytest.fixture
def cors_origins(monkeypatch):
    def _set(value: str | None):
        if value is None:
            monkeypatch.delenv("CORS_ORIGINS", raising=False)
        else:
            monkeypatch.setenv("CORS_ORIGINS", value)
        get_settings.cache_clear()
        return TestClient(create_app())
    yield _set
    get_settings.cache_clear()


def test_cors_by_default_trusts_no_foreign_origin(cors_origins):
    """Было: CORS ``*`` + credentials — любой сайт читал API (и SSE) от имени
    залогиненного админа. По умолчанию — ни одного чужого origin."""
    client = cors_origins(None)
    r = _preflight(client, "https://evil.example")
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers
    r = client.get("/", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_cors_allows_credentials_only_for_configured_origins(cors_origins):
    client = cors_origins("http://localhost:3000,http://localhost:5173")
    r = _preflight(client, "http://localhost:5173")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert r.headers["access-control-allow-credentials"] == "true"
    r = _preflight(client, "https://evil.example")
    assert "access-control-allow-origin" not in r.headers


# ── Dockerfile (B36) ──────────────────────────────────────────────────────────
def test_dockerfile_bounds_graceful_shutdown():
    """Открытые SSE-потоки держали остановку uvicorn без ограничения."""
    text = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    (cmd,) = (line for line in text.replace("\\\n", " ").splitlines()
              if line.startswith("CMD "))
    argv = json.loads(cmd.removeprefix("CMD "))
    assert argv[:2] == ["uvicorn", "app.main:app"]
    i = argv.index("--timeout-graceful-shutdown")
    assert argv[i + 1] == "10"
    # Клиент за доверенным прокси (FORWARDED_ALLOW_IPS) — для троттлинга логина.
    assert "--proxy-headers" in argv
