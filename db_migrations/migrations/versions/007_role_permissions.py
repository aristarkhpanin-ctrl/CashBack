"""role_permissions — редактируемая матрица прав (phase 26)

Раньше RBAC был зашит в код (``require_role`` по фикс-набору ролей), а
вкладка «Матрица прав» жила в локальном стейте фронта без персистенса.
Таблица хранит права per-роль (JSONB) и позволяет admin'у их менять;
``require_permission`` читает их (с кэшем). Строка ADMIN — все права,
неизменяема (сервер отклоняет PATCH), чтобы нельзя было залочить админа.

Revision ID: 007
Revises: 006
Create Date: 2026-07-19
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL = {
    "dashboard": True, "campaigns_view": True, "campaigns_create": True,
    "campaigns_edit": True, "campaigns_delete": True, "analytics": True,
    "users": True,
}
_MARKETER = {**_ALL, "campaigns_delete": False, "analytics": False, "users": False}
_ANALYST = {
    "dashboard": True, "campaigns_view": True, "campaigns_create": False,
    "campaigns_edit": False, "campaigns_delete": False, "analytics": True,
    "users": False,
}


def upgrade() -> None:
    op.create_table(
        "role_permissions",
        sa.Column("role", sa.String(16), primary_key=True),
        sa.Column("permissions", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_by", sa.String(255), nullable=True),
    )
    rp = sa.table(
        "role_permissions",
        sa.column("role", sa.String),
        sa.column("permissions", postgresql.JSONB),
    )
    op.bulk_insert(rp, [
        {"role": "ADMIN", "permissions": _ALL},
        {"role": "MARKETER", "permissions": _MARKETER},
        {"role": "ANALYST", "permissions": _ANALYST},
    ])


def downgrade() -> None:
    op.drop_table("role_permissions")
