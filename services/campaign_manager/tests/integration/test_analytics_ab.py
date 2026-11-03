"""Integration: аналитика и A/B на настоящем Postgres со схемой alembic head
(миграция 010) — этап D, контракт §2–§3 (B9, B11, B33, B34).

Ручная выборка с известными ответами (``_TRUTH``) — клиенты из разных
сегментов, офферы во всех статусах (EXPIRED и PENDING — не «открыто»),
несколько принятых офферов у одного клиента, начисления PENDING/PAID и без
оффера, каналы с NULL, когорта на 18 месяцев, архивная и завершённая
кампании — и точные ожидаемые числа для КАЖДОГО SQL аналитики. Все
стадии воронки — различные клиенты. A/B: контроль — явный ``is_control``
(не первый по алфавиту), заведомо худший тест получает ``winner=control``,
дубли конверсий не дают долю > 1, назначение идемпотентно под гонкой.

Выборка — одна БД на модуль (только чтение); тесты, которые что-то меняют
(расчёт начислений, «нет работающих кампаний», дубли конверсий), — каждый
на своей свежей БД (``db_dsn``).
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tests.integration.test_auth_migrations import _alembic

pytestmark = pytest.mark.integration

BOOTSTRAP_PASSWORD = "Bootstrap-chosen-42"

# ── выборка ───────────────────────────────────────────────────────────────────
# Время — от полудня UTC: N дней назад — всегда в прошлом и далеко от границ
# окон 30/60 дней.
TODAY = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


def noon(days_ago: int) -> datetime:
    return TODAY - timedelta(days=days_ago)


def day(days_ago: int) -> str:
    return noon(days_ago).date().isoformat()


# Клиенты: ключ → дециль. c1..c5 — когорты 2024-01 (c1..c4) и 2024-02 (c5).
USERS = {"u1": 9, "u2": 10, "u3": 5, "u4": 6, "u5": 3, "u6": 1, "u7": 2, "u8": 9,
         "c1": 5, "c2": 5, "c3": 5, "c4": 5, "c5": 5}
COHORT_CREATED = {"c1": "2024-01-05", "c2": "2024-01-12", "c3": "2024-01-20",
                  "c4": "2024-01-28", "c5": "2024-02-10"}

# Кампании: ключ → (имя, статус, архив, целевые сегменты, бюджет, потрачено)
CAMPAIGNS = {
    "A": ("Alpha", "ACTIVE", False, [1, 3, 5, 6, 9, 10], "10000", "195"),
    "B": ("Bravo", "PAUSED", False, [], "5000", "15"),        # [] — все клиенты
    "C": ("Charlie", "COMPLETED", False, [], "1000", "120"),
    "D": ("Delta", "PAUSED", True, [], "2000", "0"),          # в архиве
}

# Офферы: (ключ, клиент, кампания, MCC, статус, канал, дней назад, начисление)
# начисление — (покупка, кэшбэк, статус) или None.
OFFERS = [
    ("a1", "u1", "A", "5411", "ACCEPTED", "PUSH", 2, ("1000", "50", "PAID")),
    ("a2", "u1", "A", "5411", "ACCEPTED", "IN_APP", 3, ("600", "30", "PENDING")),
    ("a3", "u2", "A", "5812", "ACCEPTED", "PUSH", 4, ("400", "20", "PENDING")),
    ("a4", "u2", "A", "5411", "DECLINED", "SMS", 5, None),
    ("a5", "u3", "A", "5411", "ACCEPTED", None, 6, None),
    ("a6", "u3", "A", "5812", "EXPIRED", "PUSH", 7, None),
    ("a7", "u4", "A", "5411", "EXPIRED", "PUSH", 8, None),
    ("a8", "u5", "A", "5411", "SNOOZE", "EMAIL", 9, None),
    ("a9", "u6", "A", "5411", "PENDING", "PUSH", 1, None),
    ("b1", "u7", "B", "5411", "ACCEPTED", "SMS", 3, ("500", "15", "PAID")),
    ("b2", "u1", "B", "5411", "DECLINED", "PUSH", 4, None),
    ("b3", "u6", "B", "5411", "ACCEPTED", "EMAIL", 5, None),
    ("c0", "u2", "C", "5411", "ACCEPTED", "PUSH", 2, ("200", "10", "PENDING")),
    ("d1", "u3", "D", "5411", "DECLINED", "PUSH", 2, None),
    ("old", "u8", "A", "5411", "ACCEPTED", "PUSH", 100, ("800", "40", "PAID")),
]
# Предыдущее окно KPI (30–60 дней назад): 20 офферов A клиентам u1..u4 по
# кругу, приняты p0 (u1) и p3 (u4) — оба оплачены; плюс начисление без оффера.
PREV_OFFERS = 20
PREV_ACCEPTED = {0: "u1", 3: "u4"}

# Начисления когорт (кампания C, без оффера): клиент → (дата, статус)
COHORT_ACCRUALS = [
    ("c1", "2024-01-15", "PAID"), ("c1", "2024-03-10", "PAID"),
    ("c1", "2025-01-20", "PAID"), ("c1", "2025-03-05", "PAID"),
    ("c2", "2024-01-20", "PENDING"), ("c2", "2025-01-05", "PENDING"),
    ("c2", "2025-01-25", "PENDING"),
    ("c3", "2024-02-01", "PAID"), ("c3", "2025-06-30", "PAID"),
    ("c5", "2024-02-15", "PAID"), ("c5", "2025-02-15", "PAID"),
]


def _load_truth(conn) -> dict[str, uuid.UUID]:
    """Записать выборку; ключ клиента/кампании/оффера → его UUID."""
    ids: dict[str, uuid.UUID] = {}
    with conn, conn.cursor() as cur:
        for key, segment in USERS.items():
            ids[key] = uuid.uuid4()
            created = (f"{COHORT_CREATED[key]} 12:00+00" if key in COHORT_CREATED
                       else TODAY - timedelta(days=200))
            cur.execute("INSERT INTO users (user_id, external_id, segment_id, created_at) "
                        "VALUES (%s, %s, %s, %s)",
                        (str(ids[key]), f"truth-{key}", segment, created))
        for key, (name, status, archived, segs, total, spent) in CAMPAIGNS.items():
            ids[key] = uuid.uuid4()
            cur.execute("""
                INSERT INTO cashback_campaigns (campaign_id, name, target_segment_ids,
                       cashback_rate, budget_total, budget_spent, status, start_date,
                       end_date, archived_at)
                VALUES (%s, %s, %s::int[], 5, %s, %s, %s, %s, %s, %s)""",
                        (str(ids[key]), name, segs, total, spent, status,
                         "2023-12-01 00:00+00",
                         TODAY - timedelta(days=1) if status == "COMPLETED"
                         else TODAY + timedelta(days=30),
                         TODAY if archived else None))
            cur.execute("INSERT INTO campaign_categories (campaign_id, mcc_code) "
                        "VALUES (%s, '5411'), (%s, '5812')", (str(ids[key]),) * 2)

        def offer(key, user, camp, mcc, status, channel, at, accrual):
            ids[key] = uuid.uuid4()
            responded = (at + timedelta(hours=1)
                         if status in ("ACCEPTED", "DECLINED", "SNOOZE") else None)
            cur.execute("""
                INSERT INTO recommendations (recommendation_id, user_id, campaign_id,
                       mcc_code, model_score, generated_at, response_status, expires_at,
                       responded_at, channel)
                VALUES (%s, %s, %s, %s, 0.5, %s, %s, %s, %s, %s)""",
                        (str(ids[key]), str(ids[user]), str(ids[camp]), mcc, at, status,
                         at + timedelta(days=7), responded, channel))
            if accrual is not None:
                amount, cashback, accrual_status = accrual
                cur.execute("""
                    INSERT INTO cashback_accruals (user_id, campaign_id, recommendation_id,
                           transaction_id, mcc_code, transaction_amount, cashback_amount,
                           status, accrued_at, paid_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                            CASE WHEN %s = 'PAID' THEN %s END)""",
                            (str(ids[user]), str(ids[camp]), str(ids[key]), f"tx-{key}",
                             mcc, amount, cashback, accrual_status, at + timedelta(hours=2),
                             accrual_status, at + timedelta(days=1)))

        for key, user, camp, mcc, status, channel, ago, accrual in OFFERS:
            offer(key, user, camp, mcc, status, channel, noon(ago), accrual)
        for i in range(PREV_OFFERS):
            user = ("u1", "u2", "u3", "u4")[i % 4]
            accepted = PREV_ACCEPTED.get(i) == user
            offer(f"p{i}", user, "A", "5411", "ACCEPTED" if accepted else "DECLINED",
                  "PUSH", noon(40) + timedelta(hours=i),
                  ("500", "25", "PAID") if accepted else None)

        def bare_accrual(user, camp, tx, amount, cashback, status, at):
            cur.execute("""
                INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id,
                       mcc_code, transaction_amount, cashback_amount, status, accrued_at)
                VALUES (%s, %s, %s, '5411', %s, %s, %s, %s)""",
                        (str(ids[user]), str(ids[camp]), tx, amount, cashback, status, at))

        bare_accrual("u2", "A", "tx-legacy", "100", "5", "PENDING", noon(41))
        for i, (user, on, status) in enumerate(COHORT_ACCRUALS):
            bare_accrual(user, "C", f"tx-cohort-{i}", "300", "10", status,
                         f"{on} 12:00+00")
    return ids


@pytest.fixture(scope="module")
def truth(server_dsn):
    import psycopg2

    name = f"cm_truth_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(server_dsn)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    dsn = f"{server_dsn.rsplit('/', 1)[0]}/{name}"
    try:
        _alembic(dsn, "upgrade", "head", password=BOOTSTRAP_PASSWORD)
        conn = psycopg2.connect(dsn)
        try:
            ids = _load_truth(conn)
        finally:
            conn.close()
        yield dsn, ids
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


@asynccontextmanager
async def _api(dsn: str):
    """Настоящее приложение (create_app) через ASGI, вход админом."""
    import httpx
    from app.db import make_engine, make_sessionmaker
    from app.events import EventBroker
    from app.main import create_app

    from tests.unit.fakes import FakeRedis

    engine = make_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    app = create_app()
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.redis = FakeRedis()
    app.state.event_broker = EventBroker()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://cm") as client:
            r = await client.post("/auth/login", json={
                "email": "admin@bank.ru", "password": BOOTSTRAP_PASSWORD})
            assert r.status_code == 200, r.text
            client.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
            client.engine = engine
            yield client
    finally:
        await engine.dispose()


@pytest.fixture
async def api(truth):
    dsn, ids = truth
    async with _api(dsn) as client:
        client.dsn, client.ids = dsn, ids
        yield client


@pytest.fixture
async def fresh(db_dsn):
    _alembic(db_dsn, "upgrade", "head", password=BOOTSTRAP_PASSWORD)
    async with _api(db_dsn) as client:
        yield client


async def _sql(engine, sql: str, **params):
    from sqlalchemy import text

    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params)
        return result.fetchall() if result.returns_rows else None


async def _get(client, path: str, **params):
    r = await client.get(path, params=params)
    assert r.status_code == 200, f"{path} {params}: {r.status_code} {r.text}"
    return r.json()


# ── воронка: различные клиенты на каждой стадии ──────────────────────────────
def _steps(body) -> list[tuple[str, int, float]]:
    return [(s["name"], s["count"], s["drop_off_pct"]) for s in body["steps"]]


async def test_funnel_counts_distinct_users_at_every_stage(api):
    """13 клиентов; за 30 дней офферы получили 7 (u8 — только 100 дней
    назад); ответили 6 (у u4 только EXPIRED); приняли 5; купили по принятому
    офферу 3 (у u1 два оплаченных оффера — один клиент); выплачено (PAID) 2.
    Раньше: строки рекомендаций и начислений, EXPIRED — «открыто»
    (13 → 13 → 12 → 7 → 5 → 2)."""
    body = await _get(api, "/analytics/funnel")
    assert _steps(body) == [
        ("target_audience", 13, 0.0), ("received", 7, 46.15), ("opened", 6, 14.29),
        ("accepted", 5, 16.67), ("transacted", 3, 40.0), ("cashback_paid", 2, 33.33)]
    assert not any(s["pending"] for s in body["steps"])


async def test_funnel_of_a_campaign_with_target_segments(api):
    body = await _get(api, "/analytics/funnel", campaign_id=str(api.ids["A"]))
    assert _steps(body) == [
        ("target_audience", 12, 0.0), ("received", 6, 50.0), ("opened", 4, 33.33),
        ("accepted", 3, 25.0), ("transacted", 2, 33.33), ("cashback_paid", 1, 50.0)]


async def test_funnel_of_a_campaign_for_everyone_targets_all_users(api):
    """Пустые target_segment_ids — кампания для всех (как eligible_campaigns);
    раньше аудитория была 0 и воронка начиналась с «0 → 3»."""
    body = await _get(api, "/analytics/funnel", campaign_id=str(api.ids["B"]))
    assert _steps(body) == [
        ("target_audience", 13, 0.0), ("received", 3, 76.92), ("opened", 3, 0.0),
        ("accepted", 2, 33.33), ("transacted", 1, 50.0), ("cashback_paid", 1, 0.0)]


async def test_funnel_with_segment_filter(api):
    body = await _get(api, "/analytics/funnel", segment_id="premium")
    assert _steps(body) == [
        ("target_audience", 3, 0.0), ("received", 2, 33.33), ("opened", 2, 0.0),
        ("accepted", 2, 0.0), ("transacted", 2, 0.0), ("cashback_paid", 1, 50.0)]


async def test_funnel_is_monotone_on_every_cut(api):
    for params in ({}, {"campaign_id": str(api.ids["A"])}, {"segment_id": "mass"},
                   {"period": 60}, {"period": 365}):
        counts = [s["count"] for s in (await _get(api, "/analytics/funnel",
                                                   **params))["steps"]]
        assert counts[1:] == sorted(counts[1:], reverse=True), params


async def test_funnel_ignores_purchases_outside_the_offers_of_the_window(fresh):
    """Покупка по офферу, выданному до окна, и начисление без оффера (до 009)
    — не «купили» в окне: стадии — принятые офферы окна. Раньше считались
    все начисления окна: 1 принявший → 2 «купивших», отсев −100%."""
    e = fresh.engine
    uid, cid = uuid.uuid4(), uuid.uuid4()
    await _sql(e, "INSERT INTO users (user_id, external_id, segment_id) "
                  "VALUES (:uid, 'lagging', 5)", uid=uid)
    await _sql(e, """
        INSERT INTO cashback_campaigns (campaign_id, name, cashback_rate, budget_total,
               budget_spent, status, start_date, end_date)
        VALUES (:cid, 'Lag', 5, 1000, 100, 'ACTIVE', now() - interval '90 days',
                now() + interval '10 days')""", cid=cid)
    for rid, ago in ((uuid.uuid4(), 40), (uuid.uuid4(), 5)):     # до окна, в окне
        await _sql(e, """
            INSERT INTO recommendations (recommendation_id, user_id, campaign_id,
                   mcc_code, model_score, generated_at, response_status, expires_at,
                   responded_at)
            VALUES (:rid, :uid, :cid, '5411', 0.5, now() - make_interval(days => :ago),
                    'ACCEPTED', now() + interval '1 day',
                    now() - make_interval(days => :ago))""", rid=rid, uid=uid, cid=cid,
                   ago=ago)
        if ago == 40:                       # куплено по старому офферу — в окне
            await _sql(e, """
                INSERT INTO cashback_accruals (user_id, campaign_id, recommendation_id,
                       transaction_id, mcc_code, transaction_amount, cashback_amount,
                       status, accrued_at, paid_at)
                VALUES (:uid, :cid, :rid, 'tx-lag', '5411', 1000, 50, 'PAID',
                        now() - interval '10 days', now() - interval '9 days')""",
                       uid=uid, cid=cid, rid=rid)
    await _sql(e, """
        INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id, mcc_code,
               transaction_amount, cashback_amount, accrued_at)
        VALUES (:uid, :cid, 'tx-bare', '5411', 1000, 50, now() - interval '3 days')""",
               uid=uid, cid=cid)

    body = await _get(fresh, "/analytics/funnel")
    assert _steps(body) == [
        ("target_audience", 1, 0.0), ("received", 1, 0.0), ("opened", 1, 0.0),
        ("accepted", 1, 0.0), ("transacted", 0, 100.0), ("cashback_paid", 0, 0.0)]
    # за 60 дней старый оффер в окне — его покупка и выплата на месте
    assert _steps(await _get(fresh, "/analytics/funnel", period=60))[4:] == [
        ("transacted", 1, 0.0), ("cashback_paid", 1, 0.0)]


# ── KPI ───────────────────────────────────────────────────────────────────────
async def test_kpis_of_running_campaigns(api):
    """A (ACTIVE) и B (PAUSED); C завершена, D в архиве. Окно 30 дней: 12
    доставок, 7 клиентов, 6 принятых → CTR 0.5; предыдущее окно: 20
    доставок, 4 клиента, CTR 0.1. Тренд расхода — по 3 начислениям
    предыдущего окна (< KPI_TREND_MIN_BASE = 20) → null."""
    body = await _get(api, "/analytics/kpis")
    assert body == {
        "campaigns_count": 2, "reach": 7, "spent": "210.00", "budget": "15000.00",
        "avg_ctr": 0.5, "trends": {"reach": 75.0, "spent": None, "ctr": 400.0},
        "trend_window_days": 30, "has_data": True,
    }


async def test_kpis_of_one_campaign_without_history_have_no_trends(api):
    body = await _get(api, "/analytics/kpis", campaign_id=str(api.ids["C"]))
    assert body == {
        "campaigns_count": 1, "reach": 1, "spent": "120.00", "budget": "1000.00",
        "avg_ctr": 1.0, "trends": {"reach": None, "spent": None, "ctr": None},
        "trend_window_days": 30, "has_data": True,
    }


async def test_kpis_with_segment_filter(api):
    # premium: 5 доставок 2 клиентам, 3 приняты; в предыдущем окне — 10
    # доставок (< 20) → без трендов
    body = await _get(api, "/analytics/kpis", segment_id="premium", period=30)
    assert (body["reach"], body["avg_ctr"], body["trends"]) == (
        2, 0.6, {"reach": None, "spent": None, "ctr": None})
    assert body["campaigns_count"] == 2


async def test_kpis_without_running_campaigns_are_zero(fresh):
    """Нет ни одной ACTIVE/PAUSED кампании — нули и has_data=false, а не KPI
    по всем историческим кампаниям (раньше: охват — все клиенты, CTR —
    по всем рекомендациям)."""
    e = fresh.engine
    cid, uid = uuid.uuid4(), uuid.uuid4()
    await _sql(e, "INSERT INTO users (user_id, external_id, segment_id) "
                  "VALUES (:uid, 'solo', 9)", uid=uid)
    await _sql(e, """
        INSERT INTO cashback_campaigns (campaign_id, name, cashback_rate, budget_total,
               budget_spent, status, start_date, end_date)
        VALUES (:cid, 'Done', 5, 1000, 50, 'COMPLETED', now() - interval '20 days',
                now() - interval '1 day')""", cid=cid)
    rid = uuid.uuid4()
    await _sql(e, """
        INSERT INTO recommendations (recommendation_id, user_id, campaign_id, mcc_code,
               model_score, generated_at, response_status, expires_at, responded_at)
        VALUES (:rid, :uid, :cid, '5411', 0.5, now() - interval '3 days', 'ACCEPTED',
                now() + interval '4 days', now() - interval '3 days')""",
               rid=rid, uid=uid, cid=cid)
    await _sql(e, """
        INSERT INTO cashback_accruals (user_id, campaign_id, recommendation_id,
               transaction_id, mcc_code, transaction_amount, cashback_amount, accrued_at)
        VALUES (:uid, :cid, :rid, 'tx-done', '5411', 1000, 50, now() - interval '2 days')""",
               uid=uid, cid=cid, rid=rid)

    body = await _get(fresh, "/analytics/kpis")
    assert body == {
        "campaigns_count": 0, "reach": 0, "spent": "0", "budget": "0", "avg_ctr": 0.0,
        "trends": {"reach": None, "spent": None, "ctr": None},
        "trend_window_days": 30, "has_data": False,
    }
    # явный фильтр по кампании по-прежнему показывает её
    body = await _get(fresh, "/analytics/kpis", campaign_id=str(cid))
    assert (body["campaigns_count"], body["reach"], body["avg_ctr"], body["has_data"]) == (
        1, 1, 1.0, True)


# ── сегмент × MCC ─────────────────────────────────────────────────────────────
async def test_segment_matrix(api):
    rows = [(c["segment_id"], c["mcc_code"], c["impressions"], c["accepted"], c["ctr"])
            for c in await _get(api, "/analytics/segment-matrix")]
    assert rows == [
        (1, "5411", 2, 1, 0.5), (2, "5411", 1, 1, 1.0), (3, "5411", 1, 0, 0.0),
        (5, "5411", 2, 1, 0.5), (5, "5812", 1, 0, 0.0), (6, "5411", 1, 0, 0.0),
        (9, "5411", 3, 2, 0.666667), (10, "5411", 2, 1, 0.5), (10, "5812", 1, 1, 1.0)]
    premium = [(c["segment_id"], c["mcc_code"]) for c in
               await _get(api, "/analytics/segment-matrix", segment_id="premium")]
    assert premium == [(9, "5411"), (10, "5411"), (10, "5812")]


async def test_segment_matrix_of_one_campaign(api):
    """UI подписывает матрицу именем выбранной кампании — и данные её:
    раньше campaign_id не было, и под именем кампании шли офферы всех."""
    def cells(body):
        return [(c["segment_id"], c["mcc_code"], c["impressions"], c["accepted"],
                 c["ctr"]) for c in body]

    assert cells(await _get(api, "/analytics/segment-matrix",
                            campaign_id=str(api.ids["A"]))) == [
        (1, "5411", 1, 0, 0.0), (3, "5411", 1, 0, 0.0), (5, "5411", 1, 1, 1.0),
        (5, "5812", 1, 0, 0.0), (6, "5411", 1, 0, 0.0), (9, "5411", 2, 2, 1.0),
        (10, "5411", 1, 0, 0.0), (10, "5812", 1, 1, 1.0)]
    assert cells(await _get(api, "/analytics/segment-matrix",
                            campaign_id=str(api.ids["B"]))) == [
        (1, "5411", 1, 1, 1.0), (2, "5411", 1, 1, 1.0), (9, "5411", 1, 0, 0.0)]
    assert cells(await _get(api, "/analytics/segment-matrix",
                            campaign_id=str(api.ids["B"]), segment_id="premium")) == [
        (9, "5411", 1, 0, 0.0)]
    assert await _get(api, "/analytics/segment-matrix",
                      campaign_id=str(uuid.uuid4())) == []


# ── когорты ───────────────────────────────────────────────────────────────────
def _cells(body) -> list[tuple[int, int, float]]:
    return [(c["period"], c["active_users"], c["retention"]) for c in body]


async def test_cohort_retention_spans_more_than_twelve_months(api):
    """Когорта 2024-01 — 4 клиента (c5 — из 2024-02). Месяц 12 и дальше —
    свои периоды, а не 0–11 повторно (date_part('month', age)); две покупки
    c2 в 2025-01 — один активный клиент. Раньше — 500 (B9)."""
    body = await _get(api, "/analytics/cohort-retention", cohort_month="2024-01",
                      periods=18)
    assert _cells(body) == [(0, 2, 0.5), (1, 1, 0.25), (2, 1, 0.25), (12, 2, 0.5),
                            (14, 1, 0.25), (17, 1, 0.25)]
    assert {c["cohort_month"] for c in body} == {"2024-01"}
    assert _cells(await _get(api, "/analytics/cohort-retention",
                             cohort_month="2024-01", periods=24)) == _cells(body)
    assert _cells(await _get(api, "/analytics/cohort-retention",
                             cohort_month="2024-01", periods=12)) == [
        (0, 2, 0.5), (1, 1, 0.25), (2, 1, 0.25)]
    assert _cells(await _get(api, "/analytics/cohort-retention",
                             cohort_month="2024-02", periods=13)) == [
        (0, 1, 1.0), (12, 1, 1.0)]


@pytest.mark.parametrize("bad", ["2024-13", "2024-00", "24-01"])
async def test_cohort_month_must_be_a_real_month(api, bad):
    r = await api.get("/analytics/cohort-retention", params={"cohort_month": bad})
    assert r.status_code == 422


# ── топ кампаний ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("metric", "expected"), [
    # (выручка − весь кэшбэк) / весь кэшбэк
    ("roi", [("Bravo", 32.3333), ("Charlie", 28.1667), ("Alpha", 19.0), ("Delta", 0.0)]),
    # принявшие / получившие (различные клиенты): A — 5 из 7
    ("ctr", [("Charlie", 1.0), ("Alpha", 0.7143), ("Bravo", 0.6667), ("Delta", 0.0)]),
    # купившие по принятому офферу / принявшие: A — 4 из 5 (u3 не купил);
    # раньше — копия ctr по строкам (A: 7/30)
    ("conversion_rate", [("Charlie", 1.0), ("Alpha", 0.8), ("Bravo", 0.5),
                         ("Delta", 0.0)]),
    # только PAID: A — 50 + 25 + 25 + 40 (PENDING 30 + 20 + 5 — нет)
    ("cashback_paid", [("Alpha", 140.0), ("Charlie", 80.0), ("Bravo", 15.0),
                       ("Delta", 0.0)]),
])
async def test_top_campaigns(api, metric, expected):
    body = await _get(api, "/analytics/top-campaigns", metric=metric, limit=10)
    assert [(c["name"], c["metric"]) for c in body] == expected
    assert {c["campaign_id"] for c in body} == {
        str(api.ids[k]) for k in ("A", "B", "C", "D")}


async def test_top_campaigns_limit(api):
    body = await _get(api, "/analytics/top-campaigns", metric="roi", limit=2)
    assert [c["name"] for c in body] == ["Bravo", "Charlie"]


# ── статистика кампании (карточки UI) — по тем же определениям ───────────────
async def test_campaign_stats_use_the_analytics_definitions(api):
    """GET /campaigns/{id}/stats, за всё время. A — 30 офферов 7 клиентам;
    открыто 27 (EXPIRED a6/a7 и PENDING a9 — нет, SNOOZE a8 — да); принято 7
    → ctr = 7 / 30, как avg_ctr в KPI; конверсия — купившие по принятому
    офферу / принявшие (4 из 5: u3 не купил), выплачено — только PAID
    (50 + 40 + 25 + 25 из 195), ROI — по всему начисленному, как в
    top-campaigns. Раньше: «клик» = не PENDING (29, EXPIRED — «открыто»),
    ctr = 29 / 30 (open rate под именем CTR), conversion = 7 / 30,
    cashback_paid = 195, а «Охват … клиентов» в UI — число офферов."""
    body = await _get(api, f"/campaigns/{api.ids['A']}/stats")
    assert body == {
        "campaign_id": str(api.ids["A"]),
        "impressions": 30, "reach": 7, "clicks": 27, "accepted": 7,
        "transactions": 7, "revenue": "3900.00", "cashback_paid": "140.00",
        "ctr": 0.233333, "conversion_rate": 0.8, "roi": 19.0,
    }


@pytest.mark.parametrize(("key", "expected"), [
    # (офферы, клиенты, открыто, принято, начисления, выручка, выплачено,
    #  ctr, conversion_rate, roi)
    ("B", (3, 3, 3, 2, 1, "500.00", "15.00", 0.666667, 0.5, 32.3333)),
    # начисления когорт — без оффера: в покупках и деньгах, не в конверсии
    ("C", (1, 1, 1, 1, 12, "3500.00", "80.00", 1.0, 1.0, 28.1667)),
    ("D", (1, 1, 1, 0, 0, "0", "0", 0.0, 0.0, 0.0)),
])
async def test_campaign_stats_of_every_campaign(api, key, expected):
    body = await _get(api, f"/campaigns/{api.ids[key]}/stats")
    assert tuple(body[k] for k in (
        "impressions", "reach", "clicks", "accepted", "transactions", "revenue",
        "cashback_paid", "ctr", "conversion_rate", "roi")) == expected


async def test_campaign_stats_agree_with_top_campaigns(api):
    """Карточка кампании и топ кампаний — одни и те же conversion_rate,
    cashback_paid и roi (одно определение на число, ADR 0015)."""
    top = {metric: {c["campaign_id"]: c["metric"] for c in await _get(
               api, "/analytics/top-campaigns", metric=metric, limit=10)}
           for metric in ("conversion_rate", "cashback_paid", "roi")}
    for key in ("A", "B", "C", "D"):
        cid = str(api.ids[key])
        stats = await _get(api, f"/campaigns/{cid}/stats")
        assert (round(stats["conversion_rate"], 4), float(stats["cashback_paid"]),
                stats["roi"]) == (top["conversion_rate"][cid], top["cashback_paid"][cid],
                                  top["roi"][cid]), key


# ── дневной тренд ─────────────────────────────────────────────────────────────
def _points(body) -> list[tuple[str, str, int]]:
    return [(p["date"], p["segment_bucket"], p["accepted"]) for p in body["points"]]


async def test_daily_trend(api):
    """Раньше — 500 при любом запросе (B9: ``:campaign_id::uuid``)."""
    assert _points(await _get(api, "/analytics/daily-trend")) == [
        (day(6), "mass", 1), (day(5), "business", 1), (day(4), "premium", 1),
        (day(3), "premium", 1), (day(3), "senior", 1), (day(2), "premium", 2)]
    body = await _get(api, "/analytics/daily-trend", campaign_id=str(api.ids["A"]))
    assert body["campaign_id"] == str(api.ids["A"]) and body["period_days"] == 30
    assert _points(body) == [(day(6), "mass", 1), (day(4), "premium", 1),
                             (day(3), "premium", 1), (day(2), "premium", 1)]
    assert _points(await _get(api, "/analytics/daily-trend", segment_id="premium")) == [
        (day(4), "premium", 1), (day(3), "premium", 1), (day(2), "premium", 2)]


# ── каналы ────────────────────────────────────────────────────────────────────
def _channels(body) -> list[tuple]:
    return [(c["channel"], c["sent"], c["opened"], c["accepted"], c["converted"],
             c["pending"]) for c in body]


async def test_channels(api):
    """sent — все доставленные офферы канала (канал не записан → UNKNOWN):
    у PUSH 8 доставок, ответов 5 — open rate 62.5%, а не 100% по
    построению; EXPIRED и PENDING не «открыты»; converted — принятые с
    начислением. Раньше — 500 (B9)."""
    assert _channels(await _get(api, "/analytics/channels")) == [
        ("PUSH", 8, 5, 3, 3, False), ("EMAIL", 2, 2, 1, 0, False),
        ("SMS", 2, 2, 1, 1, False), ("IN_APP", 1, 1, 1, 1, False),
        ("UNKNOWN", 1, 1, 1, 0, False)]
    assert _channels(await _get(api, "/analytics/channels",
                                campaign_id=str(api.ids["A"]))) == [
        ("PUSH", 5, 2, 2, 2, False), ("EMAIL", 1, 1, 0, 0, False),
        ("IN_APP", 1, 1, 1, 1, False), ("SMS", 1, 1, 0, 0, False),
        ("UNKNOWN", 1, 1, 1, 0, False)]
    assert _channels(await _get(api, "/analytics/channels", segment_id="premium")) == [
        ("PUSH", 4, 4, 3, 3, False), ("IN_APP", 1, 1, 1, 1, False),
        ("SMS", 1, 1, 0, 0, False)]


async def test_every_analytics_endpoint_answers_on_an_empty_database(fresh):
    for path, params in [
        ("/analytics/funnel", {}), ("/analytics/kpis", {}),
        ("/analytics/segment-matrix", {}), ("/analytics/top-campaigns", {}),
        ("/analytics/cohort-retention", {"cohort_month": date.today().strftime("%Y-%m"),
                                         "periods": 18}),
        ("/analytics/daily-trend", {"campaign_id": str(uuid.uuid4()),
                                    "segment_id": "mass"}),
        ("/analytics/channels", {"campaign_id": str(uuid.uuid4()), "segment_id": "mass"}),
    ]:
        await _get(fresh, path, **params)


# ── settlement: PENDING → PAID после холда ───────────────────────────────────
async def test_settlement_pays_pending_accruals_after_the_delay(fresh):
    from app.scheduling import CampaignScheduler, run_job
    from prometheus_client import REGISTRY

    e = fresh.engine
    cid = uuid.uuid4()
    await _sql(e, """
        INSERT INTO cashback_campaigns (campaign_id, name, cashback_rate, budget_total,
               budget_spent, status, start_date, end_date)
        VALUES (:cid, 'Settle', 5, 1000, 175, 'ACTIVE', now() - interval '40 days',
                now() + interval '10 days')""", cid=cid)
    paid_earlier = datetime.now(UTC) - timedelta(days=29)
    for tx, cashback, status, ago_hours, paid_at in [
            ("due", "50", "PENDING", 25, None),            # холд прошёл
            ("fresh", "75", "PENDING", 23, None),          # ещё на холде
            ("done", "50", "PAID", 24 * 30, paid_earlier)]:
        uid, rid = uuid.uuid4(), uuid.uuid4()               # клиент на начисление
        await _sql(e, "INSERT INTO users (user_id, external_id, segment_id) "
                      "VALUES (:uid, :ext, 5)", uid=uid, ext=f"payee-{tx}")
        await _sql(e, """
            INSERT INTO recommendations (recommendation_id, user_id, campaign_id,
                   mcc_code, model_score, generated_at, response_status, expires_at,
                   responded_at)
            VALUES (:rid, :uid, :cid, '5411', 0.5, now() - make_interval(hours => :h + 1),
                    'ACCEPTED', now() + interval '1 day', now() - make_interval(hours => :h + 1))
            """, rid=rid, uid=uid, cid=cid, h=ago_hours)
        await _sql(e, """
            INSERT INTO cashback_accruals (user_id, campaign_id, recommendation_id,
                   transaction_id, mcc_code, transaction_amount, cashback_amount, status,
                   accrued_at, paid_at)
            VALUES (:uid, :cid, :rid, :tx, '5411', 1000, :cb, CAST(:st AS accrual_status),
                    now() - make_interval(hours => :h), :paid_at)""",
                   uid=uid, cid=cid, rid=rid, tx=tx, cb=Decimal(cashback), st=status,
                   h=ago_hours, paid_at=paid_at)

    def _failures():
        return REGISTRY.get_sample_value("scheduler_job_failures_total",
                                         {"job": "settle-accruals"}) or 0

    async def _ledger():
        return await _sql(e, """
            SELECT transaction_id, status::text, paid_at FROM cashback_accruals
             ORDER BY transaction_id""")

    async def _funnel():
        body = await _get(fresh, "/analytics/funnel", campaign_id=str(cid), period=60)
        return {s["name"]: s["count"] for s in body["steps"]}

    assert (await _funnel())["transacted"] == 3
    assert (await _funnel())["cashback_paid"] == 1                 # только done

    before, failures = datetime.now(UTC), _failures()
    jobs = {n: j for n, j, _ in CampaignScheduler(e, settlement_delay_hours=24).jobs()}
    await run_job("settle-accruals", jobs["settle-accruals"])
    after = datetime.now(UTC)
    assert _failures() == failures

    (done, due, fresh_row) = await _ledger()
    assert done == ("done", "PAID", paid_earlier)                  # не тронута
    assert due[:2] == ("due", "PAID") and before <= due[2] <= after
    assert fresh_row == ("fresh", "PENDING", None)
    # деньги не меняются: сумма журнала = budget_spent (инвариант этапа C)
    assert await _sql(e, """
        SELECT (SELECT sum(cashback_amount) FROM cashback_accruals),
               (SELECT budget_spent FROM cashback_campaigns WHERE campaign_id = :cid)
        """, cid=cid) == [(Decimal("175.00"), Decimal("175.00"))]
    assert (await _funnel())["cashback_paid"] == 2                 # done + due

    # повтор ничего не меняет; холд 0 часов — платит и свежие
    await run_job("settle-accruals", jobs["settle-accruals"])
    assert [r[:2] for r in await _ledger()] == [
        ("done", "PAID"), ("due", "PAID"), ("fresh", "PENDING")]
    zero = {n: j for n, j, _ in CampaignScheduler(e, settlement_delay_hours=0).jobs()}
    await run_job("settle-accruals", zero["settle-accruals"])
    assert [r[1] for r in await _ledger()] == ["PAID", "PAID", "PAID"]


# ── A/B ───────────────────────────────────────────────────────────────────────
def _experiment(name: str, *variants: dict) -> dict:
    return {"name": name, "target_metric": "acceptance_rate",
            "start_date": datetime.now(UTC).isoformat(), "variants": list(variants)}


def _variant(name: str, **extra) -> dict:
    return {"name": name, "traffic_weight": 0.5, "strategy_class": "Ranker", **extra}


async def _create(client, payload) -> dict:
    r = await client.post("/experiments", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _assignments(engine, variant_id: str, n: int, converted: int, *,
                       duplicate: int = 0) -> None:
    """``n`` назначений варианта; первые ``converted`` — с конверсией, из них
    ``duplicate`` — с повторной CONVERSION; у всех — IMPRESSION."""
    await _sql(engine, """
        WITH a AS (
            INSERT INTO ab_assignments (user_id, experiment_id, variant_id)
            SELECT gen_random_uuid(), v.experiment_id, v.variant_id
              FROM ab_variants v, generate_series(1, :n)
             WHERE v.variant_id = :vid
            RETURNING assignment_id
        ), numbered AS (
            SELECT assignment_id, row_number() OVER (ORDER BY assignment_id) AS i FROM a
        )
        INSERT INTO ab_events (assignment_id, event_type, event_at)
        SELECT assignment_id, 'IMPRESSION'::ab_event_type, now() FROM numbered
        UNION ALL
        SELECT assignment_id, 'CONVERSION'::ab_event_type, now() FROM numbered
         WHERE i <= :conv
        UNION ALL
        SELECT assignment_id, 'CONVERSION'::ab_event_type, now() + interval '1 hour'
          FROM numbered
         WHERE i <= :dup""", n=n, vid=uuid.UUID(variant_id), conv=converted, dup=duplicate)


async def test_results_use_the_explicit_control_and_flag_a_worse_treatment(api):
    """Контроль — первый вариант запроса (baseline), хотя по алфавиту первым
    идёт тест (alpha-test) — раньше контролем считался он, знак эффекта
    переворачивался, и худший вариант выглядел значимо лучшим."""
    exp = await _create(api, _experiment(f"worse-{uuid.uuid4().hex[:6]}",
                                         _variant("baseline"), _variant("alpha-test")))
    assert [(v["name"], v["is_control"]) for v in exp["variants"]] == [
        ("baseline", True), ("alpha-test", False)]
    by_name = {v["name"]: v["variant_id"] for v in exp["variants"]}
    await _assignments(api.engine, by_name["baseline"], 1000, 120)
    await _assignments(api.engine, by_name["alpha-test"], 1000, 80)

    body = await _get(api, f"/experiments/{exp['experiment_id']}/results")
    assert body["control"] == {"variant_id": by_name["baseline"], "name": "baseline",
                               "n": 1000, "successes": 120, "rate": 0.12}
    assert body["treatment"] == {"variant_id": by_name["alpha-test"],
                                 "name": "alpha-test", "n": 1000, "successes": 80,
                                 "rate": 0.08}
    assert body["diff"] == -0.04
    assert body["z"] == pytest.approx(-2.981424, abs=1e-6)
    assert body["p_value"] == pytest.approx(0.0028691, abs=1e-7)
    assert body["confidence_interval"] == pytest.approx([-0.066237, -0.013763], abs=1e-6)
    assert (body["significance"], body["winner"]) == ("significant", "control")

    listed = next(e for e in await _get(api, "/experiments")
                  if e["experiment_id"] == exp["experiment_id"])
    assert [v["is_control"] for v in listed["variants"]] == [True, False]


async def test_results_without_data_have_no_verdict(api):
    exp = await _create(api, _experiment(f"empty-{uuid.uuid4().hex[:6]}",
                                         _variant("a"), _variant("b", is_control=True)))
    body = await _get(api, f"/experiments/{exp['experiment_id']}/results")
    assert body["control"]["name"] == "b" and body["treatment"]["name"] == "a"
    assert (body["z"], body["p_value"], body["significance"], body["winner"]) == (
        None, None, "no_data", None)


async def test_conversion_counts_once_per_assignment(api):
    """Миграция 010: вторая CONVERSION того же назначения — отказ БД."""
    from sqlalchemy.exc import IntegrityError

    exp = await _create(api, _experiment(f"dup-{uuid.uuid4().hex[:6]}",
                                         _variant("c"), _variant("t")))
    vid = exp["variants"][0]["variant_id"]
    await _assignments(api.engine, vid, 1, 1)
    with pytest.raises(IntegrityError, match="uq_ab_events_conversion"):
        await _sql(api.engine, """
            INSERT INTO ab_events (assignment_id, event_type)
            SELECT assignment_id, 'CONVERSION' FROM ab_assignments
             WHERE variant_id = :vid""", vid=uuid.UUID(vid))


async def test_results_count_converted_assignments_not_events(fresh):
    """Дубли CONVERSION, записанные в обход 010 (индекс снят): успех —
    назначение с конверсией. Раньше 30 событий на 20 назначений давали долю
    1.5 → sqrt отрицательного → 500."""
    await _sql(fresh.engine, "DROP INDEX uq_ab_events_conversion")
    exp = await _create(fresh, _experiment("legacy-dups", _variant("control"),
                                           _variant("test")))
    ids = {v["name"]: v["variant_id"] for v in exp["variants"]}
    await _assignments(fresh.engine, ids["control"], 20, 20, duplicate=10)
    await _assignments(fresh.engine, ids["test"], 20, 5, duplicate=5)

    body = await _get(fresh, f"/experiments/{exp['experiment_id']}/results")
    assert (body["control"]["successes"], body["control"]["rate"]) == (20, 1.0)
    assert (body["treatment"]["successes"], body["treatment"]["rate"]) == (5, 0.25)
    assert body["z"] == pytest.approx(-4.898979, abs=1e-6)
    assert 0.0 <= body["p_value"] <= 1.0
    # n = 40 < AB_MIN_OBSERVATIONS (200) → вердикта и победителя нет
    assert (body["significance"], body["winner"]) == ("no_data", None)


async def test_results_without_a_control_variant_are_409(api):
    """Варианты, вставленные мимо API без is_control (так писал сидер), —
    явная ошибка, а не «контроль по алфавиту»."""
    exp_id = uuid.uuid4()
    await _sql(api.engine, """
        INSERT INTO ab_experiments (experiment_id, name, status, target_metric, start_date)
        VALUES (:eid, :name, 'ACTIVE', 'acceptance_rate', now())""",
               eid=exp_id, name=f"raw-{exp_id.hex[:6]}")
    await _sql(api.engine, """
        INSERT INTO ab_variants (experiment_id, name, traffic_weight, strategy_class)
        VALUES (:eid, 'x', 0.5, 'S'), (:eid, 'y', 0.5, 'S')""", eid=exp_id)
    r = await api.get(f"/experiments/{exp_id}/results")
    assert r.status_code == 409
    assert r.json()["detail"] == "experiment has no control variant"


async def test_create_rejects_duplicate_names_and_bad_controls(api):
    name = f"unique-{uuid.uuid4().hex[:6]}"
    await _create(api, _experiment(name, _variant("a"), _variant("b")))
    r = await api.post("/experiments", json=_experiment(name, _variant("a"), _variant("b")))
    assert (r.status_code, r.json()) == (409, {"detail": "experiment name already exists"})

    for variants in ([_variant("a", is_control=True), _variant("b", is_control=True)],
                     [_variant("a", is_control=False), _variant("b", is_control=False)],
                     [_variant("a"), _variant("a")]):
        r = await api.post("/experiments",
                           json=_experiment(f"bad-{uuid.uuid4().hex[:6]}", *variants))
        assert r.status_code == 422, r.text


async def test_assign_only_into_an_active_experiment(api):
    exp = await _create(api, _experiment(f"assign-{uuid.uuid4().hex[:6]}",
                                         _variant("a"), _variant("b")))
    eid, user = exp["experiment_id"], uuid.uuid4()
    r = await api.post(f"/experiments/{eid}/assign/{user}")
    assert r.status_code == 409 and "not active" in r.json()["detail"]      # DRAFT

    assert (await api.patch(f"/experiments/{eid}/status",
                            params={"action": "start"})).status_code == 200
    first = await api.post(f"/experiments/{eid}/assign/{user}")
    assert first.status_code == 200, first.text
    again = await api.post(f"/experiments/{eid}/assign/{user}")
    assert again.json() == first.json()                                     # идемпотентно

    assert (await api.patch(f"/experiments/{eid}/status",
                            params={"action": "stop"})).status_code == 200
    r = await api.post(f"/experiments/{eid}/assign/{uuid.uuid4()}")
    assert r.status_code == 409                                             # STOPPED
    r = await api.post(f"/experiments/{uuid.uuid4()}/assign/{user}")
    assert r.status_code == 404


async def test_concurrent_assign_returns_one_assignment(api):
    """Гонка назначений одного клиента: все запросы прочитали «назначения
    нет» до первого INSERT. Раньше второй INSERT ловил UNIQUE (user_id,
    experiment_id) → 500. SHARE-блокировка таблицы пропускает чтение и
    держит INSERT — так все 8 запросов гарантированно сходятся на нём."""
    import psycopg2

    exp = await _create(api, _experiment(f"race-{uuid.uuid4().hex[:6]}",
                                         _variant("a"), _variant("b")))
    eid = exp["experiment_id"]
    await api.patch(f"/experiments/{eid}/status", params={"action": "start"})
    user = uuid.uuid4()
    blocker = psycopg2.connect(api.dsn)
    try:
        with blocker.cursor() as cur:
            cur.execute("LOCK TABLE ab_assignments IN SHARE MODE")
        tasks = [asyncio.create_task(api.post(f"/experiments/{eid}/assign/{user}"))
                 for _ in range(8)]
        for _ in range(400):
            ((queued,),) = await _sql(api.engine, """
                SELECT count(*) FROM pg_stat_activity
                 WHERE datname = current_database() AND wait_event_type = 'Lock'
                   AND query LIKE 'INSERT INTO ab_assignments%'""")
            if queued == 8 or any(t.done() for t in tasks):
                break
            await asyncio.sleep(0.05)
        assert queued == 8, "assign requests never met at the INSERT"
    finally:
        blocker.rollback()
        blocker.close()
    replies = await asyncio.gather(*tasks)
    assert [r.status_code for r in replies] == [200] * 8
    assert len({(r.json()["variant_id"], r.json()["assigned_at"]) for r in replies}) == 1
    assert await _sql(api.engine, "SELECT count(*) FROM ab_assignments "
                                  "WHERE experiment_id = :eid AND user_id = :uid",
                      eid=uuid.UUID(eid), uid=user) == [(1,)]
