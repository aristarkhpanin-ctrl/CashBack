"""Миграции 002/008 (B20): выбор пароля bootstrap-админа без БД.

Поведение на настоящем Postgres (свежая установка, старая БД с admin/admin,
downgrade) — в tests/integration/test_auth_migrations.py.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import bcrypt
import pytest

VERSIONS = Path(__file__).resolve().parents[4] / "db_migrations" / "migrations" / "versions"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"migration_{name}", VERSIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIGRATIONS = [_load("002_admin_users.py"), _load("008_auth_hardening.py")]


@pytest.fixture(params=MIGRATIONS, ids=["002", "008"])
def migration(request) -> ModuleType:
    return request.param


@pytest.mark.parametrize("value", [None, ""])
def test_random_password_when_env_is_unset(monkeypatch, migration, value):
    if value is None:
        monkeypatch.delenv("ADMIN_BOOTSTRAP_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", value)
    first, generated = migration.bootstrap_password()
    second, _ = migration.bootstrap_password()
    assert generated is True
    assert len(first) >= 24 and first != second and first != "admin"


def test_operator_password_from_env(monkeypatch, migration):
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", "Operator-chosen-42")
    assert migration.bootstrap_password() == ("Operator-chosen-42", False)


@pytest.mark.parametrize("weak", [
    "admin", "short-11chr", "please-change-me", "Dev-Secret-Change-Me",
    "ж" * 37,                                      # 74 байта > предела bcrypt
])
def test_weak_operator_password_fails_the_migration(monkeypatch, migration, weak):
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", weak)
    with pytest.raises(RuntimeError) as exc:
        migration.bootstrap_password()
    assert "ADMIN_BOOTSTRAP_PASSWORD" in str(exc.value)
    assert weak not in str(exc.value)


def test_002_no_longer_ships_the_admin_password():
    source = (VERSIONS / "002_admin_users.py").read_text()
    assert MIGRATIONS[1].LEGACY_ADMIN_HASH not in source


def test_008_legacy_hash_is_bcrypt_of_admin():
    """008 узнаёт старые БД по этой константе — она должна быть верной."""
    assert bcrypt.checkpw(b"admin", MIGRATIONS[1].LEGACY_ADMIN_HASH.encode())


# ── демо-логины прежнего сидера (marketer / analyst) ─────────────────────────
def test_008_legacy_demo_hashes_are_bcrypt_of_the_public_passwords():
    """По этим константам 008 находит демо-логины со старым паролем."""
    marketer, analyst = MIGRATIONS[1].LEGACY_DEMO_HASHES
    assert bcrypt.checkpw(b"marketer", marketer.encode())
    assert bcrypt.checkpw(b"analyst", analyst.encode())


def test_demo_users_password_from_env_or_random(monkeypatch):
    monkeypatch.delenv("DEMO_USERS_PASSWORD", raising=False)
    first, generated = MIGRATIONS[1].demo_users_password()
    assert generated is True and len(first) >= 24
    monkeypatch.setenv("DEMO_USERS_PASSWORD", "Demo-users-chosen-42")
    assert MIGRATIONS[1].demo_users_password() == ("Demo-users-chosen-42", False)


@pytest.mark.parametrize("weak", ["marketer", "Analyst", "admin", "short-11chr"])
def test_weak_demo_users_password_fails_the_migration(monkeypatch, weak):
    monkeypatch.setenv("DEMO_USERS_PASSWORD", weak)
    with pytest.raises(RuntimeError) as exc:
        MIGRATIONS[1].demo_users_password()
    assert "DEMO_USERS_PASSWORD" in str(exc.value)
    assert weak not in str(exc.value)
