"""model_version on recommendations (phase 18 — online model metrics)

Без версии модели в строке рекомендации онлайн-CTR нельзя атрибуцировать
конкретной модели: ``RecommendationResponse.model_version`` жил только в
ответе API и Kafka-событии. Колонка позволяет считать
``ml_online_ctr{model_version}`` (gauge campaign_manager) и сравнивать
качество версий на реальном трафике.

Revision ID: 005
Revises: 004
Create Date: 2026-07-11
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("model_version", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_recommendations_model_version",
        "recommendations",
        ["model_version", "generated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recommendations_model_version",
                  table_name="recommendations")
    op.drop_column("recommendations", "model_version")
