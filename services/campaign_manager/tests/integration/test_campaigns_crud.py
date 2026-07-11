"""Integration smoke for the Campaign Manager CRUD path.

Spins up Postgres + Redis via testcontainers, applies the schema for
``cashback_campaigns`` + ``campaign_categories`` (a minimal subset of
the production migration), and exercises the public API through
``httpx.AsyncClient(transport=ASGITransport(app))``.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_container():
    pytest.importorskip("testcontainers")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="module")
def redis_container():
    pytest.importorskip("testcontainers")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7.2-alpine") as r:
        yield r


@pytest.fixture(scope="module")
async def app_client(postgres_container, redis_container):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pg_dsn = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg",
    )
    redis_url = (
        f"redis://{redis_container.get_container_host_ip()}"
        f":{redis_container.get_exposed_port(6379)}/0"
    )

    engine = create_async_engine(pg_dsn)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"))
        await conn.execute(text("""
            DO $$BEGIN
                CREATE TYPE campaign_status AS ENUM ('DRAFT','ACTIVE','PAUSED','COMPLETED');
            EXCEPTION WHEN duplicate_object THEN null; END$$;
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cashback_campaigns (
                campaign_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                name TEXT NOT NULL,
                target_segment_ids INT[] NOT NULL,
                cashback_rate NUMERIC(5,2) NOT NULL,
                min_transaction_amount NUMERIC(15,2),
                budget_total NUMERIC(15,2) NOT NULL,
                budget_spent NUMERIC(15,2) DEFAULT 0,
                status campaign_status DEFAULT 'DRAFT',
                start_date TIMESTAMPTZ NOT NULL,
                end_date TIMESTAMPTZ NOT NULL,
                allowed_channels TEXT[] DEFAULT '{}',
                require_existing_behavior BOOLEAN DEFAULT FALSE,
                rate_tiers JSONB
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS campaign_categories (
                campaign_id UUID REFERENCES cashback_campaigns ON DELETE CASCADE,
                mcc_code CHAR(4),
                min_transaction_amount NUMERIC(15,2),
                PRIMARY KEY(campaign_id, mcc_code)
            )
        """))
        # --- auth (фаза 15): admin_users + три роли -----------------------
        await conn.execute(text("""
            DO $$BEGIN
                CREATE TYPE admin_role AS ENUM ('ADMIN','MARKETER','ANALYST');
            EXCEPTION WHEN duplicate_object THEN null; END$$;
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_users (
                user_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(128) NOT NULL,
                full_name VARCHAR(255) NOT NULL,
                role admin_role NOT NULL DEFAULT 'ANALYST',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_login_at TIMESTAMPTZ
            )
        """))
        from app.security import hash_password
        for email, role in [("admin@test.ru", "ADMIN"),
                            ("marketer@test.ru", "MARKETER"),
                            ("analyst@test.ru", "ANALYST")]:
            await conn.execute(
                text("""
                    INSERT INTO admin_users (email, password_hash, full_name, role)
                    VALUES (:e, :p, :n, :r) ON CONFLICT (email) DO NOTHING
                """),
                {"e": email, "p": hash_password("pass123"), "n": email, "r": role},
            )
    await engine.dispose()

    os.environ["POSTGRES_DSN"] = pg_dsn
    os.environ["REDIS_URL"] = redis_url
    os.environ["CLICKHOUSE_HOST"] = "invalid"

    from app.config import get_settings
    get_settings.cache_clear()

    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        login = await ac.post("/auth/login", json={
            "email": "admin@test.ru", "password": "pass123",
        })
        assert login.status_code == 200, login.text
        ac.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        yield ac


async def _token_for(app_client, email: str) -> str:
    """Логин под другой ролью, не трогая заголовки основного клиента."""
    resp = await app_client.post(
        "/auth/login", json={"email": email, "password": "pass123"},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
async def test_create_get_and_status_transitions(app_client):
    payload = {
        "name": "Test grocery",
        "target_segment_ids": [1, 2, 3],
        "cashback_rate": "3.0",
        "min_transaction_amount": "100.00",
        "budget_total": "10000.00",
        "start_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end_date":   (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "allowed_channels": ["POS", "ONLINE"],
        "mcc_codes": ["5411"],
    }
    create = await app_client.post("/campaigns", json=payload)
    assert create.status_code == 201, create.text
    body = create.json()
    cid = body["campaign_id"]
    assert body["status"] == "DRAFT"
    assert body["mcc_codes"] == ["5411"]

    got = await app_client.get(f"/campaigns/{cid}")
    assert got.status_code == 200
    assert got.json()["campaign_id"] == cid

    activated = await app_client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"},
    )
    assert activated.status_code == 200
    assert activated.json()["current"] == "ACTIVE"

    illegal = await app_client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"},
    )
    assert illegal.status_code == 409  # ACTIVE→activate is forbidden


async def test_invalid_payload_rejected(app_client):
    bad = {
        "name": "x",
        "target_segment_ids": [],   # ← empty list is invalid
        "cashback_rate": "200",     # > 100 — invalid
        "budget_total": "0",        # not > 0
        "start_date": datetime.now(UTC).isoformat(),
        "end_date":   datetime.now(UTC).isoformat(),
        "mcc_codes": ["abc"],       # not 4-digit
    }
    resp = await app_client.post("/campaigns", json=bad)
    assert resp.status_code == 422


async def test_list_all_and_draft_edit(app_client):
    payload = {
        "name": "Editable draft",
        "target_segment_ids": [4, 5],
        "cashback_rate": "5.0",
        "budget_total": "5000.00",
        "start_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end_date":   (datetime.now(UTC) + timedelta(days=10)).isoformat(),
        "mcc_codes": ["5912"],
    }
    created = await app_client.post("/campaigns", json=payload)
    assert created.status_code == 201, created.text
    cid = created.json()["campaign_id"]

    # GET /campaigns returns the draft (full CampaignResponse items).
    listing = await app_client.get("/campaigns")
    assert listing.status_code == 200
    items = listing.json()
    ours = next(i for i in items if i["campaign_id"] == cid)
    assert ours["status"] == "DRAFT"
    assert ours["mcc_codes"] == ["5912"]
    assert ours["target_segment_ids"] == [4, 5]

    # status filter narrows the list
    drafts = await app_client.get("/campaigns", params={"status": "DRAFT"})
    assert any(i["campaign_id"] == cid for i in drafts.json())
    completed = await app_client.get("/campaigns", params={"status": "COMPLETED"})
    assert all(i["campaign_id"] != cid for i in completed.json())

    # PATCH fields while DRAFT — name, rate and mcc set are editable.
    edit = await app_client.patch(f"/campaigns/{cid}", json={
        "name": "Edited draft",
        "cashback_rate": "7.5",
        "mcc_codes": ["5912", "5411"],
    })
    assert edit.status_code == 200, edit.text
    body = edit.json()
    assert body["name"] == "Edited draft"
    assert body["cashback_rate"] == "7.5"
    assert sorted(body["mcc_codes"]) == ["5411", "5912"]

    # invalid dates on merge → 422
    bad_dates = await app_client.patch(f"/campaigns/{cid}", json={
        "end_date": (datetime.now(UTC) - timedelta(days=5)).isoformat(),
    })
    assert bad_dates.status_code == 422

    # after activation the campaign becomes immutable → 409
    await app_client.patch(f"/campaigns/{cid}/status", params={"action": "activate"})
    frozen = await app_client.patch(f"/campaigns/{cid}", json={"name": "nope"})
    assert frozen.status_code == 409


async def _create_active_campaign(app_client, budget: str) -> str:
    payload = {
        "name": f"Budget race {uuid.uuid4().hex[:6]}",
        "target_segment_ids": [1],
        "cashback_rate": "5.0",
        "budget_total": budget,
        "start_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end_date":   (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        "mcc_codes": ["5411"],
    }
    created = await app_client.post("/campaigns", json=payload)
    assert created.status_code == 201, created.text
    cid = created.json()["campaign_id"]
    activated = await app_client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"},
    )
    assert activated.status_code == 200
    return cid


async def test_budget_reservation_happy_path_and_insufficient(app_client):
    cid = await _create_active_campaign(app_client, "1000.00")

    ok = await app_client.post(
        f"/campaigns/{cid}/budget/check", json={"amount": "700.00"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["reserved"] is True
    assert Decimal(body["remaining_budget"]) == Decimal("300.00")

    # Business failure is HTTP 200 + reserved=false, NOT an error status.
    refused = await app_client.post(
        f"/campaigns/{cid}/budget/check", json={"amount": "700.00"},
    )
    assert refused.status_code == 200
    body = refused.json()
    assert body["reserved"] is False
    assert body["reason"] == "insufficient_budget"
    assert Decimal(body["remaining_budget"]) == Decimal("300.00")


async def test_budget_reservation_concurrent_race(app_client):
    """Two parallel reservations race for a budget that fits only one.

    ``SELECT … FOR UPDATE`` in reserve_budget must serialize the requests:
    exactly one wins, the loser sees the already-debited remainder and is
    refused — the budget can never go negative.
    """
    import asyncio

    cid = await _create_active_campaign(app_client, "1000.00")

    r1, r2 = await asyncio.gather(
        app_client.post(f"/campaigns/{cid}/budget/check",
                        json={"amount": "700.00"}),
        app_client.post(f"/campaigns/{cid}/budget/check",
                        json={"amount": "700.00"}),
    )
    assert r1.status_code == 200 and r2.status_code == 200
    outcomes = sorted([r1.json()["reserved"], r2.json()["reserved"]])
    assert outcomes == [False, True], (r1.json(), r2.json())

    loser = r1.json() if not r1.json()["reserved"] else r2.json()
    assert loser["reason"] == "insufficient_budget"
    assert Decimal(loser["remaining_budget"]) == Decimal("300.00")

    # Final state: exactly one debit of 700.
    got = await app_client.get(f"/campaigns/{cid}")
    assert Decimal(got.json()["budget_spent"]) == Decimal("700.00")


async def test_budget_reservation_refused_for_paused_campaign(app_client):
    cid = await _create_active_campaign(app_client, "1000.00")
    await app_client.patch(f"/campaigns/{cid}/status", params={"action": "pause"})

    resp = await app_client.post(
        f"/campaigns/{cid}/budget/check", json={"amount": "10.00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reserved"] is False
    assert body["reason"] == "campaign status=PAUSED"


async def test_auth_matrix(app_client):
    """401 без токена; ANALYST читает, но не мутирует; MARKETER мутирует."""
    payload = {
        "name": "RBAC probe",
        "target_segment_ids": [1],
        "cashback_rate": "1.0",
        "budget_total": "100.00",
        "start_date": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end_date":   (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "mcc_codes": ["5411"],
    }

    # Без токена — 401 и на чтение, и на запись.
    anon_list = await app_client.get("/campaigns", headers={"Authorization": ""})
    assert anon_list.status_code in (401, 403)
    anon_create = await app_client.post(
        "/campaigns", json=payload, headers={"Authorization": ""},
    )
    assert anon_create.status_code in (401, 403)

    # ANALYST: чтение — 200, мутация — 403.
    analyst = {"Authorization": f"Bearer {await _token_for(app_client, 'analyst@test.ru')}"}
    assert (await app_client.get("/campaigns", headers=analyst)).status_code == 200
    denied = await app_client.post("/campaigns", json=payload, headers=analyst)
    assert denied.status_code == 403
    assert "ANALYST" in denied.json()["detail"]

    # MARKETER: мутация — 201, статусные действия доступны.
    marketer = {"Authorization": f"Bearer {await _token_for(app_client, 'marketer@test.ru')}"}
    created = await app_client.post("/campaigns", json=payload, headers=marketer)
    assert created.status_code == 201
    cid = created.json()["campaign_id"]
    activated = await app_client.patch(
        f"/campaigns/{cid}/status", params={"action": "activate"}, headers=marketer,
    )
    assert activated.status_code == 200

    # Управление пользователями — только ADMIN.
    assert (await app_client.get("/auth/users", headers=analyst)).status_code == 403
    admin_users = await app_client.get("/auth/users")
    assert admin_users.status_code == 200
    assert {u["role"] for u in admin_users.json()} >= {"ADMIN", "MARKETER", "ANALYST"}


async def test_login_refresh_and_me(app_client):
    bad = await app_client.post(
        "/auth/login",
        json={"email": "admin@test.ru", "password": "wrong"},
        headers={"Authorization": ""},
    )
    assert bad.status_code == 401

    login = await app_client.post(
        "/auth/login",
        json={"email": "admin@test.ru", "password": "pass123"},
        headers={"Authorization": ""},
    )
    assert login.status_code == 200
    pair = login.json()

    me = await app_client.get("/auth/me", headers={
        "Authorization": f"Bearer {pair['access_token']}",
    })
    assert me.status_code == 200
    assert me.json()["email"] == "admin@test.ru"
    assert me.json()["role"] == "ADMIN"

    # refresh-токен не работает как access…
    as_access = await app_client.get("/auth/me", headers={
        "Authorization": f"Bearer {pair['refresh_token']}",
    })
    assert as_access.status_code == 401

    # …но выдаёт новую пару через /auth/refresh.
    refreshed = await app_client.post(
        "/auth/refresh", json={"refresh_token": pair["refresh_token"]},
        headers={"Authorization": ""},
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


async def test_user_management_lifecycle(app_client):
    created = await app_client.post("/auth/users", json={
        "email": "new.analyst@test.ru", "password": "secret6",
        "full_name": "Новый Аналитик", "role": "ANALYST",
    })
    assert created.status_code == 201, created.text
    uid = created.json()["user_id"]

    # Дубль email → 409.
    dup = await app_client.post("/auth/users", json={
        "email": "new.analyst@test.ru", "password": "secret6",
        "full_name": "Дубль", "role": "ANALYST",
    })
    assert dup.status_code == 409

    # Повышение роли и деактивация.
    updated = await app_client.patch(f"/auth/users/{uid}", json={"role": "MARKETER"})
    assert updated.status_code == 200
    assert updated.json()["role"] == "MARKETER"

    deactivated = await app_client.patch(f"/auth/users/{uid}", json={"is_active": False})
    assert deactivated.status_code == 200

    # Деактивированный пользователь не может залогиниться.
    login = await app_client.post(
        "/auth/login",
        json={"email": "new.analyst@test.ru", "password": "secret6"},
        headers={"Authorization": ""},
    )
    assert login.status_code == 401

    # Самозащита: админ не может деактивировать сам себя.
    me = await app_client.get("/auth/me")
    self_kill = await app_client.patch(
        f"/auth/users/{me.json()['user_id']}", json={"is_active": False},
    )
    assert self_kill.status_code == 409


async def test_metrics_and_root(app_client):
    root = await app_client.get("/")
    assert root.status_code == 200
    assert root.json()["service"] == "campaign-manager"

    metrics = await app_client.get("/metrics")
    assert metrics.status_code == 200
    assert "process_" in metrics.text or "http_requests_total" in metrics.text
