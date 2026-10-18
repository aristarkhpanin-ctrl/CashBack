"""009: upgrade → downgrade → upgrade на пустой БД и на БД с данными.

Downgrade обязан вернуть схему 008 в точности (сравнение снимков каталога),
данные — сохраниться (теряются только новые колонки 009).
"""
from __future__ import annotations

import subprocess
import sys
import time
from decimal import Decimal

import psycopg2
import pytest
from dbm_support import MIGRATIONS_DIR, alembic_env, run_alembic

pytestmark = pytest.mark.integration

# Снимок схемы public: всё, что меняет 009, и всё, что могло задеть.
_SNAPSHOT_SQL = {
    "columns": """
        SELECT table_name, column_name, data_type, udt_name, is_nullable,
               column_default, character_maximum_length, numeric_precision,
               numeric_scale
          FROM information_schema.columns
         WHERE table_schema = 'public'
    """,
    "constraints": """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
          FROM pg_constraint
         WHERE connamespace = 'public'::regnamespace
    """,
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes "
               "WHERE schemaname = 'public'",
    "functions": """
        SELECT p.proname, pg_get_function_identity_arguments(p.oid),
               pg_get_function_result(p.oid), p.provolatile::text, l.lanname,
               p.prosrc
          FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang
         WHERE p.pronamespace = 'public'::regnamespace
    """,
    "triggers": "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) "
                "FROM pg_trigger WHERE NOT tgisinternal",
    "enums": """
        SELECT t.typname, string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
          FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
         GROUP BY t.typname
    """,
}


def _query(dsn: str, sql: str, *params):
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params or None)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _snapshot(dsn: str) -> dict[str, list[tuple]]:
    return {name: sorted(_query(dsn, sql), key=repr) for name, sql in _SNAPSHOT_SQL.items()}


def _revision(dsn: str) -> str:
    return _query(dsn, "SELECT version_num FROM alembic_version")[0][0]


def _assert_same(actual: dict, expected: dict) -> None:
    for part in expected:
        missing = sorted(set(expected[part]) - set(actual[part]), key=repr)
        extra = sorted(set(actual[part]) - set(expected[part]), key=repr)
        assert (missing, extra) == ([], []), f"{part}: missing={missing} extra={extra}"


NEW_IN_009 = {
    ("users", "rfm_score"), ("cashback_campaigns", "archived_at"),
    ("cashback_accruals", "recommendation_id"),
}


# ── пустая БД ────────────────────────────────────────────────────────────────
def test_cycle_on_empty_database_restores_both_schemas(fresh_dsn):
    run_alembic(fresh_dsn, "upgrade", "008")
    at_008 = _snapshot(fresh_dsn)
    run_alembic(fresh_dsn, "upgrade", "head")
    assert _revision(fresh_dsn) == "009"
    at_head = _snapshot(fresh_dsn)
    columns = {(t, c) for t, c, *_ in at_head["columns"]}
    assert NEW_IN_009 <= columns
    assert NEW_IN_009.isdisjoint({(t, c) for t, c, *_ in at_008["columns"]})

    run_alembic(fresh_dsn, "downgrade", "008")
    _assert_same(_snapshot(fresh_dsn), at_008)
    run_alembic(fresh_dsn, "upgrade", "head")
    _assert_same(_snapshot(fresh_dsn), at_head)

    run_alembic(fresh_dsn, "downgrade", "base")
    assert _query(fresh_dsn, "SELECT table_name FROM information_schema.tables "
                             "WHERE table_schema = 'public'") == [("alembic_version",)]
    run_alembic(fresh_dsn, "upgrade", "head")
    _assert_same(_snapshot(fresh_dsn), at_head)


