"""admin users for the management UI (phase 15 — auth/RBAC)

Bootstrap-администратор ``admin@bank.ru`` (B20): пароль — из переменной
окружения ``ADMIN_BOOTSTRAP_PASSWORD`` (≥ 12 символов, не известный дефолт;
иначе миграция падает с понятным сообщением), а если она не задана —
случайный ``secrets.token_urlsafe(18)``, который печатается в stdout ОДИН раз
(«bootstrap admin password: …»). Пароля ``admin`` больше нет.

Сгенерированный пароль никто не выбирал, поэтому его нужно сменить при первом
входе. Колонки ``must_change_password`` здесь ещё нет (её добавляет 008 —
и для БД, созданных старой 002), поэтому 002 оставляет пометку в таблице
``alembic_bootstrap`` (email + хэш сгенерированного пароля), а 008 по ней
ставит флаг — только если хэш с тех пор не менялся.

Хэш считает Postgres (``crypt(…, gen_salt('bf', 12))`` из pgcrypto, его
создаёт 001): контейнеру миграций не нужен bcrypt. Пароль при этом уходит
в БД открытым текстом в запросе — тому, кто читает логи сервера БД, он и так
не нужен: такой доступ позволяет переписать хэш напрямую.

Revision ID: 002
Revises: 001
Create Date: 2026-07-11
"""
from __future__ import annotations

import os
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ADMIN_ROLE_VALUES = ("ADMIN", "MARKETER", "ANALYST")
BOOTSTRAP_EMAIL = "admin@bank.ru"
BOOTSTRAP_PASSWORD_ENV = "ADMIN_BOOTSTRAP_PASSWORD"
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72              # предел bcrypt
KNOWN_DEFAULT_PASSWORDS = frozenset({
    "admin", "dev-secret-change-me", "please-change-me", "change-me",
    "changeme", "secret", "test-secret",
})


def bootstrap_password() -> tuple[str, bool]:
    """``(пароль, сгенерирован_ли)``: из окружения или случайный."""
    password = os.environ.get(BOOTSTRAP_PASSWORD_ENV, "")
    if not password:
        return secrets.token_urlsafe(18), True
    if (len(password) < MIN_PASSWORD_LENGTH
            or len(password.encode()) > MAX_PASSWORD_BYTES
            or password.strip().lower() in KNOWN_DEFAULT_PASSWORDS):
        raise RuntimeError(
            f"{BOOTSTRAP_PASSWORD_ENV} must be {MIN_PASSWORD_LENGTH}+ characters "
            f"(at most {MAX_PASSWORD_BYTES} bytes) and not a known default; "
            f"unset it to generate a random password"
        )
    return password, False


def upgrade() -> None:
    admin_role = postgresql.ENUM(*ADMIN_ROLE_VALUES, name="admin_role")
    admin_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "admin_users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("role",
                  postgresql.ENUM(*ADMIN_ROLE_VALUES, name="admin_role",
                                  create_type=False),
                  nullable=False, server_default="ANALYST"),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_admin_users_email", "admin_users", ["email"])
    op.create_table(
        "alembic_bootstrap",
        sa.Column("email", sa.String(255), primary_key=True),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # Bootstrap-администратор: без него свежий стенд недоступен для логина.
    password, generated = bootstrap_password()
    op.execute(
        sa.text(
            """
            INSERT INTO admin_users (email, password_hash, full_name, role)
            VALUES (:email, crypt(:password, gen_salt('bf', 12)), :name, 'ADMIN')
            """
        ).bindparams(email=BOOTSTRAP_EMAIL, password=password,
                     name="Аристарх Панин")
    )
    if generated:
        op.execute(
            sa.text(
                """
                INSERT INTO alembic_bootstrap (email, password_hash)
                SELECT email, password_hash FROM admin_users WHERE email = :email
                """
            ).bindparams(email=BOOTSTRAP_EMAIL)
        )
        print(f"bootstrap admin password: {password}", flush=True)


def downgrade() -> None:
    op.drop_table("alembic_bootstrap")
    op.drop_table("admin_users")
    postgresql.ENUM(name="admin_role").drop(op.get_bind(), checkfirst=True)
