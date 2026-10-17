"""Integration: сброс пароля админом против одновременной смены пароля
владельцем учётки (B35, контракт §3) — настоящий Postgres, настоящее
приложение (``create_app``), запросы параллельно в одном event loop.

Обработчики читали строку ``admin_users`` без блокировки и писали
``token_version = k + 1`` значением: две записи давали k+1 вместо k+2, а
пароль — последнего коммита. Админ получал 200 на сброс, а тот, кто
держит учётку, сохранял свой пароль и свежую пару токенов. Теперь строка
блокируется (``SELECT … FOR UPDATE``): кто бы ни взял её первым, в силе
пароль админа, а токены из смены пароля не принимаются.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.test_auth_migrations import _alembic

pytestmark = pytest.mark.integration

BOOTSTRAP_PASSWORD = "Bootstrap-chosen-42"
VICTIM_PASSWORD = "Victim-original-42"
ATTACKER_PASSWORD = "Attacker-chosen-42"
RESET_PASSWORD = "Admin-reset-pass-42"
TRIALS = 3


@pytest.fixture
async def stack(db_dsn):
    import httpx
    from app.db import make_engine, make_sessionmaker
    from app.events import EventBroker
    from app.main import create_app

    from tests.unit.fakes import FakeRedis

    _alembic(db_dsn, "upgrade", "head", password=BOOTSTRAP_PASSWORD)
    engine = make_engine(db_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    app = create_app()
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.redis = FakeRedis()
    app.state.event_broker = EventBroker()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cm") as client:
            yield client
    finally:
        await engine.dispose()


async def _login(client, email: str, password: str):
    return await client.post("/auth/login", json={"email": email, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_admin_reset_wins_over_concurrent_change_password(stack, db_dsn):
    client = stack
    r = await _login(client, "admin@bank.ru", BOOTSTRAP_PASSWORD)
    assert r.status_code == 200, r.text
    admin = _bearer(r.json()["access_token"])

    for trial in range(TRIALS):
        email = f"victim{trial}@bank.ru"
        r = await client.post("/auth/users", headers=admin, json={
            "email": email, "password": VICTIM_PASSWORD,
            "full_name": "Victim", "role": "MARKETER"})
        assert r.status_code == 201, r.text
        victim_id = r.json()["user_id"]
        r = await _login(client, email, VICTIM_PASSWORD)     # учётка у злоумышленника
        assert r.status_code == 200, r.text
        attacker = _bearer(r.json()["access_token"])

        change, reset = await asyncio.gather(
            client.post("/auth/change-password", headers=attacker, json={
                "current_password": VICTIM_PASSWORD, "new_password": ATTACKER_PASSWORD}),
            client.patch(f"/auth/users/{victim_id}", headers=admin,
                         json={"password": RESET_PASSWORD}),
        )

        assert reset.status_code == 200, reset.text
        assert change.status_code in (200, 401), change.text
        # В силе пароль админа, пароль злоумышленника — нет.
        assert (await _login(client, email, RESET_PASSWORD)).status_code == 200, trial
        assert (await _login(client, email, ATTACKER_PASSWORD)).status_code == 401, trial
        # Пара из смены пароля (если та прошла первой) уже отозвана сбросом.
        if change.status_code == 200:
            pair = change.json()
            me = await client.get("/auth/me", headers=_bearer(pair["access_token"]))
            assert me.status_code == 401, (trial, me.text)
            again = await client.post("/auth/refresh",
                                      json={"refresh_token": pair["refresh_token"]})
            assert again.status_code == 401, (trial, again.text)
        (version,) = _version(db_dsn, victim_id)
        assert version == (2 if change.status_code == 200 else 1), trial


def _version(dsn: str, user_id: str) -> tuple[int]:
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT token_version FROM admin_users WHERE user_id = %s",
                        (user_id,))
            return cur.fetchone()
    finally:
        conn.close()