# ── БД с данными (как после сидера на 008) ───────────────────────────────────
_SEED_008 = """
INSERT INTO users (user_id, external_id, segment_id)
SELECT ('00000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
       'demo_' || lpad(i::text, 8, '0'), 1 + i % 10
  FROM generate_series(1, 40) AS i;

INSERT INTO user_consents (user_id, consent_type, status, document_version, created_at)
SELECT user_id, 'personalised_cashback',
       (CASE WHEN external_id < 'demo_00000035' THEN 'GRANTED' ELSE 'REVOKED' END)::consent_status,
       'v1.0', now() - interval '20 days'
  FROM users;

INSERT INTO cashback_campaigns (campaign_id, name, target_segment_ids, cashback_rate,
        min_transaction_amount, budget_total, budget_spent, status, start_date,
        end_date, allowed_channels, rate_tiers, daily_limit, rfm_min, rfm_max)
VALUES
 ('10000000-0000-0000-0000-000000000001', 'Кэшбэк на продукты', '{}', 5.00, 300,
  100000, 0, 'ACTIVE', now() - interval '30 days', now() + interval '60 days',
  ARRAY['ONLINE', 'POS'], NULL, 5000, NULL, NULL),
 ('10000000-0000-0000-0000-000000000002', 'Рестораны', '{1,2,3,4,5}', 7.00, 500,
  50000, 0, 'PAUSED', now() - interval '30 days', now() + interval '60 days',
  ARRAY['ONLINE'], NULL, NULL, 2, 5),
 ('10000000-0000-0000-0000-000000000003', 'АЗС на лето', '{}', 3.00, NULL,
  80000, 0, 'COMPLETED', now() - interval '90 days', now() - interval '1 day',
  ARRAY['POS'], '[{"min_amount": 0, "max_amount": 5000, "rate": 1.0},
                  {"min_amount": 5000, "max_amount": null, "rate": 3.0}]', NULL,
  NULL, NULL);

INSERT INTO campaign_categories (campaign_id, mcc_code, min_transaction_amount)
VALUES ('10000000-0000-0000-0000-000000000001', '5411', 500),
       ('10000000-0000-0000-0000-000000000001', '5499', NULL),
       ('10000000-0000-0000-0000-000000000002', '5812', 500),
       ('10000000-0000-0000-0000-000000000003', '5541', NULL);

INSERT INTO recommendations (recommendation_id, user_id, campaign_id, mcc_code,
        model_score, generated_at, response_status, expires_at, responded_at,
        channel, model_version)
SELECT ('20000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
       ('00000000-0000-0000-0000-' || lpad((1 + i % 40)::text, 12, '0'))::uuid,
       c.campaign_id, c.mcc, 0.5 + (i % 5) / 10.0, now() - interval '5 days',
       (ARRAY['ACCEPTED', 'DECLINED', 'PENDING', 'EXPIRED', 'SNOOZE'])[1 + i % 5]
           ::recommendation_response_status,
       now() + interval '2 days', now() - interval '4 days', 'PUSH', 'v3'
  FROM generate_series(0, 59) AS i
 CROSS JOIN LATERAL (
       SELECT (ARRAY['10000000-0000-0000-0000-000000000001',
                     '10000000-0000-0000-0000-000000000002',
                     '10000000-0000-0000-0000-000000000003'])[1 + i % 3]::uuid AS campaign_id,
              (ARRAY['5411', '5812', '5541'])[1 + i % 3] AS mcc) c;

-- начисления — по принятым офферам, как сидер: без recommendation_id
INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id, mcc_code,
        transaction_amount, cashback_amount, status, accrued_at)
SELECT r.user_id, r.campaign_id, 'seed-' || r.recommendation_id, r.mcc_code, 1500,
       calculate_cashback(r.campaign_id, 1500),
       (CASE WHEN r.model_score > 0.6 THEN 'PAID' ELSE 'PENDING' END)::accrual_status,
       now() - interval '3 days'
  FROM recommendations r
 WHERE r.response_status = 'ACCEPTED';

UPDATE cashback_campaigns c
   SET budget_spent = s.total
  FROM (SELECT campaign_id, sum(cashback_amount) AS total
          FROM cashback_accruals GROUP BY campaign_id) s
 WHERE s.campaign_id = c.campaign_id;
"""

