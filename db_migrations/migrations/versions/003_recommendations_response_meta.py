"""response metadata for recommendations (phase 16 — analytics)

Adds ``responded_at`` (когда клиент отреагировал — база для daily-trend и
дальнейшего OST) and ``channel`` (канал доставки предложения — база для
канальной аналитики). Без них динамику приходилось считать по generated_at,
а канал не сохранялся вовсе.

Revision ID: 003
Revises: 002
Create Date: 2026-07-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Каналы доставки предложения — зеркалят адаптеры NotificationPipeline
# (transaction_listener/app/notification): Push / SMS / Email / In-App.
DELIVERY_CHANNEL_VALUES = ("PUSH", "SMS", "EMAIL", "IN_APP")


def upgrade() -> None:
    channel_enum = postgresql.ENUM(*DELIVERY_CHANNEL_VALUES, name="delivery_channel")
    channel_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "recommendations",
        sa.Column("responded_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "recommendations",
        sa.Column(
            "channel",
            postgresql.ENUM(*DELIVERY_CHANNEL_VALUES, name="delivery_channel",
                            create_type=False),
            nullable=True,
        ),
    )
    # Частичный индекс: daily-trend агрегирует только принятые отклики.
    op.create_index(
        "ix_recommendations_responded_accepted",
        "recommendations",
        ["responded_at"],
        postgresql_where=sa.text("response_status = 'ACCEPTED'"),
    )


def downgrade() -> None:
    op.drop_index("ix_recommendations_responded_accepted",
                  table_name="recommendations")
    op.drop_column("recommendations", "channel")
    op.drop_column("recommendations", "responded_at")
    postgresql.ENUM(name="delivery_channel").drop(op.get_bind(), checkfirst=True)
