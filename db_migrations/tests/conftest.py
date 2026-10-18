"""Фикстуры тестов миграций на настоящем PostgreSQL.

Сервер — ``MIGRATIONS_IT_POSTGRES_DSN`` (DSN с правом CREATEDB, например
``postgresql://cashback:cashback@127.0.0.1:5432/postgres``) или одноразовый
testcontainers-Postgres (нужен Docker). Нет ни того, ни другого — тесты
пропускаются (локально); CI считает пропуск провалом.

* ``fresh_dsn`` — своя пустая БД на тест (миграции вверх/вниз);
* ``pg`` — соединение с БД на alembic head (одна на сессию), каждый тест —
  в своей транзакции, которая откатывается: тесты не видят данных друг друга.
"""
from __future__ import annotations

import os

import psycopg2
import pytest
from dbm_support import Databases, Pg, plain_dsn, run_alembic

DSN_ENV = "MIGRATIONS_IT_POSTGRES_DSN"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: tests on a real PostgreSQL (alembic migrations)")


@pytest.fixture(scope="session")
def server_dsn():
    dsn = os.environ.get(DSN_ENV)
    if dsn:
        yield plain_dsn(dsn)
        return
    try:    # 4.15+ перенёс модули в testcontainers.community
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError:
            pytest.skip(f"{DSN_ENV} is not set and testcontainers is not installed")
    try:
        container = PostgresContainer("postgres:16-alpine")   # клиент Docker — уже здесь
        container.start()
    except Exception as exc:  # noqa: BLE001 — нет Docker → локальный пропуск
        pytest.skip(f"{DSN_ENV} is not set and Docker is unavailable: {exc}")
    try:
        yield plain_dsn(container.get_connection_url())
    finally:
        container.stop()


@pytest.fixture
def fresh_dsn(server_dsn):
    dbs = Databases(server_dsn)
    try:
        yield dbs.create()
    finally:
        dbs.drop_all()


@pytest.fixture(scope="session")
def head_dsn(server_dsn):
    dbs = Databases(server_dsn)
    try:
        dsn = dbs.create()
        run_alembic(dsn, "upgrade", "head")
        yield dsn
    finally:
        dbs.drop_all()


@pytest.fixture
def pg(head_dsn):
    conn = psycopg2.connect(head_dsn)
    try:
        yield Pg(conn)
    finally:
        conn.rollback()
        conn.close()