_TABLES = ("users", "user_consents", "cashback_campaigns", "campaign_categories",
           "recommendations", "cashback_accruals", "admin_users", "ml_limits",
           "role_permissions")


def _columns_at(dsn: str) -> dict[str, list[str]]:
    rows = _query(dsn, "SELECT table_name, column_name FROM information_schema.columns "
                       "WHERE table_schema = 'public' AND table_name = ANY(%s) "
                       "ORDER BY table_name, ordinal_position", list(_TABLES))
    out: dict[str, list[str]] = {}
    for table, column in rows:
        out.setdefault(table, []).append(column)
    return out


def _fingerprint(dsn: str, columns: dict[str, list[str]]) -> dict[str, tuple]:
    """Число строк и md5 содержимого по заданным колонкам каждой таблицы."""
    out = {}
    for table, cols in columns.items():
        select = ", ".join(f'"{c}"' for c in cols)
        out[table] = _query(dsn, f"""
            SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), ''))
              FROM (SELECT {select} FROM {table}) t""")[0]
    return out


def _seeded_008(dsn: str) -> tuple[dict, dict]:
    run_alembic(dsn, "upgrade", "008")
    _query(dsn, _SEED_008)
    columns = _columns_at(dsn)
    return columns, _fingerprint(dsn, columns)


def _old_cashback(dsn: str) -> list[tuple]:
    return _query(dsn, "SELECT campaign_id::text, calculate_cashback(campaign_id, 1500), "
                       "calculate_cashback(campaign_id, 400) FROM cashback_campaigns "
                       "ORDER BY 1")


def test_upgrade_keeps_seeded_data_and_applies_new_rules(fresh_dsn):
    columns, before = _seeded_008(fresh_dsn)
    assert before["cashback_accruals"][0] == 12            # 60 офферов, каждый 5-й принят

    run_alembic(fresh_dsn, "upgrade", "head")

    assert _fingerprint(fresh_dsn, columns) == before
    # каждое старое начисление привязано к офферу, который оно оплатило
    # (сидер: transaction_id = 'seed-' || оффер)
    assert _query(fresh_dsn, """
        SELECT (SELECT count(*) FROM cashback_accruals
                 WHERE transaction_id = 'seed-' || recommendation_id),
               (SELECT count(*) FROM users WHERE rfm_score IS NULL),
               (SELECT count(*) FROM cashback_campaigns WHERE archived_at IS NULL)
    """) == [(12, 40, 3)]

    # eligibility на сидированных данных: продукты — всем с согласием,
    # минимум категории 500 сильнее минимума кампании 300
    user = "00000000-0000-0000-0000-000000000007"
    revoked = "00000000-0000-0000-0000-000000000038"
    eligible = """SELECT campaign_id::text, min_transaction_amount
                    FROM eligible_campaigns(%s, %s, %s, now())"""
    assert _query(fresh_dsn, eligible, user, "5411", None) == [
        ("10000000-0000-0000-0000-000000000001", Decimal("500.00"))]
    assert _query(fresh_dsn, eligible, user, "5411", 400) == []
    assert _query(fresh_dsn, eligible, revoked, "5411", None) == []
    assert _query(fresh_dsn, eligible, user, "5812", None) == []          # PAUSED
    assert _query(fresh_dsn, eligible, user, "5541", None) == []          # COMPLETED
    assert _query(fresh_dsn, "SELECT calculate_cashback(%s, '5411', 400), "
                             "calculate_cashback(%s, '5499', 400)",
                  "10000000-0000-0000-0000-000000000001",
                  "10000000-0000-0000-0000-000000000001") == [(0, Decimal("20.00"))]

    # оффер, оплаченный до 009, второй раз не платит (B45: ключ, переживший
    # DEL старого listener'а, иначе оплатил бы его снова)
    (rec_id, rec_user), = _query(fresh_dsn, """
        SELECT recommendation_id::text, user_id::text FROM recommendations
         WHERE response_status = 'ACCEPTED' ORDER BY recommendation_id LIMIT 1""")
    insert = """INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id,
                    mcc_code, transaction_amount, cashback_amount, recommendation_id)
                VALUES (%s, '10000000-0000-0000-0000-000000000001', %s, '5411', 1500,
                        75, %s)"""
    with pytest.raises(psycopg2.errors.UniqueViolation) as paid_before:
        _query(fresh_dsn, insert, rec_user, "live-0", rec_id)
    assert paid_before.value.diag.constraint_name == "uq_accrual_recommendation"
    # новый принятый оффер оплачивается ровно один раз
    (new_rec,), = _query(fresh_dsn, """
        INSERT INTO recommendations (user_id, campaign_id, mcc_code, model_score,
                                     response_status, expires_at, responded_at)
        VALUES (%s, '10000000-0000-0000-0000-000000000001', '5411', 0.9, 'ACCEPTED',
                now() + interval '1 day', now())
        RETURNING recommendation_id::text""", rec_user)
    _query(fresh_dsn, insert, rec_user, "live-1", new_rec)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _query(fresh_dsn, insert, rec_user, "live-2", new_rec)

    # кампания с журналом не удаляется; COMPLETED не «оживает»
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _query(fresh_dsn, "DELETE FROM cashback_campaigns "
                          "WHERE campaign_id = '10000000-0000-0000-0000-000000000003'")
    with pytest.raises(psycopg2.errors.CheckViolation):
        _query(fresh_dsn, "UPDATE cashback_campaigns SET status = 'ACTIVE' "
                          "WHERE campaign_id = '10000000-0000-0000-0000-000000000003'")


