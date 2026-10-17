"""Integration: миграции 002/008 (B20) на настоящем Postgres.

Каждый тест — своя свежая БД (``db_dsn`` из conftest.py); alembic
запускается подпроцессом, как ``scripts/init_db.sh``.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "db_migrations"
LEGACY_ADMIN_HASH = "$2b$12$qIYXRcx2QAiOif7zODxWR.4XxKISlyUrGxt5MV8EKwKf7WA/E9hOq"
# Демо-логины прежнего сидера: email → (роль, публичный пароль, его bcrypt).
LEGACY_DEMO_USERS = {
    "m.sokolova@bank.ru": (
        "MARKETER", "marketer",
        "$2b$12$yygSc5CQMnJW2odxHX/31edrZY5etN6U4afFW6/NtH.JgbcEwSmNS"),
    "d.ivanov@bank.ru": (
        "ANALYST", "analyst",
        "$2b$12$7urn2rB62CxjIkZhTLw2ueE0wjoyhMn0cwC.ZCkMsS1nmIy7ad3XG"),
}
PASSWORD_LINE = re.compile(r"^bootstrap admin password: (\S+)$", re.MULTILINE)
DEMO_PASSWORD_LINE = re.compile(r"^demo users password: (\S+)$", re.MULTILINE)


def _alembic(dsn: str, *args: str, password: str | None = None,
             demo_password: str | None = None,
             check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("ADMIN_BOOTSTRAP_PASSWORD", "DEMO_USERS_PASSWORD")}
    env["POSTGRES_DSN"] = dsn
    if password is not None:
        env["ADMIN_BOOTSTRAP_PASSWORD"] = password
    if demo_password is not None:
        env["DEMO_USERS_PASSWORD"] = demo_password
    proc = subprocess.run([sys.executable, "-m", "alembic", *args],
                          cwd=MIGRATIONS_DIR, env=env,
                          capture_output=True, text=True, timeout=120)
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _query(dsn: str, sql: str, *params):
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


def _admin(dsn: str) -> tuple[str, bool, int]:
    ((hashed, must_change, version),) = _query(
        dsn, "SELECT password_hash, must_change_password, token_version "
             "FROM admin_users WHERE email = 'admin@bank.ru'")
    return hashed, must_change, version


def _verifies(password: str, hashed: str) -> bool:
    from app.security import verify_password
    return verify_password(password, hashed)


def _columns(dsn: str) -> set[str]:
    rows = _query(dsn, "SELECT column_name FROM information_schema.columns "
                       "WHERE table_name = 'admin_users'")
    return {r[0] for r in rows}


def _as_old_002(dsn: str) -> None:
    """БД, созданная прежней 002: пароль admin, без пометки alembic_bootstrap."""
    _query(dsn, "UPDATE admin_users SET password_hash = %s", LEGACY_ADMIN_HASH)
    _query(dsn, "DROP TABLE alembic_bootstrap")


# ── свежая установка ──────────────────────────────────────────────────────────
def test_fresh_install_prints_random_password_once_and_forces_change(db_dsn):
    proc = _alembic(db_dsn, "upgrade", "head")
    printed = PASSWORD_LINE.findall(proc.stdout)
    assert len(printed) == 1, proc.stdout
    hashed, must_change, version = _admin(db_dsn)
    assert _verifies(printed[0], hashed) and not _verifies("admin", hashed)
    assert hashed.startswith("$2a$12$")                  # bcrypt той же стоимости
    assert must_change is True and version == 0


def test_fresh_install_with_operator_password(db_dsn):
    proc = _alembic(db_dsn, "upgrade", "head", password="Operator-chosen-42")
    assert PASSWORD_LINE.search(proc.stdout) is None
    hashed, must_change, _ = _admin(db_dsn)
    assert _verifies("Operator-chosen-42", hashed)
    assert must_change is False


@pytest.mark.parametrize("weak", ["admin", "short-pass", "please-change-me"])
def test_weak_operator_password_fails_with_clear_message(db_dsn, weak):
    proc = _alembic(db_dsn, "upgrade", "head", password=weak, check=False)
    assert proc.returncode != 0
    assert "ADMIN_BOOTSTRAP_PASSWORD must be 12+ characters" in proc.stderr
    tables = _query(db_dsn, "SELECT to_regclass('admin_users')")
    assert tables == [(None,)]                         # транзакция откатилась


# ── существующая БД со встроенным admin/admin ────────────────────────────────
def test_legacy_admin_password_is_replaced_and_must_be_changed(db_dsn):
    _alembic(db_dsn, "upgrade", "007")
    _as_old_002(db_dsn)
    _query(db_dsn, "INSERT INTO admin_users (email, password_hash, full_name, role) "
                   "VALUES ('m@bank.ru', '$2b$12$other', 'M', 'MARKETER')")
    proc = _alembic(db_dsn, "upgrade", "head")
    (printed,) = PASSWORD_LINE.findall(proc.stdout)
    hashed, must_change, _ = _admin(db_dsn)
    assert not _verifies("admin", hashed)            # admin/admin больше не входит
    assert _verifies(printed, hashed)
    assert must_change is True
    other = _query(db_dsn, "SELECT must_change_password FROM admin_users "
                           "WHERE email = 'm@bank.ru'")
    assert other == [(False,)]


def test_legacy_admin_gets_operator_password(db_dsn):
    _alembic(db_dsn, "upgrade", "007")
    _as_old_002(db_dsn)
    proc = _alembic(db_dsn, "upgrade", "head", password="Operator-chosen-42")
    assert PASSWORD_LINE.search(proc.stdout) is None
    hashed, must_change, _ = _admin(db_dsn)
    assert _verifies("Operator-chosen-42", hashed) and must_change is True


# ── существующая БД с демо-логинами прежнего сидера ──────────────────────────
def _as_old_seeder(dsn: str) -> None:
    """Логины marketer/analyst прежнего сидера: пароль был в README."""
    for email, (role, _, hashed) in LEGACY_DEMO_USERS.items():
        _query(dsn, "INSERT INTO admin_users (email, password_hash, full_name, role) "
                    "VALUES (%s, %s, %s, %s)", email, hashed, email, role)


def _demo_users(dsn: str) -> dict[str, tuple[str, bool]]:
    rows = _query(dsn, "SELECT email, password_hash, must_change_password "
                       "FROM admin_users WHERE email = ANY(%s)",
                  list(LEGACY_DEMO_USERS))
    return {email: (hashed, must_change) for email, hashed, must_change in rows}


def test_legacy_demo_logins_lose_their_public_passwords(db_dsn):
    _alembic(db_dsn, "upgrade", "007", password="Operator-chosen-42")
    _as_old_seeder(db_dsn)
    proc = _alembic(db_dsn, "upgrade", "head")
    assert PASSWORD_LINE.search(proc.stdout) is None
    (printed,) = DEMO_PASSWORD_LINE.findall(proc.stdout)
    users = _demo_users(db_dsn)
    assert set(users) == set(LEGACY_DEMO_USERS)
    for email, (hashed, must_change) in users.items():
        public = LEGACY_DEMO_USERS[email][1]
        assert not _verifies(public, hashed), email   # marketer/analyst не входят
        assert _verifies(printed, hashed) and must_change is True
    # bootstrap-админ с паролем оператора не затронут
    hashed, must_change, _ = _admin(db_dsn)
    assert _verifies("Operator-chosen-42", hashed) and must_change is False


def test_legacy_demo_logins_get_demo_users_password(db_dsn):
    _alembic(db_dsn, "upgrade", "007")
    _as_old_seeder(db_dsn)
    proc = _alembic(db_dsn, "upgrade", "head", demo_password="Demo-users-chosen-42")
    assert DEMO_PASSWORD_LINE.search(proc.stdout) is None
    for hashed, must_change in _demo_users(db_dsn).values():
        assert _verifies("Demo-users-chosen-42", hashed) and must_change is True


def test_weak_demo_users_password_fails_only_with_legacy_logins(db_dsn):
    _alembic(db_dsn, "upgrade", "007")
    # без старых демо-логинов пароль не нужен — миграция его не проверяет
    _alembic(db_dsn, "upgrade", "head", demo_password="marketer")
    _alembic(db_dsn, "downgrade", "007")
    _as_old_seeder(db_dsn)
    proc = _alembic(db_dsn, "upgrade", "head", demo_password="marketer", check=False)
    assert proc.returncode != 0
    assert "DEMO_USERS_PASSWORD must be 12+ characters" in proc.stderr
    rows = _query(db_dsn, "SELECT password_hash FROM admin_users WHERE email = ANY(%s)",
                  list(LEGACY_DEMO_USERS))
    assert {r[0] for r in rows} == {h for _, _, h in LEGACY_DEMO_USERS.values()}  # откат


# ── downgrade ─────────────────────────────────────────────────────────────────
def test_downgrade_drops_columns_and_upgrade_is_repeatable(db_dsn):
    _alembic(db_dsn, "upgrade", "head")
    assert {"must_change_password", "token_version",
            "password_changed_at"} <= _columns(db_dsn)
    _alembic(db_dsn, "downgrade", "007")
    assert not {"must_change_password", "token_version",
                "password_changed_at"} & _columns(db_dsn)

    # сгенерированный пароль не менялся → флаг возвращается
    _alembic(db_dsn, "upgrade", "head")
    assert _admin(db_dsn)[1] is True
    # после смены пароля пометка 002 больше не срабатывает
    _query(db_dsn, "UPDATE admin_users SET password_hash = '$2b$12$changed'")
    _alembic(db_dsn, "downgrade", "007")
    _alembic(db_dsn, "upgrade", "head")
    assert _admin(db_dsn)[1] is False

    _alembic(db_dsn, "downgrade", "base")
    assert _query(db_dsn, "SELECT to_regclass('alembic_bootstrap'), "
                          "to_regclass('admin_users')") == [(None, None)]
