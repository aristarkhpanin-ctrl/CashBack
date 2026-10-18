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
* повторный запуск на тех же БД ничего не дублирует — в том числе когда
  демо-кампании уже не ACTIVE (PAUSED / COMPLETED): они переиспользуются
  по имени, а рекомендации идут только в ACTIVE;
* в демо-данные попадают только demo_* пользователи и демо-кампании:
  чужие (mobile_api / e2e, без согласия) не получают транзакций,
  рекомендаций и начислений; недостающие согласия демо-пользователей
  досеваются, отозванные не перезаписываются;
* упавший шаг печатает в stderr полный трейсбек и всё сообщение ошибки,
  а в сводке остаётся одна строка причины;
* B20 — демо-логины админ-панели получают пароль ``DEMO_USERS_PASSWORD``
  (окружение, иначе ``--env-file``), без него — случайный, напечатанный в
  сводке; публичный пароль старого сидера (marketer / analyst) заменяется
  с отзывом сессий, остальные существующие логины не трогаются;
* этап C (деньги, контракт §6) — демо-пользователи получают ``rfm_score``
  (RFM-квинтиль по user_rfm_features, шаг 4b); рекомендации — только в
  кампании, которые ``eligible_campaigns`` отдаёт клиенту (сегмент,
  последнее согласие, RFM); каждое начисление ссылается на свою
  ACCEPTED-рекомендацию (не больше одного на оффер), покупка — между
  принятием и истечением оффера, кэшбэк — ``calculate_cashback`` (минимум
  категории, rate_tiers), дневной лимит и бюджет не превышаются,
  ``budget_spent`` = сумме начислений.
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

STEP_KEYS = ["PG", "1/7", "2/7", "2b/7", "3/7", "4/7", "4b/7", "5/7", "6/7", "6b/7",
             "7/7"]
_STEP_LINE = re.compile(r"^\s+\[(OK|SKIP|FAIL|NOT RUN)\]\s+(\S+)\s+(.*)$")
USERS_RE = r"(\d+) демо-пользователей \(добавлено (\d+), согласий \+(\d+)\)"
CAMPAIGNS_RE = (r"(\d+) демо-кампаний \(создано (\d+); ([^)]*)\), "
                r"ACTIVE-кампаний в БД всего (\d+)")
RFM_RE = (r"rfm_score у (\d+) из (\d+) демо-пользователей "
          r"\(квинтили 1…5: (\d+)/(\d+)/(\d+)/(\d+)/(\d+); без признаков — (\d+)\)")
ACCRUALS_RE = (r"\+(\d+) начислений, budget_spent \+([\d.]+) "
               r"\(не хватило бюджета или дневного лимита кампании: (\d+)\)")