def test_downgrade_with_new_data_restores_008_and_upgrades_again(fresh_dsn):
    columns, _ = _seeded_008(fresh_dsn)
    old_cashback = _old_cashback(fresh_dsn)
    run_alembic(fresh_dsn, "upgrade", "head")

    # данные, которые пишет новый код: новые принятые офферы и их начисления
    _query(fresh_dsn, """
        UPDATE users SET rfm_score = 1 + (segment_id % 5);
        UPDATE cashback_campaigns SET archived_at = now()
         WHERE campaign_id = '10000000-0000-0000-0000-000000000003';
        INSERT INTO recommendations (recommendation_id, user_id, campaign_id, mcc_code,
               model_score, response_status, expires_at, responded_at)
        SELECT ('30000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
               ('00000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
               '10000000-0000-0000-0000-000000000001', '5411', 0.9, 'ACCEPTED',
               now() + interval '2 days', now() - interval '1 hour'
          FROM generate_series(1, 4) AS i;
        INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id, mcc_code,
               transaction_amount, cashback_amount, recommendation_id)
        SELECT user_id, campaign_id, 'live-' || recommendation_id, mcc_code, 1500, 75,
               recommendation_id
          FROM recommendations
         WHERE recommendation_id::text LIKE '30000000-%';
    """)
    written = _fingerprint(fresh_dsn, columns)
    assert written["cashback_accruals"][0] == 16
    links = "SELECT transaction_id, recommendation_id::text FROM cashback_accruals"
    linked = sorted(_query(fresh_dsn, links))
    assert all(rec is not None for _, rec in linked)

    run_alembic(fresh_dsn, "downgrade", "008")

    assert _revision(fresh_dsn) == "008"
    assert _fingerprint(fresh_dsn, columns) == written          # строки на месте
    assert not NEW_IN_009 & {
        (t, c) for t, cols in _columns_at(fresh_dsn).items() for c in cols}
    assert _old_cashback(fresh_dsn) == old_cashback             # calculate_cashback из 001
    assert _query(fresh_dsn, "SELECT count(*) FROM pg_proc WHERE proname IN "
                             "('eligible_campaigns', 'campaign_status_guard')") == [(0,)]
    assert _query(fresh_dsn, "SELECT count(*) FROM pg_trigger "
                             "WHERE tgname = 'trg_campaign_status_guard'") == [(0,)]
    assert _query(fresh_dsn, """
        SELECT conname, confdeltype::text FROM pg_constraint
         WHERE conname IN ('cashback_accruals_campaign_id_fkey',
                           'recommendations_campaign_id_fkey') ORDER BY 1""") == [
        ("cashback_accruals_campaign_id_fkey", "c"),
        ("recommendations_campaign_id_fkey", "c")]

    run_alembic(fresh_dsn, "upgrade", "head")
    assert _fingerprint(fresh_dsn, columns) == written
    # downgrade стёр ссылки на офферы — повторный upgrade восстановил те же
    assert sorted(_query(fresh_dsn, links)) == linked


