"""ML limits per segment bucket (phase 18 — business control over ML)

Бизнес задаёт ограничения ML-рекомендаций через админ-панель
(страница «ML-лимиты»); BRE применяет их как правило R7 (MlRateCap).
Ключ — сегментная корзина (см. campaign_manager/app/segments.py и
frontend adapters.ts), а не дециль: бизнес мыслит корзинами.

Отдельная строка ``__global__`` хранит глобальный выключатель ML —
одна таблица вместо отдельного kv-хранилища настроек.

Revision ID: 004
Revises: 003
Create Date: 2026-07-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Дефолты зеркалят DEFAULT_LIMITS фронтенда (MlLimits.tsx) — при первом
# запуске UI и BRE видят одинаковые значения.
_DEFAULTS = [
    # bucket,      min%, max%, daily_budget, auto_approve, risk
    ("premium",   3.0, 15.0, 200000, True,  "medium"),
    ("business",  3.0, 12.0, 150000, True,  "medium"),
    ("young",     2.0, 10.0, 100000, False, "high"),
    ("mass",      1.0,  7.0,  80000, False, "low"),
    ("senior",    2.0,  8.0,  60000, True,  "low"),
]


def upgrade() -> None:
    op.create_table(
        "ml_limits",
        sa.Column("segment_bucket", sa.String(32), primary_key=True),
        sa.Column("min_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("max_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("daily_budget", sa.Numeric(15, 2), nullable=False),
        sa.Column("auto_approve", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("risk_level", sa.String(16), nullable=False,
                  server_default="medium"),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("min_rate >= 0 AND max_rate <= 100 "
                           "AND min_rate <= max_rate",
                           name="ck_ml_limits_rates"),
    )

    for bucket, mn, mx, budget, auto, risk in _DEFAULTS:
        op.execute(
            sa.text(
                "INSERT INTO ml_limits (segment_bucket, min_rate, max_rate, "
                "daily_budget, auto_approve, risk_level) "
                "VALUES (:b, :mn, :mx, :db, :aa, :r) "
                "ON CONFLICT (segment_bucket) DO NOTHING"
            ).bindparams(b=bucket, mn=mn, mx=mx, db=budget, aa=auto, r=risk)
        )
    # Глобальный выключатель ML-рекомендаций (rate-поля не используются).
    op.execute(sa.text(
        "INSERT INTO ml_limits (segment_bucket, min_rate, max_rate, "
        "daily_budget, auto_approve, risk_level, enabled) "
        "VALUES ('__global__', 0, 100, 0, false, 'medium', true) "
        "ON CONFLICT (segment_bucket) DO NOTHING"
    ))


def downgrade() -> None:
    op.drop_table("ml_limits")