FOREIGN_CAMPAIGN = "Чужая кампания"
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
def stores(postgres_container, redis_container, clickhouse_cashback, tmp_path):
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
                              redis_url=redis_url, rdb=rdb, tmp_path=tmp_path,
                              no_env_file=tmp_path / "absent.env")
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
def _run_seeder(env, *args: str, ch_url: str | None = None,
                demo_password: str | None = None) -> SimpleNamespace:
    """Сидер отдельным процессом. ``DEMO_USERS_PASSWORD`` — только из
    ``demo_password``, а ``--env-file`` по умолчанию указывает на
    несуществующий файл: .env разработчика не влияет на тест."""
    proc_env = {k: v for k, v in os.environ.items() if k != "DEMO_USERS_PASSWORD"}
    if demo_password is not None:
        proc_env["DEMO_USERS_PASSWORD"] = demo_password
    if "--env-file" not in args:
        args = (*args, "--env-file", str(env.no_env_file))
    proc = subprocess.run(
        [sys.executable, str(SEEDER),
         "--pg-dsn", env.pg_dsn,
         "--ch-url", ch_url or env.ch_url,
         "--ch-user", CH_USER, "--ch-pass", CH_PASS, "--ch-db", CH_DB,
         "--redis-url", env.redis_url,
         *args],
        capture_output=True, text=True, timeout=180, cwd=REPO_ROOT, env=proc_env,
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


def _pg_scalar(env, sql: str, params: tuple = ()):
    with env.pg.cursor() as c:
        # Без параметров — None: иначе psycopg2 разбирает `%` в SQL (LIKE 'x%').
        c.execute(sql, params or None)
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
        "accruals_without_offer": (
            "SELECT count(*) FROM cashback_accruals WHERE recommendation_id IS NULL"),
        "rfm_scored": "SELECT count(rfm_score) FROM users",
        "rfm_sum": "SELECT COALESCE(sum(rfm_score), 0) FROM users",
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


def _assert_traceback(run, function: str, exc_name: str) -> None:
    """В stderr есть трейсбек, доходящий до ``function`` в сидере."""
    assert "Traceback (most recent call last)" in run.err, run.log
    assert re.search(rf'File ".*seed_demo_data\.py", line \d+, in {function}\n', run.err), \
        run.log
    assert re.search(rf"^\S*{exc_name}\b", run.err, re.M), run.log


def _demo_campaign_ids(env, status: str | None = None) -> set[str]:
    sql = (f"SELECT campaign_id::text FROM cashback_campaigns "
           f"WHERE name NOT LIKE '{FOREIGN_CAMPAIGN}%'")
    if status:
        sql += f" AND status = '{status}'"
    with env.pg.cursor() as cur:
        cur.execute(sql)
        return {cid for (cid,) in cur.fetchall()}


def _pg_set(env, sql: str) -> set:
    with env.pg.cursor() as cur:
        cur.execute(sql)
        return {row[0] for row in cur.fetchall()}


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seeder_module():
    spec = importlib.util.spec_from_file_location("seed_demo_data", SEEDER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Начисления, которых не заплатил бы движок начислений (transaction_listener):
# нет своей ACCEPTED-рекомендации того же клиента, кампании и MCC; покупка не
# между принятием и истечением оффера (или в будущем); кэшбэк не равен
# calculate_cashback (категория, её минимум, rate_tiers).
_ACCRUALS_OFF_THE_LIVE_PATH = """
    SELECT count(*) FROM cashback_accruals a
      LEFT JOIN recommendations r USING (recommendation_id)
     WHERE r.recommendation_id IS NULL
        OR r.response_status <> 'ACCEPTED'
        OR (r.user_id, r.campaign_id, r.mcc_code) <> (a.user_id, a.campaign_id, a.mcc_code)
        OR a.accrued_at <= r.responded_at OR a.accrued_at >= r.expires_at
        OR a.accrued_at > now()
        OR a.cashback_amount <> calculate_cashback(a.campaign_id, a.mcc_code,
                                                   a.transaction_amount)
"""
# ... и не положенные клиенту в момент покупки (eligible_campaigns: окно дат,
# минимум категории, сегмент, согласие, RFM). Только пока бюджет и дневной
# лимит не ограничивают: функция видит их сегодняшнее состояние.
_ACCRUALS_NOT_ELIGIBLE = """
    SELECT count(*) FROM cashback_accruals a
     WHERE NOT EXISTS (
            SELECT 1 FROM eligible_campaigns(a.user_id, a.mcc_code,
                                             a.transaction_amount, a.accrued_at) e
             WHERE e.campaign_id = a.campaign_id)
"""
# Рекомендации, которых не выдал бы recommendation_api: кампания клиенту не
# положена сейчас (сумма неизвестна — NULL), оффер старше старта кампании,
# ответ на него — в будущем.
_RECOMMENDATIONS_OFF_THE_LIVE_PATH = """
    SELECT count(*) FROM recommendations r
      JOIN cashback_campaigns c USING (campaign_id)
     WHERE r.generated_at < c.start_date
        OR r.responded_at > now()
        OR NOT EXISTS (
            SELECT 1 FROM eligible_campaigns(r.user_id, r.mcc_code, NULL, now()) e
             WHERE e.campaign_id = r.campaign_id)
"""
_BUDGET_MISMATCH = """
    SELECT count(*) FROM cashback_campaigns c
     WHERE c.budget_spent <> (SELECT COALESCE(sum(a.cashback_amount), 0)
                                FROM cashback_accruals a
                               WHERE a.campaign_id = c.campaign_id)
"""


def _assert_ledger_like_the_live_path(env, *, limits_binding: bool = False) -> None:
    """Деньги демо-данных — те, что заплатил бы боевой путь (контракт §6)."""
    assert _pg_scalar(env, "SELECT count(*) FROM cashback_accruals "
                           "WHERE recommendation_id IS NULL") == 0
    assert _pg_scalar(env, "SELECT count(*) FROM (SELECT recommendation_id "
                           "FROM cashback_accruals GROUP BY 1 HAVING count(*) > 1) d") == 0
    assert _pg_scalar(env, _ACCRUALS_OFF_THE_LIVE_PATH) == 0
    assert _pg_scalar(env, _BUDGET_MISMATCH) == 0
    if not limits_binding:
        assert _pg_scalar(env, _ACCRUALS_NOT_ELIGIBLE) == 0
        assert _pg_scalar(env, _RECOMMENDATIONS_OFF_THE_LIVE_PATH) == 0


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
    assert _num(run, "2/7", USERS_RE) == ["40", "40", "40"]
    assert _num(run, "1/7", CAMPAIGNS_RE) == ["7", "7", "ACTIVE 7", "7"]
    assert _num(run, "2b/7", r"\+(\d+) демо-логинов") == ["2"]
    assert _num(run, "3/7", r"\+(\d+) строк в transactions_raw") == ["400"]
    assert _num(run, "4/7", r"(\d+) строк в user_rfm_features") == ["40"]
    by_score = [str(_pg_scalar(stores, "SELECT count(*) FROM users WHERE rfm_score = %s",
                               (level,))) for level in range(1, 6)]
    assert _num(run, "4b/7", RFM_RE) == ["40", "40", *by_score, "0"]
    assert _num(run, "5/7", r"(\d+) ключей features") == ["40"]
    assert _num(run, "6/7", r"\+(\d+) рекомендаций") == [str(c["recommendations"])]
    assert _num(run, "6b/7", r"\+(\d+) экспериментов, \+(\d+) назначений, \+(\d+) ab_events") \
        == ["2", "40", str(c["ab_events"])]
    n_acc, total, _refused = _num(run, "7/7", ACCRUALS_RE)
    assert int(n_acc) == c["accruals"] and Decimal(total) == c["budget_spent"]
    assert c["recommendations"] > 0 and c["accruals"] > 0

    # Этап C: деньги — те, что заплатил бы боевой путь.
    assert c["rfm_scored"] == 40
    _assert_ledger_like_the_live_path(stores)


def test_rfm_score_is_the_quintile_of_the_features(stores):
    """users.rfm_score — RFM-квинтиль по user_rfm_features у каждого
    демо-пользователя: монотонен по каждому из R (меньше дней — лучше), F и
    M, от худших (1) до лучших (5)."""
    run = _run_seeder(stores, "--users", "50", "--tx-per-user", "6")
    assert run.rc == 0, run.log

    features = {}
    for line in _ch_query(stores.ch_url, "SELECT toString(user_id), recency_days, "
                          "frequency_total, monetary_total FROM user_rfm_features "
                          "FORMAT TSV").splitlines():
        uid, days, freq, money = line.split("\t")
        features[uid] = (-int(days), int(freq), Decimal(money))
    with stores.pg.cursor() as cur:
        cur.execute("SELECT user_id::text, rfm_score FROM users")
        scores = dict(cur.fetchall())
    assert set(scores) == set(features) and len(scores) == 50
    # Оба края шкалы заняты (ровное деление на пятёрки при ничьих не
    # гарантировано — его проверяет test_rfm_quintiles_formula).
    assert {1, 5} <= set(scores.values()) <= {1, 2, 3, 4, 5}
    # Не хуже по всем трём измерениям — не ниже оценка.
    for a, fa in features.items():
        for b, fb in features.items():
            if all(x >= y for x, y in zip(fa, fb, strict=True)):
                assert scores[a] >= scores[b], (a, fa, scores[a], b, fb, scores[b])


def test_rfm_quintiles_formula():
    """Квинтиль — по перцентилю среднего места: равные — вместе, общий для
    всех (и единственный) — средний; итог — квинтиль суммы R + F + M."""
    seeder = _seeder_module()

    assert seeder._quintiles({str(i): i for i in range(5)}) == {
        "0": 1, "1": 2, "2": 3, "3": 4, "4": 5}
    assert sorted(seeder._quintiles({str(i): i for i in range(40)}).values()) == \
        [level for level in range(1, 6) for _ in range(8)]
    assert seeder._quintiles({"a": 7}) == {"a": 3}
    assert set(seeder._quintiles({str(i): 7 for i in range(9)}).values()) == {3}
    ties = seeder._quintiles({"a": 1, "b": 1, "c": 2, "d": 3, "e": 3})
    assert ties["a"] == ties["b"] < ties["c"] < ties["d"] == ties["e"]

    # R — давность: 0 дней лучше 89.
    rows = [(f"u{i}", 89 - 10 * i, i, Decimal(100 * i)) for i in range(10)]
    assert seeder.rfm_scores(rows) == {
        "u0": 1, "u1": 1, "u2": 2, "u3": 2, "u4": 3,
        "u5": 3, "u6": 4, "u7": 4, "u8": 5, "u9": 5}
    # Лучший по одному измерению при худших двух — не лучший в итоге.
    mixed = [("recent", 0, 1, Decimal("10")), ("loyal", 80, 50, Decimal("90000")),
             ("mid", 40, 20, Decimal("5000"))]
    assert seeder.rfm_scores(mixed)["loyal"] > seeder.rfm_scores(mixed)["recent"]


def test_rerun_on_same_stores_changes_nothing(stores):
    first = _run_seeder(stores, "--users", "30", "--tx-per-user", "8")
    assert first.rc == 0, first.log
    before = _counts(stores)

    second = _run_seeder(stores, "--users", "30", "--tx-per-user", "8")

    assert second.rc == 0, second.log
    assert {s for s, _ in second.steps.values()} == {"OK"}, second.log
    assert _counts(stores) == before
    assert _num(second, "1/7", CAMPAIGNS_RE) == ["7", "0", "ACTIVE 7", "7"]
    assert _num(second, "2/7", USERS_RE) == ["30", "0", "0"]
    assert _num(second, "3/7", r"\+(\d+) строк") == ["0"]
    assert _num(second, "4b/7", RFM_RE) == _num(first, "4b/7", RFM_RE)
    assert _num(second, "6/7", r"\+(\d+) рекомендаций") == ["0"]
    assert _num(second, "7/7", r"\+(\d+) начислений") == ["0"]


def test_growing_users_seeds_consents_only_for_real_users(stores):
    """Второй запуск с бо́льшим --users: demo_* первых 20 уже есть (ON CONFLICT)."""
    first = _run_seeder(stores, "--users", "20", "--tx-per-user", "5")
    assert first.rc == 0, first.log

    second = _run_seeder(stores, "--users", "35", "--tx-per-user", "5")

    assert second.rc == 0, second.log
    assert {s for s, _ in second.steps.values()} == {"OK"}, second.log
    assert _num(second, "2/7", USERS_RE) == ["35", "15", "15"]
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
    n_acc, total, refused = _num(second, "7/7", ACCRUALS_RE)
    assert int(refused) > 0, second.log        # потолок бюджета реально сработал
    c = _counts(stores)
    assert c["over_budget"] == 0
    assert c["budget_spent"] - spent_before == Decimal(total) <= 7 * 100
    assert c["accruals_sum"] == c["budget_spent"]
    _assert_ledger_like_the_live_path(stores, limits_binding=True)
    first_acc = int(_num(first, "7/7", r"\+(\d+) начислений")[0])
    assert c["accruals"] == first_acc + int(n_acc)


def test_offers_and_cashback_only_where_eligible_campaigns_allows(stores):
    """B26: оффер и начисление — только в кампанию, которую eligible_campaigns
    отдаёт клиенту. Сегмент вне таргета, отозванное согласие и RFM-оценка вне
    диапазона кампании — ни рекомендаций, ни начислений."""
    with stores.pg.cursor() as cur:
        # demo_00000000…03: сегмент, на который не нацелена ни одна кампания.
        cur.execute("INSERT INTO users (external_id, segment_id) "
                    "SELECT 'demo_' || lpad(g::text, 8, '0'), 99 "
                    "FROM generate_series(0, 3) g")
        # demo_00000004…07: согласие отозвано (сидер его не перезаписывает).
        cur.execute("INSERT INTO users (external_id, segment_id) "
                    "SELECT 'demo_' || lpad(g::text, 8, '0'), 5 "
                    "FROM generate_series(4, 7) g")
        cur.execute("INSERT INTO user_consents (user_id, consent_type, status, "
                    "document_version) SELECT user_id, 'personalised_cashback', "
                    "'REVOKED', 'v1.0' FROM users WHERE segment_id = 5")
        # Демо-кампанию «Кэшбэк на продукты» админ уже завёл — только для
        # лучших клиентов (rfm 5…5); сидер переиспользует её по имени.
        cur.execute(
            "INSERT INTO cashback_campaigns (name, target_segment_ids, cashback_rate, "
            "min_transaction_amount, budget_total, status, start_date, end_date, "
            "allowed_channels, rfm_min, rfm_max) VALUES ('Кэшбэк на продукты', "
            "ARRAY[1,2,3,4,5,6,7,8,9,10], 5, 300, 1500000, 'ACTIVE', "
            "now() - interval '30 days', now() + interval '60 days', ARRAY['POS'], 5, 5) "
            "RETURNING campaign_id::text")
        rfm_campaign = cur.fetchone()[0]
        cur.execute("INSERT INTO campaign_categories (campaign_id, mcc_code, "
                    "min_transaction_amount) VALUES (%s, '5411', 300)", (rfm_campaign,))
    excluded = _pg_set(stores, "SELECT user_id::text FROM users")

    run = _run_seeder(stores, "--users", "40", "--tx-per-user", "10")

    assert run.rc == 0, run.log
    assert {s for s, _ in run.steps.values()} == {"OK"}, run.log
    assert _num(run, "1/7", CAMPAIGNS_RE) == ["7", "6", "ACTIVE 7", "7"]
    (not_eligible,) = _num(run, "6/7", r"не положена ни одна кампания — (\d+)")
    assert int(not_eligible) >= len(excluded) == 8, run.log
    offered = _pg_set(stores, "SELECT user_id::text FROM recommendations "
                              "UNION SELECT user_id::text FROM cashback_accruals")
    assert offered and not offered & excluded, run.log
    # RFM-фильтр: в кампанию «только rfm 5» — только клиенты с оценкой 5.
    assert _pg_scalar(stores, "SELECT count(*) FROM recommendations WHERE campaign_id = %s",
                      (rfm_campaign,)) > 0, run.log
    assert _pg_scalar(stores, """
        SELECT count(*) FROM recommendations r JOIN users u USING (user_id)
         WHERE r.campaign_id = %s AND u.rfm_score IS DISTINCT FROM 5""",
                      (rfm_campaign,)) == 0, run.log
    _assert_ledger_like_the_live_path(stores)


def test_accruals_never_exceed_the_daily_limit(stores):
    """B22: как у движка начислений — расход кампании за UTC-сутки не выше
    daily_limit, без частичных выплат."""
    first = _run_seeder(stores, "--users", "30", "--tx-per-user", "2")
    assert first.rc == 0, first.log
    with stores.pg.cursor() as cur:   # журнал с чистого листа, лимит 300 ₽ в сутки
        cur.execute("DELETE FROM cashback_accruals")
        cur.execute("UPDATE cashback_campaigns SET budget_spent = 0, daily_limit = 300")

    second = _run_seeder(stores, "--users", "120", "--tx-per-user", "2")

    assert second.rc == 0, second.log
    n_acc, _total, refused = _num(second, "7/7", ACCRUALS_RE)
    assert int(n_acc) > 0 and int(refused) > 0, second.log   # лимит реально сработал
    assert _pg_scalar(stores, """
        SELECT count(*) FROM (
            SELECT campaign_id FROM cashback_accruals
             GROUP BY campaign_id, date_trunc('day', accrued_at AT TIME ZONE 'UTC')
            HAVING sum(cashback_amount) > 300) d""") == 0
    _assert_ledger_like_the_live_path(stores, limits_binding=True)


def test_unreachable_clickhouse_exits_nonzero_and_lists_failed_steps(stores):
    run = _run_seeder(stores, "--users", "25", "--tx-per-user", "4",
                      ch_url=f"http://127.0.0.1:{_closed_port()}")

    assert run.rc != 0, run.log
    assert run.steps["3/7"][0] == "FAIL", run.log
    assert run.steps["3/7"][1].startswith("URLError"), run.log
    for key in ("4/7", "4b/7", "5/7"):
        assert run.steps[key][0] == "NOT RUN", run.log
    assert "упали шаги 3/7" in run.err, run.log
    # Место ошибки видно в stderr: полный трейсбек до функции шага.
    _assert_traceback(run, "seed_transactions", "URLError")
    # Независимые шаги Postgres выполнены и честно отражены в сводке.
    for key in ("PG", "1/7", "2/7", "2b/7", "6/7", "6b/7", "7/7"):
        assert run.steps[key][0] == "OK", run.log
    c = _counts(stores)
    assert c["users"] == 25 and c["recommendations"] > 0
    assert _num(run, "6/7", r"\+(\d+) рекомендаций") == [str(c["recommendations"])]
    assert c["transactions"] == 0 and c["redis_features"] == 0
    assert c["rfm_scored"] == 0                # без признаков оценка неизвестна


def test_skip_transactions_is_reported_as_skipped_not_as_seeded(stores):
    run = _run_seeder(stores, "--users", "15", "--skip-transactions")

    assert run.rc == 0, run.log
    status, detail = run.steps["3/7"]
    assert status == "SKIP" and "--skip-transactions" in detail, run.log
    assert not re.search(r"\+\d+ строк в transactions_raw|транзакций за 90 дней", run.out)
    assert run.steps["4/7"] == ("OK", "0 строк в user_rfm_features (108 признаков на юзера)")
    assert _num(run, "4b/7", RFM_RE) == ["0", "15", "0", "0", "0", "0", "0", "15"]
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
    # Сводка — одна строка, а трейсбек и многострочное сообщение psycopg2
    # (строка LINE с местом в SQL) целиком уходят в stderr.
    _assert_traceback(run, "seed_admin_users", "UndefinedTable")
    _assert_traceback(run, "seed_ab_experiments", "UndefinedTable")
    assert "LINE 1: INSERT INTO admin_users" in run.err, run.log
    assert "LINE 1: INSERT INTO ab_events" in run.err, run.log
    assert "LINE" not in run.steps["2b/7"][1] + run.steps["6b/7"][1]
    for key in ("1/7", "2/7", "3/7", "4/7", "4b/7", "5/7", "6/7", "7/7"):
        assert run.steps[key][0] == "OK", run.log
    assert int(_ch_query(stores.ch_url, "SELECT count() FROM user_rfm_features")) == 20
    assert sum(1 for _ in stores.rdb.scan_iter("features:*")) == 20
    assert _pg_scalar(stores, "SELECT count(*) FROM recommendations") > 0
    # Упавший шаг откатывается целиком и не ломает транзакцию следующих.
    assert _pg_scalar(stores, "SELECT count(*) FROM ab_experiments") == 0
    assert _num(run, "7/7", r"\+(\d+) начислений") == [
        str(_pg_scalar(stores, "SELECT count(*) FROM cashback_accruals"))]


def test_rerun_reuses_demo_campaigns_that_left_active(stores):
    """Кампанию ставят на паузу (API) и завершают (планировщик после
    end_date): повторный запуск не создаёт демо-кампании заново и не трогает
    их статус."""
    first = _run_seeder(stores, "--users", "20", "--tx-per-user", "3")
    assert first.rc == 0, first.log
    with stores.pg.cursor() as cur:
        cur.execute("UPDATE cashback_campaigns SET status = 'PAUSED' "
                    "WHERE name = 'Кэшбэк на продукты'")
        cur.execute("UPDATE cashback_campaigns SET status = 'COMPLETED', "
                    "end_date = now() - interval '1 day', "
                    "start_date = now() - interval '90 days' "
                    "WHERE name = 'АЗС на лето'")
    active_ids = _demo_campaign_ids(stores, "ACTIVE")
    assert len(active_ids) == 5

    second = _run_seeder(stores, "--users", "32", "--tx-per-user", "3")

    assert second.rc == 0, second.log
    assert {s for s, _ in second.steps.values()} == {"OK"}, second.log
    assert _num(second, "1/7", CAMPAIGNS_RE) == [
        "7", "0", "ACTIVE 5, PAUSED 1, COMPLETED 1", "5"]
    # Ни одной новой кампании, ни одного дубля имени, статусы те же.
    assert _pg_scalar(stores, "SELECT count(*) FROM cashback_campaigns") == 7
    assert _pg_scalar(stores, "SELECT count(DISTINCT name) FROM cashback_campaigns") == 7
    assert _pg_scalar(stores, "SELECT status::text FROM cashback_campaigns "
                              "WHERE name = 'Кэшбэк на продукты'") == "PAUSED"
    assert _pg_scalar(stores, "SELECT status::text FROM cashback_campaigns "
                              "WHERE name = 'АЗС на лето'") == "COMPLETED"
    # Новые 12 пользователей получили рекомендации только в ACTIVE-кампании.
    assert _num(second, "2/7", USERS_RE) == ["32", "12", "12"]
    assert _num(second, "6/7", r"у (\d+) пользователей уже были") == ["20"]
    new_rec_campaigns = _pg_set(stores, """
        SELECT DISTINCT r.campaign_id::text FROM recommendations r
          JOIN users u USING (user_id)
         WHERE u.external_id >= 'demo_00000020'""")
    assert new_rec_campaigns and new_rec_campaigns <= active_ids, second.log
    assert _pg_scalar(stores, "SELECT count(*) FROM cashback_campaigns "
                              "WHERE budget_spent > budget_total") == 0


def test_no_active_demo_campaign_fails_only_when_recommendations_are_needed(stores):
    first = _run_seeder(stores, "--users", "10", "--tx-per-user", "2")
    assert first.rc == 0, first.log
    with stores.pg.cursor() as cur:   # все демо-кампании отработали своё
        cur.execute("UPDATE cashback_campaigns SET status = 'COMPLETED'")
    recs_before = _pg_scalar(stores, "SELECT count(*) FROM recommendations")

    # Тем же пользователям рекомендации уже не нужны — шаг честно OK, +0.
    same = _run_seeder(stores, "--users", "10", "--tx-per-user", "2")
    assert same.rc == 0, same.log
    assert _num(same, "1/7", CAMPAIGNS_RE) == ["7", "0", "ACTIVE 0, COMPLETED 7", "0"]
    assert _num(same, "6/7", r"\+(\d+) рекомендаций") == ["0"]

    # Новым — не к чему привязать: шаг падает с понятной причиной, а не
    # создаёт второй комплект демо-кампаний.
    grown = _run_seeder(stores, "--users", "15", "--tx-per-user", "2")
    assert grown.rc == 1, grown.log
    status, detail = grown.steps["6/7"]
    assert status == "FAIL" and detail.startswith("RuntimeError: нет ACTIVE демо-кампаний"), \
        grown.log
    assert grown.steps["7/7"][0] == "NOT RUN", grown.log
    for key in ("PG", "1/7", "2/7", "2b/7", "3/7", "4/7", "4b/7", "5/7", "6b/7"):
        assert grown.steps[key][0] == "OK", grown.log
    _assert_traceback(grown, "seed_recommendations", "RuntimeError")
    assert _pg_scalar(stores, "SELECT count(*) FROM cashback_campaigns") == 7
    assert _pg_scalar(stores, "SELECT count(*) FROM cashback_campaigns "
                              "WHERE status = 'ACTIVE'") == 0
    assert _pg_scalar(stores, "SELECT count(*) FROM recommendations") == recs_before


def test_foreign_users_and_campaigns_stay_out_of_demo_data(stores):
    """Пользователи mobile_api / e2e (без согласия) и кампании из админки:
    сидер их не выбирает, даже если их больше, чем --users, и они старше."""
    with stores.pg.cursor() as cur:
        cur.execute("INSERT INTO users (external_id, segment_id, created_at, rfm_score) "
                    "SELECT 'e2e_' || g, 5, now() - interval '30 days', 2 "
                    "FROM generate_series(1, 40) g")
        for i in range(7):
            cur.execute(
                "INSERT INTO cashback_campaigns (name, target_segment_ids, "
                "cashback_rate, budget_total, status, start_date, end_date) "
                "VALUES (%s, ARRAY[1,2,3,4,5,6,7,8,9,10], 5, 100000, 'ACTIVE', "
                "now() - interval '1 day', now() + interval '30 days') "
                "RETURNING campaign_id", (f"{FOREIGN_CAMPAIGN} {i}",))
            cid = cur.fetchone()[0]
            cur.execute("INSERT INTO campaign_categories (campaign_id, mcc_code) "
                        "VALUES (%s, '5411')", (cid,))
    foreign_users = _pg_set(stores, "SELECT user_id::text FROM users")
    foreign_campaigns = _pg_set(stores, "SELECT campaign_id::text FROM cashback_campaigns")

    run = _run_seeder(stores, "--users", "30", "--tx-per-user", "4")

    assert run.rc == 0, run.log
    assert {s for s, _ in run.steps.values()} == {"OK"}, run.log
    assert _num(run, "2/7", USERS_RE) == ["30", "30", "30"]
    assert _num(run, "1/7", CAMPAIGNS_RE) == ["7", "7", "ACTIVE 7", "14"]
    demo_users = _pg_set(stores, "SELECT user_id::text FROM users "
                                 "WHERE external_id LIKE 'demo\\_%'")
    demo_campaigns = _demo_campaign_ids(stores)
    assert len(demo_users) == 30 and len(demo_campaigns) == 7

    ch_users = set(_ch_query(stores.ch_url, "SELECT DISTINCT toString(user_id) "
                             "FROM transactions_raw FORMAT TSV").split())
    assert ch_users == demo_users
    assert int(_ch_query(stores.ch_url, "SELECT count() FROM user_rfm_features")) == 30
    rec_users = _pg_set(stores, "SELECT DISTINCT user_id::text FROM recommendations")
    rec_campaigns = _pg_set(stores, "SELECT DISTINCT campaign_id::text FROM recommendations")
    acc_users = _pg_set(stores, "SELECT DISTINCT user_id::text FROM cashback_accruals")
    acc_campaigns = _pg_set(stores, "SELECT DISTINCT campaign_id::text FROM cashback_accruals")
    ab_users = _pg_set(stores, "SELECT DISTINCT user_id::text FROM ab_assignments")
    assert rec_users and rec_users <= demo_users
    assert ab_users <= demo_users
    assert acc_users <= demo_users
    assert rec_campaigns and rec_campaigns <= demo_campaigns
    assert acc_campaigns <= demo_campaigns
    # У каждого, кому что-то посеяно, есть GRANTED-согласие (applicable filter).
    assert _pg_scalar(stores, """
        SELECT count(*) FROM (SELECT user_id FROM recommendations
                              UNION SELECT user_id FROM cashback_accruals) s
         WHERE NOT EXISTS (SELECT 1 FROM user_consents c
                            WHERE c.user_id = s.user_id
                              AND c.consent_type = 'personalised_cashback'
                              AND c.status = 'GRANTED')""") == 0
    # Чужие данные не тронуты: ни согласий, ни бюджета, ни рекомендаций,
    # ни RFM-оценки (её сидер пишет только демо-пользователям).
    assert _pg_scalar(stores, "SELECT count(*) FROM user_consents") == 30
    assert _pg_scalar(stores, "SELECT count(*) FROM users WHERE external_id LIKE 'e2e\\_%' "
                              "AND rfm_score IS DISTINCT FROM 2") == 0
    assert _pg_scalar(stores, "SELECT count(rfm_score) FROM users "
                              "WHERE external_id LIKE 'demo\\_%'") == 30
    assert _pg_scalar(stores, f"SELECT sum(budget_spent) FROM cashback_campaigns "
                              f"WHERE name LIKE '{FOREIGN_CAMPAIGN}%'") == 0
    assert not (rec_users | ch_users) & foreign_users
    assert not (rec_campaigns | acc_campaigns) & foreign_campaigns

    before = _counts(stores)
    again = _run_seeder(stores, "--users", "30", "--tx-per-user", "4")
    assert again.rc == 0, again.log
    assert _num(again, "1/7", CAMPAIGNS_RE) == ["7", "0", "ACTIVE 7", "14"]
    assert _num(again, "2/7", USERS_RE) == ["30", "0", "0"]
    assert _counts(stores) == before


def test_rerun_adds_missing_consents_but_keeps_revoked(stores):
    first = _run_seeder(stores, "--users", "20", "--tx-per-user", "2")
    assert first.rc == 0, first.log
    with stores.pg.cursor() as cur:
        cur.execute("DELETE FROM user_consents WHERE user_id IN (SELECT user_id "
                    "FROM users WHERE external_id IN "
                    "('demo_00000001', 'demo_00000002', 'demo_00000003'))")
        cur.execute("UPDATE user_consents SET status = 'REVOKED' WHERE user_id IN "
                    "(SELECT user_id FROM users WHERE external_id IN "
                    "('demo_00000004', 'demo_00000005'))")
    assert _counts(stores)["users_without_consent"] == 3

    second = _run_seeder(stores, "--users", "20", "--tx-per-user", "2")

    assert second.rc == 0, second.log
    assert _num(second, "2/7", USERS_RE) == ["20", "0", "3"]
    c = _counts(stores)
    assert c["users"] == 20 and c["consents"] == 20
    assert c["users_without_consent"] == 0 and c["duplicate_consents"] == 0
    assert _pg_scalar(stores, "SELECT count(*) FROM user_consents "
                              "WHERE status = 'REVOKED'") == 2


# ---------------------------------------------------------------------------
# B20: пароли демо-логинов админ-панели
# ---------------------------------------------------------------------------
DEMO_LOGINS = ("m.sokolova@bank.ru", "d.ivanov@bank.ru")
# Что ставил сидер до stage B: пароль = роль (marketer / analyst), хэши
# были в репозитории — ими знают войти все.
LEGACY_LOGIN_HASHES = {
    "m.sokolova@bank.ru": "$2b$12$yygSc5CQMnJW2odxHX/31edrZY5etN6U4afFW6/NtH.JgbcEwSmNS",
    "d.ivanov@bank.ru": "$2b$12$7urn2rB62CxjIkZhTLw2ueE0wjoyhMn0cwC.ZCkMsS1nmIy7ad3XG",
}
DEMO_PASSWORD = "Demo-logins-0123456789"
LOGINS_RE = r"\+(\d+) демо-логинов админ-панели, публичный пароль заменён у (\d+)"
RANDOM_PASSWORD_RE = r"пароль \(случайный, DEMO_USERS_PASSWORD не задан\): (\S+)$"


def _password_ok(env, email: str, password: str) -> bool:
    """Проверка тем же bcrypt, что у логина (pgcrypto ↔ python bcrypt)."""
    return _pg_scalar(env, "SELECT password_hash = crypt(%s, password_hash) "
                           "FROM admin_users WHERE email = %s", (password, email))


def _login_row(env, email: str) -> tuple:
    with env.pg.cursor() as cur:
        cur.execute("SELECT password_hash, token_version, password_changed_at "
                    "FROM admin_users WHERE email = %s", (email,))
        return cur.fetchone()


def test_demo_logins_get_demo_users_password(stores):
    run = _run_seeder(stores, "--users", "5", "--tx-per-user", "1",
                      demo_password=DEMO_PASSWORD)

    assert run.rc == 0, run.log
    assert _num(run, "2b/7", LOGINS_RE) == ["2", "0"]
    assert run.steps["2b/7"][1].endswith("пароль — DEMO_USERS_PASSWORD"), run.log
    # Пароль оператора не попадает в вывод (CI-логи, терминал).
    assert DEMO_PASSWORD not in run.out + run.err
    for email, public in zip(DEMO_LOGINS, ("marketer", "analyst"), strict=True):
        assert _password_ok(stores, email, DEMO_PASSWORD), email
        assert not _password_ok(stores, email, public), email
        assert _login_row(stores, email)[0] not in LEGACY_LOGIN_HASHES.values()


def test_demo_users_password_is_read_from_env_file_and_env_wins(stores):
    env_file = stores.tmp_path / "demo.env"
    env_file.write_text(f"# make env\nDEMO_USERS_PASSWORD={DEMO_PASSWORD}\n")

    run = _run_seeder(stores, "--users", "5", "--tx-per-user", "1",
                      "--env-file", str(env_file))
    assert run.rc == 0, run.log
    assert all(_password_ok(stores, email, DEMO_PASSWORD) for email in DEMO_LOGINS)

    # Окружение важнее файла — как у docker compose: удалённый логин сидер
    # создаёт заново уже с паролем из окружения.
    with stores.pg.cursor() as cur:
        cur.execute("DELETE FROM admin_users WHERE email = %s", (DEMO_LOGINS[0],))
    again = _run_seeder(stores, "--users", "5", "--tx-per-user", "1",
                        "--env-file", str(env_file),
                        demo_password="From-the-environment-42")
    assert again.rc == 0, again.log
    assert _password_ok(stores, DEMO_LOGINS[0], "From-the-environment-42")


def test_demo_logins_without_password_get_a_random_one_shown_in_summary(stores):
    run = _run_seeder(stores, "--users", "5", "--tx-per-user", "1")

    assert run.rc == 0, run.log
    assert _num(run, "2b/7", LOGINS_RE) == ["2", "0"]
    (password,) = _num(run, "2b/7", RANDOM_PASSWORD_RE)
    assert len(password) >= 16
    assert all(_password_ok(stores, email, password) for email in DEMO_LOGINS)
    rows = [_login_row(stores, email) for email in DEMO_LOGINS]

    # Повторный запуск не меняет пароли и не печатает новый.
    again = _run_seeder(stores, "--users", "5", "--tx-per-user", "1")
    assert again.rc == 0, again.log
    assert _num(again, "2b/7", LOGINS_RE) == ["0", "0"]
    assert "не менялись" in again.steps["2b/7"][1], again.log
    assert "случайный" not in again.out
    assert [_login_row(stores, email) for email in DEMO_LOGINS] == rows


def test_legacy_public_demo_password_is_replaced_other_logins_untouched(stores):
    """База, засеянная старым сидером: у m.sokolova остался публичный пароль
    marketer, d.ivanov сменил свой — сидер меняет только первый и отзывает
    его сессии (token_version)."""
    with stores.pg.cursor() as cur:
        cur.execute(
            "INSERT INTO admin_users (email, password_hash, full_name, role) VALUES "
            "(%s, %s, 'Мария Соколова', 'MARKETER'), "
            "(%s, crypt('Ivanov-own-password', gen_salt('bf', 4)), 'Дмитрий Иванов', 'ANALYST')",
            (DEMO_LOGINS[0], LEGACY_LOGIN_HASHES[DEMO_LOGINS[0]], DEMO_LOGINS[1]),
        )
    ivanov_before = _login_row(stores, DEMO_LOGINS[1])

    run = _run_seeder(stores, "--users", "5", "--tx-per-user", "1",
                      demo_password=DEMO_PASSWORD)

    assert run.rc == 0, run.log
    assert _num(run, "2b/7", LOGINS_RE) == ["0", "1"]
    pw_hash, version, changed_at = _login_row(stores, DEMO_LOGINS[0])
    assert pw_hash != LEGACY_LOGIN_HASHES[DEMO_LOGINS[0]]
    assert _password_ok(stores, DEMO_LOGINS[0], DEMO_PASSWORD)
    assert not _password_ok(stores, DEMO_LOGINS[0], "marketer")
    assert version == 1 and changed_at is not None
    assert _login_row(stores, DEMO_LOGINS[1]) == ivanov_before
    assert _password_ok(stores, DEMO_LOGINS[1], "Ivanov-own-password")


@pytest.mark.parametrize("weak", ["marketer", "short-pw", "please-change-me"])
def test_weak_demo_users_password_fails_only_its_step(stores, weak):
    run = _run_seeder(stores, "--users", "5", "--tx-per-user", "1",
                      demo_password=weak)

    assert run.rc != 0, run.log
    status, detail = run.steps["2b/7"]
    assert status == "FAIL", run.log
    assert "DEMO_USERS_PASSWORD must be 12+ characters" in detail
    assert weak not in detail
    assert {key for key, (st, _) in run.steps.items() if st != "OK"} == {"2b/7"}, run.log
    assert _pg_scalar(stores, "SELECT count(*) FROM admin_users "
                              "WHERE email = ANY(%s)", (list(DEMO_LOGINS),)) == 0