# ── старые начисления → офферы, которые они оплатили (B45) ───────────────────
_LEGACY_008 = """
INSERT INTO users (user_id, external_id, segment_id)
SELECT ('00000000-0000-0000-0000-00000000000' || i)::uuid, 'legacy_' || i, 3
  FROM generate_series(1, 4) AS i;

INSERT INTO cashback_campaigns (campaign_id, name, cashback_rate, budget_total,
        status, start_date, end_date)
VALUES ('10000000-0000-0000-0000-000000000001', 'Продукты', 5, 100000, 'ACTIVE',
        now() - interval '30 days', now() + interval '30 days'),
       ('10000000-0000-0000-0000-000000000002', 'Ещё продукты', 5, 100000, 'ACTIVE',
        now() - interval '30 days', now() + interval '30 days');

-- офферы: (id, клиент, кампания, MCC, статус, принят N дней назад)
INSERT INTO recommendations (recommendation_id, user_id, campaign_id, mcc_code,
        model_score, response_status, expires_at, responded_at)
SELECT ('20000000-0000-0000-0000-0000000000' || r.id)::uuid,
       ('00000000-0000-0000-0000-00000000000' || r.u)::uuid,
       ('10000000-0000-0000-0000-00000000000' || r.c)::uuid, r.mcc, 0.9,
       r.st::recommendation_response_status, now() + interval '7 days',
       now() - make_interval(days => r.ago)
  FROM (VALUES ('a1', 1, 1, '5411', 'ACCEPTED', 10),
               ('a2', 1, 1, '5411', 'ACCEPTED', 5),
               ('b1', 2, 1, '5411', 'DECLINED', 9),
               ('b2', 2, 1, '5411', 'ACCEPTED', 1),
               ('d1', 3, 1, '5411', 'ACCEPTED', 6),
               ('d2', 3, 2, '5411', 'ACCEPTED', 6),
               ('e1', 4, 1, '5812', 'ACCEPTED', 6)) AS r(id, u, c, mcc, st, ago);

-- начисления старого listener'а: (покупка, клиент, кампания, MCC, N дней назад)
INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id, mcc_code,
        transaction_amount, cashback_amount, accrued_at)
SELECT ('00000000-0000-0000-0000-00000000000' || a.u)::uuid,
       ('10000000-0000-0000-0000-00000000000' || a.c)::uuid, a.tx, a.mcc, 1500, 75,
       now() - make_interval(days => a.ago)
  FROM (VALUES ('x1', 1, 1, '5411', 7),     -- после a1, до a2       → a1
               ('x2', 1, 1, '5411', 4),     -- после a2              → a2
               ('x3', 1, 1, '5411', 3),     -- a2 уже оплачен x2 (B45) → нет
               ('y1', 2, 1, '5411', 2),     -- b1 отклонён, b2 принят позже → нет
               ('z1', 3, 1, '5411', 5),     -- покупка z1 по d1      → d1
               ('z1', 3, 2, '5411', 4),     -- та же покупка ещё раз → нет
               ('w1', 4, 1, '5411', 5))     -- оффер e1 — на другой MCC → нет
       AS a(tx, u, c, mcc, ago);
"""


