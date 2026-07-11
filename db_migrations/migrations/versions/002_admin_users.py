"""admin users for the management UI (phase 15 — auth/RBAC)

Revision ID: 002
Revises: 001
Create Date: 2026-07-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ADMIN_ROLE_VALUES = ("ADMIN", "MARKETER", "ANALYST")

# bcrypt("admin"), 12 раундов — предвычислен, чтобы контейнеру миграций
# не требовался bcrypt. Демо-стенд; в проде пароль меняется при первом входе.
_DEFAULT_ADMIN_HASH = "$2b$12$qIYXRcx2QAiOif7zODxWR.4XxKISlyUrGxt5MV8EKwKf7WA/E9hOq"


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

    # Bootstrap-администратор: без него свежий стенд недоступен для логина.
    op.execute(
        sa.text(
            """
            INSERT INTO admin_users (email, password_hash, full_name, role)
            VALUES (:email, :ph, :name, 'ADMIN')
            ON CONFLICT (email) DO NOTHING
            """
        ).bindparams(
            email="admin@bank.ru",
            ph=_DEFAULT_ADMIN_HASH,
            name="Аристарх Панин",
        )
    )


def downgrade() -> None:
    op.drop_table("admin_users")
    postgresql.ENUM(name="admin_role").drop(op.get_bind(), checkfirst=True)
