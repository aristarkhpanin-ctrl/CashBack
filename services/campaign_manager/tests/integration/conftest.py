"""Общие фикстуры интеграционных тестов campaign_manager на настоящем Postgres.

Сервер — ``CM_IT_POSTGRES_DSN`` (DSN с правом CREATEDB, например
``postgresql://cashback:cashback@127.0.0.1:5432/postgres``) или одноразовый
testcontainers-Postgres; каждый тест получает свою свежую БД (``db_dsn``).
"""
from __future__ import annotations

import os
import uuid

import pytest

# Settings без секретов не создаются (B19), а app.main строит приложение при
# импорте; CI экспортирует свои случайные ключи на прогон.
os.environ.setdefault("JWT_SECRET", "integration-test-jwt-secret-0123456789abcdef")
os.environ.setdefault("SERVICE_JWT_SECRET",
                      "integration-test-service-secret-0123456789abcdef")


@pytest.fixture(scope="module")
def server_dsn():
    psycopg2 = pytest.importorskip("psycopg2")
    del psycopg2
    dsn = os.environ.get("CM_IT_POSTGRES_DSN")
    if dsn:
        yield dsn
        return
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture
def db_dsn(server_dsn):
    import psycopg2

    name = f"cm_auth_mig_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(server_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    base = server_dsn.rsplit("/", 1)[0]
    try:
        yield f"{base}/{name}"
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