def test_upgrade_links_legacy_accruals_to_the_offers_they_paid(fresh_dsn):
    run_alembic(fresh_dsn, "upgrade", "008")
    _query(fresh_dsn, _LEGACY_008)

    run_alembic(fresh_dsn, "upgrade", "head")

    rows = _query(fresh_dsn, """
        SELECT a.transaction_id, right(c.campaign_id::text, 1),
               right(a.recommendation_id::text, 2)
          FROM cashback_accruals a JOIN cashback_campaigns c USING (campaign_id)
         ORDER BY 1, 2""")
    assert rows == [("w1", "1", None), ("x1", "1", "a1"), ("x2", "1", "a2"),
                    ("x3", "1", None), ("y1", "1", None), ("z1", "1", "d1"),
                    ("z1", "2", None)]
    # оплаченный до 009 оффер новым начислением не оплатить
    with pytest.raises(psycopg2.errors.UniqueViolation) as exc:
        _query(fresh_dsn, """
            INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id,
                   mcc_code, transaction_amount, cashback_amount, recommendation_id)
            VALUES ('00000000-0000-0000-0000-000000000001',
                    '10000000-0000-0000-0000-000000000001', 'x4', '5411', 1500, 75,
                    '20000000-0000-0000-0000-0000000000a2')""")
    assert exc.value.diag.constraint_name == "uq_accrual_recommendation"


# ── блокировки: сразу и не дольше lock_timeout ───────────────────────────────
def _waiting_on_a_lock(dsn: str) -> bool:
    return _query(dsn, """
        SELECT count(*) FROM pg_stat_activity
         WHERE datname = current_database() AND wait_event_type = 'Lock'""")[0][0] > 0


@pytest.mark.parametrize(("busy", "probe"), [
    # отчёт читает журнал начислений: миграция ждёт его, ничего не держа —
    # клиенты читаются как обычно
    ("cashback_accruals", "users"),
    # отчёт читает клиентов (последними в порядке): ждать дольше
    # lock_timeout миграция не станет — начисления снова доступны
    ("users", "cashback_accruals"),
])
def test_upgrade_takes_its_locks_up_front_and_gives_up_after_lock_timeout(
        fresh_dsn, busy, probe):
    """Под нагрузкой 009 не держит одни таблицы, ожидая другие (так она
    останавливала сервисы и ловила deadlock с INSERT рекомендации), и не
    ждёт бесконечно: не дождалась — откат до 008, ничего не изменено."""
    run_alembic(fresh_dsn, "upgrade", "008")
    report = psycopg2.connect(fresh_dsn)
    migration = None
    try:
        with report.cursor() as cur:            # ACCESS SHARE до конца транзакции
            cur.execute(f"SELECT count(*) FROM {busy}")
        migration = subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=MIGRATIONS_DIR, env=alembic_env(fresh_dsn),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 30
        while not _waiting_on_a_lock(fresh_dsn):
            assert migration.poll() is None and time.monotonic() < deadline, \
                "the migration never waited for the busy table"
            time.sleep(0.1)

        reader = psycopg2.connect(fresh_dsn)
        try:
            with reader, reader.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '15s'")
                started = time.monotonic()
                cur.execute(f"SELECT count(*) FROM {probe}")
                waited = time.monotonic() - started
        finally:
            reader.close()
        output, _ = migration.communicate(timeout=30)
    finally:
        report.rollback()
        report.close()
        if migration is not None and migration.poll() is None:
            migration.wait(timeout=60)

    assert waited < (1 if probe == "users" else 10)
    assert migration.returncode != 0
    assert "lock timeout" in output
    assert _revision(fresh_dsn) == "008"
    assert "rfm_score" not in _columns_at(fresh_dsn)["users"]
    run_alembic(fresh_dsn, "upgrade", "head")           # отчёт закончился — проходит
    assert _revision(fresh_dsn) == "009"
