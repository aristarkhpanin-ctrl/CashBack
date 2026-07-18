"""campaign wizard fields (phase 22 — daily_limit, auto_pause, rfm, created_by)

5-шаговый визард собирает дневной лимит, тумблер авто-приостановки,
RFM-квантили и автора — но модель кампании их не хранила, и при сохранении в
live они терялись. Колонки:

* ``daily_limit``  — явный дневной порог расхода (раньше планировщик выводил
  cap как budget_total/days_left); backfill тем же выражением.
* ``auto_pause``   — гейт авто-приостановки (кампания с ``false`` не паузится).
* ``rfm_min/max``  — квантили RFM-фильтра аудитории.
* ``created_by``   — автор кампании (FK admin_users).

Revision ID: 006
Revises: 005
Create Date: 2026-07-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "cashback_campaigns",
        sa.Column("daily_limit", sa.Numeric(15, 2), nullable=True),
    )
    op.add_column(
        "cashback_campaigns",
        sa.Column("auto_pause", sa.Boolean(), server_default=sa.true(),
                  nullable=False),
    )
    op.add_column(
        "cashback_campaigns",
        sa.Column("rfm_min", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "cashback_campaigns",
        sa.Column("rfm_max", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "cashback_campaigns",
        sa.Column("created_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("admin_users.user_id", ondelete="SET NULL"),
                  nullable=True),
    )

    # Backfill daily_limit = budget_total / max(длительность в днях, 1) —
    # то же выражение, что раньше выводил планировщик из бюджета и дат.
    op.execute(
        """
        UPDATE cashback_campaigns
           SET daily_limit = ROUND(
                 budget_total
                 / GREATEST(
                     CEIL(EXTRACT(EPOCH FROM (end_date - start_date)) / 86400.0),
                     1
                   ), 2)
         WHERE daily_limit IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("cashback_campaigns", "created_by")
    op.drop_column("cashback_campaigns", "rfm_max")
    op.drop_column("cashback_campaigns", "rfm_min")
    op.drop_column("cashback_campaigns", "auto_pause")
    op.drop_column("cashback_campaigns", "daily_limit")
