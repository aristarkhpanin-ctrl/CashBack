"""010: явный контроль A/B, одна конверсия на назначение, paid_at и индексы
окон аналитики (этап D — B33, B34).

* схема на head: ограничения и значения по умолчанию (``pg`` — БД на head,
  тест в своей откатываемой транзакции);
* upgrade на данных 009: контроль по порядку создания, дубли конверсий
  удалены (самая ранняя остаётся), ``paid_at`` выплаченных = ``accrued_at``;
* upgrade → downgrade → upgrade: downgrade возвращает схему 009 в точности,
  строки сохраняются;
* блокировки — сразу и не дольше lock_timeout; чтение рекомендаций не ждёт.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from dbm_support import MIGRATIONS_DIR, NOW, alembic_env, run_alembic

pytestmark = pytest.mark.integration

UNIQUE_VIOLATION = "23505"


def _query(dsn: str, sql: str, *params):
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params or None)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _revision(dsn: str) -> str:
    return _query(dsn, "SELECT version_num FROM alembic_version")[0][0]


# ── схема на head ─────────────────────────────────────────────────────────────
def _experiment(pg, *variants: tuple[str, bool]) -> tuple[str, list[str]]:
    exp_id = str(uuid.uuid4())
    pg.rows("INSERT INTO ab_experiments (experiment_id, name, target_metric, start_date) "
            "VALUES (%s, %s, 'acceptance_rate', %s)", exp_id, f"e-{exp_id}", NOW)
    ids = []
    for name, control in variants:
        ids.append(pg.value("INSERT INTO ab_variants (experiment_id, name, traffic_weight, "
                            "strategy_class, is_control) VALUES (%s, %s, 0.5, 'S', %s) "
                            "RETURNING variant_id::text", exp_id, name, control))
    return exp_id, ids


def _assignment(pg, exp_id: str, variant_id: str) -> str:
    return pg.value("INSERT INTO ab_assignments (user_id, experiment_id, variant_id) "
                    "VALUES (gen_random_uuid(), %s, %s) RETURNING assignment_id::text",
                    exp_id, variant_id)


def test_variant_is_not_control_by_default(pg):
    exp_id, _ = _experiment(pg)
    variant = pg.value("INSERT INTO ab_variants (experiment_id, name, traffic_weight, "
                       "strategy_class) VALUES (%s, 'x', 1, 'S') RETURNING is_control",
                       exp_id)
    assert variant is False


def test_one_control_per_experiment(pg):
    exp_id, _ = _experiment(pg, ("control", True), ("test", False))
    exc = pg.error("INSERT INTO ab_variants (experiment_id, name, traffic_weight, "
                   "strategy_class, is_control) VALUES (%s, 'second', 0, 'S', true)", exp_id)
    assert exc.pgcode == UNIQUE_VIOLATION
    assert exc.diag.constraint_name == "uq_ab_variants_control"
    # у другого эксперимента — свой контроль
    _experiment(pg, ("control", True))
    # без контроля — можно (сидер / до выбора); вариантов-не-контролей сколько угодно
    _experiment(pg, ("a", False), ("b", False))


def test_one_conversion_per_assignment(pg):
    exp_id, (control, _) = _experiment(pg, ("c", True), ("t", False))
    assignment = _assignment(pg, exp_id, control)
    pg.rows("INSERT INTO ab_events (assignment_id, event_type) VALUES "
            "(%s, 'IMPRESSION'), (%s, 'IMPRESSION'), (%s, 'CLICK'), (%s, 'CONVERSION')",
            assignment, assignment, assignment, assignment)
    exc = pg.error("INSERT INTO ab_events (assignment_id, event_type) "
                   "VALUES (%s, 'CONVERSION')", assignment)
    assert exc.pgcode == UNIQUE_VIOLATION
    assert exc.diag.constraint_name == "uq_ab_events_conversion"
    other = _assignment(pg, exp_id, control)
    pg.rows("INSERT INTO ab_events (assignment_id, event_type) VALUES (%s, 'CONVERSION')",
            other)


def test_paid_at_is_nullable_timestamptz(pg):
    user_id, campaign_id = pg.user(), pg.campaign()
    pg.accrual(user_id, campaign_id)
    assert pg.rows("SELECT status::text, paid_at FROM cashback_accruals "
                   "WHERE user_id = %s", user_id) == [("PENDING", None)]
    assert pg.rows("SELECT data_type, is_nullable FROM information_schema.columns "
                   "WHERE table_name = 'cashback_accruals' AND column_name = 'paid_at'") == [
        ("timestamp with time zone", "YES")]


def test_analytics_window_indexes(pg):
    defs = dict(pg.rows("SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN "
                        "('ix_recommendations_generated_window', "
                        "'ix_accruals_accrued_campaign')"))
    assert defs == {
        "ix_recommendations_generated_window":
            "CREATE INDEX ix_recommendations_generated_window ON public.recommendations "
            "USING btree (generated_at) INCLUDE (campaign_id, response_status, user_id)",
        "ix_accruals_accrued_campaign":
            "CREATE INDEX ix_accruals_accrued_campaign ON public.cashback_accruals "
            "USING btree (accrued_at, campaign_id)",
    }
    # окно воронки/KPI читается из индекса, без таблицы
    pg.rows("SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off")
    plan = "\n".join(r[0] for r in pg.rows(
        "EXPLAIN SELECT campaign_id, response_status, user_id FROM recommendations "
        "WHERE generated_at >= now() - interval '30 days'"))
    assert "Index Only Scan using ix_recommendations_generated_window" in plan


# ── upgrade на данных 009 ─────────────────────────────────────────────────────
E_API = "40000000-0000-0000-0000-000000000001"      # создан через API: порядок в JSONB
E_SEED = "40000000-0000-0000-0000-000000000002"     # сидер: без JSONB, имя control
E_PLAIN = "40000000-0000-0000-0000-000000000003"    # без JSONB и без control
E_SINGLE = "40000000-0000-0000-0000-000000000004"
E_EMPTY = "40000000-0000-0000-0000-000000000005"


def _v(n: int) -> str:
    return f"50000000-0000-0000-0000-{n:012d}"


def _a(n: int) -> str:
    return f"60000000-0000-0000-0000-{n:012d}"


def _e(n: int) -> str:
    return f"70000000-0000-0000-0000-{n:012d}"


T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

# (variant_id, эксперимент, имя): меньший variant_id — НЕ контроль там, где
# решает порядок создания или имя
_VARIANTS = [
    (_v(1), E_API, "alpha"), (_v(2), E_API, "zeta"),
    (_v(3), E_SEED, "treatment"), (_v(4), E_SEED, "control"),
    (_v(5), E_PLAIN, "b"), (_v(6), E_PLAIN, "a"),
    (_v(7), E_SINGLE, "only"),
]
# события: (event_id, назначение, тип, минут от T0)
_EVENTS = [
    (_e(1), _a(1), "IMPRESSION", 0), (_e(2), _a(1), "CONVERSION", 30),
    (_e(3), _a(1), "CONVERSION", 10),            # самая ранняя конверсия a1
    (_e(4), _a(1), "CONVERSION", 50), (_e(5), _a(1), "IMPRESSION", 5),
    (_e(6), _a(2), "CONVERSION", 20),            # единственная — остаётся
    (_e(8), _a(3), "CONVERSION", 40),            # одновременные: меньший id
    (_e(7), _a(3), "CONVERSION", 40),
    (_e(9), _a(3), "CLICK", 40),
]


def _seed_009(dsn: str) -> None:
    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            for exp_id, name, payload in [
                    (E_API, "api", [{"name": "zeta"}, {"name": "alpha"}]),
                    (E_SEED, "seed", None), (E_PLAIN, "plain", None),
                    (E_SINGLE, "single", [{"name": "only"}]), (E_EMPTY, "empty", None)]:
                cur.execute("INSERT INTO ab_experiments (experiment_id, name, status, "
                            "target_metric, start_date, variants) "
                            "VALUES (%s, %s, 'ACTIVE', 'acceptance_rate', %s, %s)",
                            (exp_id, name, T0,
                             json.dumps(payload) if payload is not None else None))
            for variant_id, exp_id, name in _VARIANTS:
                cur.execute("INSERT INTO ab_variants (variant_id, experiment_id, name, "
                            "traffic_weight, strategy_class) VALUES (%s, %s, %s, 0.5, 'S')",
                            (variant_id, exp_id, name))
            for n, variant_id in ((1, _v(2)), (2, _v(2)), (3, _v(1))):
                cur.execute("INSERT INTO ab_assignments (assignment_id, user_id, "
                            "experiment_id, variant_id) VALUES (%s, gen_random_uuid(), %s, %s)",
                            (_a(n), E_API, variant_id))
            for event_id, assignment, kind, minutes in _EVENTS:
                cur.execute("INSERT INTO ab_events (event_id, assignment_id, event_type, "
                            "event_at) VALUES (%s, %s, %s, %s)",
                            (event_id, assignment, kind, T0 + timedelta(minutes=minutes)))
            # начисления: выплаченное и ожидающее
            cur.execute("INSERT INTO users (user_id, external_id, segment_id) "
                        "VALUES ('00000000-0000-0000-0000-000000000001', 'payee', 3)")
            cur.execute("INSERT INTO cashback_campaigns (campaign_id, name, cashback_rate, "
                        "budget_total, budget_spent, status, start_date, end_date) "
                        "VALUES ('10000000-0000-0000-0000-000000000001', 'c', 5, 1000, 80, "
                        "'ACTIVE', %s, %s)", (T0 - timedelta(days=30), T0 + timedelta(days=30)))
            for tx, status, minutes in (("paid", "PAID", 0), ("pending", "PENDING", 60)):
                cur.execute("INSERT INTO cashback_accruals (user_id, campaign_id, "
                            "transaction_id, mcc_code, transaction_amount, cashback_amount, "
                            "status, accrued_at) VALUES "
                            "('00000000-0000-0000-0000-000000000001', "
                            "'10000000-0000-0000-0000-000000000001', %s, '5411', 800, 40, "
                            "%s, %s)", (tx, status, T0 + timedelta(minutes=minutes)))
    finally:
        conn.close()


def _controls(dsn: str) -> list[tuple[str, str]]:
    return _query(dsn, "SELECT experiment_id::text, name FROM ab_variants "
                       "WHERE is_control ORDER BY experiment_id")


def _events(dsn: str) -> list[tuple[str, str]]:
    return _query(dsn, "SELECT event_id::text, event_type::text FROM ab_events "
                       "ORDER BY event_id")


def test_upgrade_backfills_controls_conversions_and_paid_at(fresh_dsn):
    run_alembic(fresh_dsn, "upgrade", "009")
    _seed_009(fresh_dsn)

    run_alembic(fresh_dsn, "upgrade", "010")

    # контроль: первый в теле создания (zeta, хотя alpha раньше по алфавиту и
    # по variant_id); без тела — вариант control; иначе — меньший variant_id
    assert _controls(fresh_dsn) == [(E_API, "zeta"), (E_SEED, "control"),
                                    (E_PLAIN, "b"), (E_SINGLE, "only")]
    assert _query(fresh_dsn, "SELECT count(*) FROM ab_variants") == [(len(_VARIANTS),)]
    # конверсия назначения — одна, самая ранняя; другие события не тронуты
    assert _events(fresh_dsn) == [
        (_e(1), "IMPRESSION"), (_e(3), "CONVERSION"), (_e(5), "IMPRESSION"),
        (_e(6), "CONVERSION"), (_e(7), "CONVERSION"), (_e(9), "CLICK")]
    # выплаченное начисление выплачено в момент начисления; ожидающее — нет
    assert _query(fresh_dsn, "SELECT transaction_id, status::text, paid_at "
                             "FROM cashback_accruals ORDER BY transaction_id") == [
        ("paid", "PAID", T0), ("pending", "PENDING", None)]


# ── upgrade → downgrade → upgrade ─────────────────────────────────────────────
_SNAPSHOT_SQL = {
    "columns": """
        SELECT table_name, column_name, data_type, udt_name, is_nullable, column_default
          FROM information_schema.columns WHERE table_schema = 'public'""",
    "constraints": """
        SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
          FROM pg_constraint WHERE connamespace = 'public'::regnamespace""",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes "
               "WHERE schemaname = 'public'",
}


def _snapshot(dsn: str) -> dict[str, set[tuple]]:
    return {name: set(_query(dsn, sql)) for name, sql in _SNAPSHOT_SQL.items()}


NEW_IN_010 = {
    "columns": {("ab_variants", "is_control"), ("cashback_accruals", "paid_at")},
    "indexes": {"uq_ab_variants_control", "uq_ab_events_conversion",
                "ix_recommendations_generated_window", "ix_accruals_accrued_campaign"},
}


def test_cycle_restores_the_009_schema_exactly(fresh_dsn):
    run_alembic(fresh_dsn, "upgrade", "009")
    at_009 = _snapshot(fresh_dsn)
    run_alembic(fresh_dsn, "upgrade", "010")
    assert _revision(fresh_dsn) == "010"
    at_010 = _snapshot(fresh_dsn)
    assert NEW_IN_010["columns"] <= {(t, c) for t, c, *_ in at_010["columns"]}
    assert NEW_IN_010["indexes"] <= {i for _, i, _ in at_010["indexes"]}

    run_alembic(fresh_dsn, "downgrade", "009")
    assert _snapshot(fresh_dsn) == at_009
    run_alembic(fresh_dsn, "upgrade", "010")
    assert _snapshot(fresh_dsn) == at_010


def test_downgrade_keeps_rows_and_upgrade_backfills_again(fresh_dsn):
    run_alembic(fresh_dsn, "upgrade", "009")
    _seed_009(fresh_dsn)
    run_alembic(fresh_dsn, "upgrade", "010")
    # новый код: другой контроль у E_PLAIN, выплата задачей settlement
    _query(fresh_dsn, f"""
        UPDATE ab_variants SET is_control = false WHERE experiment_id = '{E_PLAIN}';
        UPDATE ab_variants SET is_control = true WHERE variant_id = '{_v(6)}';
        UPDATE cashback_accruals SET status = 'PAID', paid_at = now()
         WHERE transaction_id = 'pending';""")
    counts = "SELECT (SELECT count(*) FROM ab_variants), (SELECT count(*) FROM ab_events), " \
             "(SELECT count(*) FROM cashback_accruals WHERE status = 'PAID')"
    before = _query(fresh_dsn, counts)

    run_alembic(fresh_dsn, "downgrade", "009")
    assert _revision(fresh_dsn) == "009"
    assert _query(fresh_dsn, counts) == before == [(len(_VARIANTS), 6, 2)]
    columns = {(t, c) for t, c, *_ in _snapshot(fresh_dsn)["columns"]}
    assert not NEW_IN_010["columns"] & columns

    run_alembic(fresh_dsn, "upgrade", "010")
    # выбор контроля и момент выплаты не пережили downgrade — backfill заново
    assert _controls(fresh_dsn) == [(E_API, "zeta"), (E_SEED, "control"),
                                    (E_PLAIN, "b"), (E_SINGLE, "only")]
    assert _query(fresh_dsn, "SELECT transaction_id, paid_at = accrued_at "
                             "FROM cashback_accruals ORDER BY 1") == [
        ("paid", True), ("pending", True)]


# ── блокировки: сразу и не дольше lock_timeout ───────────────────────────────
def _waiting_on_a_lock(dsn: str) -> bool:
    return _query(dsn, """
        SELECT count(*) FROM pg_stat_activity
         WHERE datname = current_database() AND wait_event_type = 'Lock'""")[0][0] > 0


def test_upgrade_does_not_block_reading_recommendations_and_gives_up(fresh_dsn):
    """Запись рекомендации держит ROW EXCLUSIVE: 010 ждёт SHARE на
    recommendations (последней), но читать рекомендации это не мешает;
    дольше lock_timeout миграция не ждёт — откат до 009, ничего не изменено."""
    run_alembic(fresh_dsn, "upgrade", "009")
    writer = psycopg2.connect(fresh_dsn)
    migration = None
    try:
        with writer.cursor() as cur:            # ROW EXCLUSIVE до конца транзакции
            cur.execute("UPDATE recommendations SET channel = NULL WHERE false")
        migration = subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "010"],
            cwd=MIGRATIONS_DIR, env=alembic_env(fresh_dsn),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 30
        while not _waiting_on_a_lock(fresh_dsn):
            assert migration.poll() is None and time.monotonic() < deadline, \
                "the migration never waited for recommendations"
            time.sleep(0.1)

        reader = psycopg2.connect(fresh_dsn)
        try:
            with reader, reader.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '15s'")
                started = time.monotonic()
                cur.execute("SELECT count(*) FROM recommendations")
                waited = time.monotonic() - started
        finally:
            reader.close()
        output, _ = migration.communicate(timeout=30)
    finally:
        writer.rollback()
        writer.close()
        if migration is not None and migration.poll() is None:
            migration.wait(timeout=60)

    assert waited < 1
    assert migration.returncode != 0
    assert "lock timeout" in output
    assert _revision(fresh_dsn) == "009"
    assert ("ab_variants", "is_control") not in {
        (t, c) for t, c, *_ in _snapshot(fresh_dsn)["columns"]}
    run_alembic(fresh_dsn, "upgrade", "010")            # запись закончилась — проходит
    assert _revision(fresh_dsn) == "010"
