"""Integration: scripts/seed_demo_data.py против настоящих хранилищ.

Postgres (миграции alembic до head), ClickHouse (SQL-миграции так же, как
scripts/init_db.sh: пользователь/БД ``cashback``) и Redis поднимаются
testcontainers. Сидер запускается отдельным процессом, как в nightly.

Покрывает:
* B8  — шаг [4/7] больше не падает с TypeError, все 7 шагов проходят;
* B16 — сводка печатает фактический исход каждого шага, exit-code ≠ 0,
  если хоть один шаг упал, независимые шаги при этом выполняются;
* сидер не вставляет согласия для несуществующих user_id (FK) при росте
  ``--users`` и не выводит ``budget_spent`` за ``budget_total``
  (CHECK ck_campaign_budget);
* повторный запуск на тех же БД ничего не дублирует.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SEEDER = REPO_ROOT / "scripts" / "seed_demo_data.py"
CH_MIGRATIONS = REPO_ROOT / "infrastructure" / "clickhouse" / "migrations"
CH_IMAGE = "clickhouse/clickhouse-server:24.3-alpine"
# Как в docker-compose.yml / scripts/init_db.sh.
CH_USER = CH_PASS = CH_DB = "cashback"
REDIS_DB = 7  # отдельный индекс: другие модули пишут в /0

# tests/pyproject.toml режет каждый тест на 60 с (pytest-timeout в CI), а в
# первый тест модуля входит подъём трёх контейнеров. Маркер timeout
# регистрирует только сам плагин (--strict-markers), поэтому он условный.
_TIMEOUT = [pytest.mark.timeout(300)] if importlib.util.find_spec("pytest_timeout") else []
pytestmark = [pytest.mark.integration, *_TIMEOUT]

STEP_KEYS = ["PG", "1/7", "2/7", "2b/7", "3/7", "4/7", "5/7", "6/7", "6b/7", "7/7"]
_STEP_LINE = re.compile(r"^\s+\[(OK|SKIP|FAIL|NOT RUN)\]\s+(\S+)\s+(.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# ---------------------------------------------------------------------------
# Контейнеры и окружение одного теста
# ---------------------------------------------------------------------------
def _ch_query(url: str, sql: str, database: str = CH_DB) -> str:
    req = urllib.request.Request(
        f"{url}/?database={database}", data=sql.encode(), method="POST",
        headers={"X-ClickHouse-User": CH_USER, "X-ClickHouse-Key": CH_PASS},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()


@pytest.fixture(scope="module")
def clickhouse_cashback(_docker_available):
    """ClickHouse с тем же пользователем/БД, что в docker-compose.yml."""
    if not _docker_available:
        pytest.skip("Docker is not available — skipping testcontainers fixture")
    pytest.importorskip("testcontainers")
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer(CH_IMAGE)
        .with_env("CLICKHOUSE_DB", CH_DB)
        .with_env("CLICKHOUSE_USER", CH_USER)
        .with_env("CLICKHOUSE_PASSWORD", CH_PASS)
        .with_env("CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT", "1")
        .with_exposed_ports(8123)
    )
    with container:
        url = (f"http://{container.get_container_host_ip()}:"
               f"{container.get_exposed_port(8123)}")
        deadline = time.monotonic() + 120
        while True:
            try:
                if _ch_query(url, "SELECT 1", database="default").strip() == "1":
                    break
            except (urllib.error.URLError, ConnectionError, OSError):
                pass
            if time.monotonic() > deadline:
                pytest.fail(f"ClickHouse не поднялся:\n{container.get_logs()}")
            time.sleep(1)
        yield container, url


def _apply_ch_migrations(container) -> None:
    """Повторяет run_clickhouse_migrations() из scripts/init_db.sh."""
    base = ["clickhouse-client", "--user", CH_USER, "--password", CH_PASS]
    for cmd in (
        [*base, "--query", f"DROP DATABASE IF EXISTS {CH_DB} SYNC"],
        [*base, "--query", f"CREATE DATABASE IF NOT EXISTS {CH_DB}"],
        *(
            [*base, "--database", CH_DB, "--multiquery", "--query", f.read_text()]
            for f in sorted(CH_MIGRATIONS.glob("*.sql"))
        ),
    ):
        code, output = container.exec(cmd)
        assert code == 0, f"{cmd[:6]} -> {code}: {output.decode(errors='replace')}"


@pytest.fixture()
def stores(postgres_container, redis_container, clickhouse_cashback):
    """Чистые PG (alembic head) + ClickHouse (миграции) + Redis для одного теста."""
    psycopg2 = pytest.importorskip("psycopg2")
    redis = pytest.importorskip("redis")
    pytest.importorskip("alembic")

    # ----- Postgres: своя БД в общем контейнере, миграции до head -----
    pg_host = postgres_container.get_container_host_ip()
    pg_port = postgres_container.get_exposed_port(5432)
    pg_user, pg_pass = postgres_container.username, postgres_container.password
    db_name = f"seed_demo_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(host=pg_host, port=pg_port, user=pg_user,
                             password=pg_pass, dbname=postgres_container.dbname)
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute(f'CREATE DATABASE "{db_name}"')
    pg_dsn = f"postgresql://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{db_name}"
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT / "db_migrations", capture_output=True, text=True, timeout=120,
        env={**os.environ, "POSTGRES_DSN": pg_dsn},
    )
    assert migrate.returncode == 0, migrate.stdout + migrate.stderr

    # ----- ClickHouse: БД cashback пересоздаётся под каждый тест -----
    container, ch_url = clickhouse_cashback
    _apply_ch_migrations(container)

    # ----- Redis -----
    redis_url = (f"redis://{redis_container.get_container_host_ip()}:"
                 f"{redis_container.get_exposed_port(6379)}/{REDIS_DB}")
    rdb = redis.Redis.from_url(redis_url, decode_responses=True)
    rdb.flushdb()

    pg = psycopg2.connect(pg_dsn)
    pg.autocommit = True
    try:
        yield SimpleNamespace(pg_dsn=pg_dsn, pg=pg, ch_url=ch_url,
                              redis_url=redis_url, rdb=rdb)
    finally:
        pg.close()
        rdb.flushdb()
        rdb.close()
        with admin.cursor() as c:
            c.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.close()


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
def _run_seeder(env, *args: str, ch_url: str | None = None) -> SimpleNamespace:
    proc = subprocess.run(
        [sys.executable, str(SEEDER),
         "--pg-dsn", env.pg_dsn,
         "--ch-url", ch_url or env.ch_url,
         "--ch-user", CH_USER, "--ch-pass", CH_PASS, "--ch-db", CH_DB,
         "--redis-url", env.redis_url,
         *args],
        capture_output=True, text=True, timeout=180, cwd=REPO_ROOT,
    )
    out, errs = _ANSI.sub("", proc.stdout), _ANSI.sub("", proc.stderr)
    steps: dict[str, tuple[str, str]] = {}
    in_summary = False
    for line in out.splitlines():
        if line.startswith("Итог по шагам"):
            in_summary = True
            continue
        m = _STEP_LINE.match(line) if in_summary else None
        if m:
            _title, _, detail = m.group(3).partition(" — ")
            steps[m.group(2)] = (m.group(1), detail)
    return SimpleNamespace(rc=proc.returncode, out=out, err=errs, steps=steps,
                           log=f"exit={proc.returncode}\n{out}\n--- stderr ---\n{errs}")


def _num(run, key: str, pattern: str) -> list[str]:
    status, detail = run.steps[key]
    m = re.search(pattern, detail)
    assert m, f"{key}: «{detail}» не совпадает с {pattern!r}\n{run.log}"
    return list(m.groups())


def _pg_scalar(env, sql: str):
    with env.pg.cursor() as c:
        c.execute(sql)
        return c.fetchone()[0]


def _counts(env) -> dict[str, object]:
    q = {
        "users": "SELECT count(*) FROM users",
        "consents": "SELECT count(*) FROM user_consents",
        "users_without_consent": (
            "SELECT count(*) FROM users u WHERE NOT EXISTS (SELECT 1 FROM "
            "user_consents c WHERE c.user_id = u.user_id)"),
        "duplicate_consents": (
            "SELECT count(*) FROM (SELECT user_id FROM user_consents "
            "GROUP BY user_id, consent_type HAVING count(*) > 1) d"),
        "campaigns_active": "SELECT count(*) FROM cashback_campaigns WHERE status = 'ACTIVE'",
        "admin_users": "SELECT count(*) FROM admin_users",
        "recommendations": "SELECT count(*) FROM recommendations",
        "accruals": "SELECT count(*) FROM cashback_accruals",
        "accruals_sum": "SELECT COALESCE(sum(cashback_amount), 0) FROM cashback_accruals",
        "budget_spent": "SELECT sum(budget_spent) FROM cashback_campaigns",
        "over_budget": "SELECT count(*) FROM cashback_campaigns WHERE budget_spent > budget_total",
        "ab_experiments": "SELECT count(*) FROM ab_experiments",
        "ab_assignments": "SELECT count(*) FROM ab_assignments",
        "ab_events": "SELECT count(*) FROM ab_events",
    }
    res: dict[str, object] = {k: _pg_scalar(env, sql) for k, sql in q.items()}
    res["transactions"] = int(_ch_query(env.ch_url, "SELECT count() FROM transactions_raw"))
    res["tx_ids"] = int(_ch_query(env.ch_url, "SELECT uniqExact(transaction_id) FROM transactions_raw"))
    res["tx_users"] = int(_ch_query(env.ch_url, "SELECT uniqExact(user_id) FROM transactions_raw"))
    res["rfm"] = int(_ch_query(env.ch_url, "SELECT count() FROM user_rfm_features"))
    res["redis_features"] = sum(1 for _ in env.rdb.scan_iter("features:*", count=1000))
    return res


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------
def test_all_steps_succeed_and_summary_matches_stores(stores):
    run = _run_seeder(stores, "--users", "40", "--tx-per-user", "10")

    assert run.rc == 0, run.log
    assert list(run.steps) == STEP_KEYS, run.log
    assert {s for s, _ in run.steps.values()} == {"OK"}, run.log

    c = _counts(stores)
    assert c["users"] == 40 and c["consents"] == 40
    assert c["users_without_consent"] == 0 and c["duplicate_consents"] == 0
    assert c["campaigns_active"] == 7
    assert c["admin_users"] == 3                      # admin@ из миграции + 2 демо
    assert c["transactions"] == c["tx_ids"] == 400 and c["tx_users"] == 40
    assert c["rfm"] == 40 and c["redis_features"] == 40
    assert c["ab_experiments"] == 2 and c["ab_assignments"] == 40
    assert c["over_budget"] == 0
    assert c["accruals_sum"] == c["budget_spent"]
    feature = json.loads(stores.rdb.get(next(stores.rdb.scan_iter("features:*"))))
    assert feature["frequency_total"] == 10

    # Каждая цифра сводки — то, что реально лежит в хранилищах.
    assert _num(run, "2/7", r"(\d+) пользователей \(добавлено (\d+), согласий \+(\d+)\)") \
        == ["40", "40", "40"]
    assert _num(run, "1/7", r"(\d+) ACTIVE-кампаний \(создано (\d+)\)") == ["7", "7"]
    assert _num(run, "2b/7", r"\+(\d+) демо-логинов") == ["2"]
    assert _num(run, "3/7", r"\+(\d+) строк в transactions_raw") == ["400"]
    assert _num(run, "4/7", r"(\d+) строк в user_rfm_features") == ["40"]
    assert _num(run, "5/7", r"(\d+) ключей features") == ["40"]
    assert _num(run, "6/7", r"\+(\d+) рекомендаций") == [str(c["recommendations"])]
    assert _num(run, "6b/7", r"\+(\d+) экспериментов, \+(\d+) назначений, \+(\d+) ab_events") \
        == ["2", "40", str(c["ab_events"])]
    n_acc, total = _num(run, "7/7", r"\+(\d+) начислений, budget_spent \+([\d.]+)")
    assert int(n_acc) == c["accruals"] and Decimal(total) == c["budget_spent"]
    assert c["recommendations"] > 0 and c["accruals"] > 0


def test_rerun_on_same_stores_changes_nothing(stores):
    first = _run_seeder(stores, "--users", "30", "--tx-per-user", "8")
    assert first.rc == 0, first.log
    before = _counts(stores)

    second = _run_seeder(stores, "--users", "30", "--tx-per-user", "8")

    assert second.rc == 0, second.log
    assert {s for s, _ in second.steps.values()} == {"OK"}, second.log
    assert _counts(stores) == before
    assert _num(second, "3/7", r"\+(\d+) строк") == ["0"]
    assert _num(second, "6/7", r"\+(\d+) рекомендаций") == ["0"]
    assert _num(second, "7/7", r"\+(\d+) начислений") == ["0"]


def test_growing_users_seeds_consents_only_for_real_users(stores):
    """Второй запуск с бо́льшим --users: demo_* первых 20 уже есть (ON CONFLICT)."""
    first = _run_seeder(stores, "--users", "20", "--tx-per-user", "5")
    assert first.rc == 0, first.log

    second = _run_seeder(stores, "--users", "35", "--tx-per-user", "5")

    assert second.rc == 0, second.log
    assert {s for s, _ in second.steps.values()} == {"OK"}, second.log
    assert _num(second, "2/7", r"(\d+) пользователей \(добавлено (\d+), согласий \+(\d+)\)") \
        == ["35", "15", "15"]
    c = _counts(stores)
    assert c["users"] == 35 and c["consents"] == 35
    assert c["users_without_consent"] == 0 and c["duplicate_consents"] == 0
    # Транзакции и признаки — ровно у 35 настоящих пользователей, без дублей.
    assert c["transactions"] == c["tx_ids"] == 35 * 5 and c["tx_users"] == 35
    assert c["rfm"] == 35 and c["redis_features"] == 35
    ch_users = set(_ch_query(stores.ch_url, "SELECT DISTINCT toString(user_id) "
                             "FROM transactions_raw FORMAT TSV").split())
    with stores.pg.cursor() as cur:
        cur.execute("SELECT user_id::text FROM users")
        assert ch_users == {uid for (uid,) in cur.fetchall()}


def test_accruals_never_push_budget_spent_over_budget_total(stores):
    first = _run_seeder(stores, "--users", "20", "--tx-per-user", "2")
    assert first.rc == 0, first.log
    # Бюджеты почти выбраны (как после работы настоящего движка начислений).
    with stores.pg.cursor() as cur:
        cur.execute("UPDATE cashback_campaigns SET budget_total = budget_spent + 100")
    spent_before = _pg_scalar(stores, "SELECT sum(budget_spent) FROM cashback_campaigns")

    second = _run_seeder(stores, "--users", "120", "--tx-per-user", "2")

    assert second.rc == 0, second.log
    assert second.steps["7/7"][0] == "OK", second.log
    n_acc, total, refused = _num(
        second, "7/7",
        r"\+(\d+) начислений, budget_spent \+([\d.]+) \(не хватило бюджета кампании: (\d+)\)")
    assert int(refused) > 0, second.log        # потолок бюджета реально сработал
    c = _counts(stores)
    assert c["over_budget"] == 0
    assert c["budget_spent"] - spent_before == Decimal(total) <= 7 * 100
    assert c["accruals_sum"] == c["budget_spent"]
    per_campaign_mismatch = _pg_scalar(stores, """
        SELECT count(*) FROM cashback_campaigns c
         WHERE c.budget_spent <> (SELECT COALESCE(sum(a.cashback_amount), 0)
                                    FROM cashback_accruals a
                                   WHERE a.campaign_id = c.campaign_id)""")
    assert per_campaign_mismatch == 0
    first_acc = int(_num(first, "7/7", r"\+(\d+) начислений")[0])
    assert c["accruals"] == first_acc + int(n_acc)


def test_unreachable_clickhouse_exits_nonzero_and_lists_failed_steps(stores):
    run = _run_seeder(stores, "--users", "25", "--tx-per-user", "4",
                      ch_url=f"http://127.0.0.1:{_closed_port()}")

    assert run.rc != 0, run.log
    assert run.steps["3/7"][0] == "FAIL", run.log
    assert run.steps["3/7"][1].startswith("URLError"), run.log
    assert run.steps["4/7"][0] == "NOT RUN" and run.steps["5/7"][0] == "NOT RUN", run.log
    assert "упали шаги 3/7" in run.err, run.log
    # Независимые шаги Postgres выполнены и честно отражены в сводке.
    for key in ("PG", "1/7", "2/7", "2b/7", "6/7", "6b/7", "7/7"):
        assert run.steps[key][0] == "OK", run.log
    c = _counts(stores)
    assert c["users"] == 25 and c["recommendations"] > 0
    assert _num(run, "6/7", r"\+(\d+) рекомендаций") == [str(c["recommendations"])]
    assert c["transactions"] == 0 and c["redis_features"] == 0


def test_skip_transactions_is_reported_as_skipped_not_as_seeded(stores):
    run = _run_seeder(stores, "--users", "15", "--skip-transactions")

    assert run.rc == 0, run.log
    status, detail = run.steps["3/7"]
    assert status == "SKIP" and "--skip-transactions" in detail, run.log
    assert not re.search(r"\+\d+ строк в transactions_raw|транзакций за 90 дней", run.out)
    assert run.steps["4/7"] == ("OK", "0 строк в user_rfm_features (108 признаков на юзера)")
    assert _counts(stores)["transactions"] == 0


def test_failed_independent_steps_give_nonzero_exit_but_later_steps_still_run(stores):
    # Схема без admin_users и ab_events (например, старая миграция):
    # шаги 2b и 6b падают, остальные обязаны отработать.
    with stores.pg.cursor() as cur:
        cur.execute("DROP TABLE admin_users CASCADE")
        cur.execute("DROP TABLE ab_events CASCADE")

    run = _run_seeder(stores, "--users", "20", "--tx-per-user", "3")

    assert run.rc != 0, run.log
    assert run.steps["2b/7"][0] == "FAIL" and run.steps["6b/7"][0] == "FAIL", run.log
    assert "UndefinedTable" in run.steps["2b/7"][1]
    assert "упали шаги 2b/7, 6b/7" in run.err, run.log
    for key in ("1/7", "2/7", "3/7", "4/7", "5/7", "6/7", "7/7"):
        assert run.steps[key][0] == "OK", run.log
    assert int(_ch_query(stores.ch_url, "SELECT count() FROM user_rfm_features")) == 20
    assert sum(1 for _ in stores.rdb.scan_iter("features:*")) == 20
    assert _pg_scalar(stores, "SELECT count(*) FROM recommendations") > 0
    # Упавший шаг откатывается целиком и не ломает транзакцию следующих.
    assert _pg_scalar(stores, "SELECT count(*) FROM ab_experiments") == 0
    assert _num(run, "7/7", r"\+(\d+) начислений") == [
        str(_pg_scalar(stores, "SELECT count(*) FROM cashback_accruals"))]
