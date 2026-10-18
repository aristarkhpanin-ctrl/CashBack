"""Общие помощники тестов миграций: запуск alembic, БД, фабрики строк."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2

MIGRATIONS_DIR = Path(__file__).resolve().parents[1]
REPO = MIGRATIONS_DIR.parent

# Опорный момент «сейчас» для правил eligibility: явные даты вместо now().
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def plain_dsn(dsn: str) -> str:
    """``postgresql+<driver>://…`` → ``postgresql://…`` (для psycopg2/alembic)."""
    return re.sub(r"^postgres(?:ql)?\+\w+://", "postgresql://", dsn)


def alembic_env(dsn: str) -> dict[str, str]:
    """Окружение alembic-подпроцесса для БД ``dsn``."""
    # 002/008 генерируют случайный пароль bootstrap-админа, если переменная
    # не задана; чужое (возможно, слабое) значение из окружения не берём.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_BOOTSTRAP_PASSWORD", "DEMO_USERS_PASSWORD")}
    env["POSTGRES_DSN"] = dsn
    return env


def run_alembic(dsn: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """alembic подпроцессом, как ``scripts/init_db.sh``."""
    proc = subprocess.run([sys.executable, "-m", "alembic", *args],
                          cwd=MIGRATIONS_DIR, env=alembic_env(dsn),
                          capture_output=True, text=True, timeout=300)
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


class Databases:
    """Создаёт и удаляет БД на сервере ``server_dsn`` (нужно право CREATEDB)."""

    def __init__(self, server_dsn: str) -> None:
        self._server = server_dsn
        self._names: list[str] = []

    def create(self) -> str:
        name = f"dbm_{uuid.uuid4().hex[:12]}"
        self._admin(f'CREATE DATABASE "{name}"')
        self._names.append(name)
        return urlsplit(self._server)._replace(path=f"/{name}").geturl()

    def drop_all(self) -> None:
        while self._names:
            self._admin(f'DROP DATABASE IF EXISTS "{self._names.pop()}" WITH (FORCE)')

    def _admin(self, sql: str) -> None:
        conn = psycopg2.connect(self._server)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
        finally:
            conn.close()


class Pg:
    """psycopg2-соединение; запросы, ожидаемые ошибки и фабрики строк.

    Фабрики по умолчанию создают «всё разрешено»: клиент сегмента 3 с
    согласием GRANTED, ACTIVE-кампания на MCC 5411 без минимумов, таргетинга
    и лимитов, окно дат вокруг ``NOW``.
    """

    def __init__(self, conn) -> None:
        self.conn = conn

    def rows(self, sql: str, *params):
        with self.conn.cursor() as cur:
            cur.execute(sql, params or None)
            return cur.fetchall() if cur.description else []

    def value(self, sql: str, *params):
        (row,) = self.rows(sql, *params)
        (value,) = row
        return value

    def error(self, sql: str, *params) -> psycopg2.Error:
        """Ошибка, которой Postgres отвечает на ``sql`` (транзакция жива)."""
        with self.conn.cursor() as cur:
            cur.execute("SAVEPOINT expect_error")
            try:
                cur.execute(sql, params or None)
            except psycopg2.Error as exc:
                cur.execute("ROLLBACK TO SAVEPOINT expect_error")
                return exc
            cur.execute("RELEASE SAVEPOINT expect_error")
        raise AssertionError(f"no error from: {sql} {params}")

    # -- фабрики -------------------------------------------------------------
    def user(self, *, segment_id: int | None = 3, rfm_score: int | None = None,
             consent: str | None = "GRANTED") -> str:
        user_id = str(uuid.uuid4())
        self.rows("INSERT INTO users (user_id, external_id, segment_id, rfm_score) "
                  "VALUES (%s, %s, %s, %s)",
                  user_id, f"t_{user_id}", segment_id, rfm_score)
        if consent is not None:
            self.consent(user_id, consent, NOW - timedelta(days=10))
        return user_id

    def consent(self, user_id: str, status: str, created_at: datetime, *,
                consent_type: str = "personalised_cashback",
                consent_id: str | None = None) -> str:
        consent_id = consent_id or str(uuid.uuid4())
        self.rows("INSERT INTO user_consents (consent_id, user_id, consent_type, "
                  "status, document_version, created_at) "
                  "VALUES (%s, %s, %s, %s, 'v1', %s)",
                  consent_id, user_id, consent_type, status, created_at)
        return consent_id

    def campaign(self, *, mccs: dict[str, float | None] | None = None,
                 status: str = "ACTIVE", rate: float = 5.0,
                 min_amount: float | None = None, budget_total: float = 100000,
                 budget_spent: float = 0, daily_limit: float | None = None,
                 start: datetime | None = None, end: datetime | None = None,
                 segments: list[int] | None = None, rfm_min: int | None = None,
                 rfm_max: int | None = None, tiers: list | None = None,
                 archived_at: datetime | None = None, name: str = "test") -> str:
        """Кампания и её категории: ``mccs`` — MCC → минимум категории."""
        campaign_id = str(uuid.uuid4())
        self.rows(
            """
            INSERT INTO cashback_campaigns (
                campaign_id, name, target_segment_ids, cashback_rate,
                min_transaction_amount, budget_total, budget_spent, status,
                start_date, end_date, allowed_channels, rate_tiers,
                daily_limit, rfm_min, rfm_max, archived_at)
            VALUES (%s, %s, %s::int[], %s, %s, %s, %s, %s, %s, %s,
                    ARRAY['PUSH', 'IN_APP'], %s, %s, %s, %s, %s)
            """,
            campaign_id, name, segments or [], rate, min_amount, budget_total,
            budget_spent, status, start or NOW - timedelta(days=30),
            end or NOW + timedelta(days=30),
            json.dumps(tiers) if tiers is not None else None,
            daily_limit, rfm_min, rfm_max, archived_at,
        )
        for mcc, mcc_min in (mccs if mccs is not None else {"5411": None}).items():
            self.rows("INSERT INTO campaign_categories (campaign_id, mcc_code, "
                      "min_transaction_amount) VALUES (%s, %s, %s)",
                      campaign_id, mcc, mcc_min)
        return campaign_id

    def recommendation(self, user_id: str, campaign_id: str, *, mcc: str = "5411",
                       status: str = "ACCEPTED") -> str:
        rec_id = str(uuid.uuid4())
        self.rows("INSERT INTO recommendations (recommendation_id, user_id, "
                  "campaign_id, mcc_code, model_score, response_status, expires_at) "
                  "VALUES (%s, %s, %s, %s, 0.9, %s, %s)",
                  rec_id, user_id, campaign_id, mcc, status, NOW + timedelta(days=7))
        return rec_id

    ACCRUAL_SQL = ("INSERT INTO cashback_accruals (user_id, campaign_id, transaction_id, "
                   "mcc_code, transaction_amount, cashback_amount, accrued_at, "
                   "recommendation_id) VALUES (%s, %s, %s, '5411', 1000, %s, %s, %s)")

    def accrual(self, user_id: str, campaign_id: str, *, cashback: float = 50,
                accrued_at: datetime | None = None,
                recommendation_id: str | None = None,
                transaction_id: str | None = None) -> None:
        self.rows(self.ACCRUAL_SQL, user_id, campaign_id,
                  transaction_id or f"tx-{uuid.uuid4()}", cashback,
                  accrued_at or NOW, recommendation_id)
