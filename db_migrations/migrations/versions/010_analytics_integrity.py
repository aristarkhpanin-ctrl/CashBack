"""analytics integrity (stage D — B33, B34)

Цифры аналитики и A/B считаются по данным, а не по соглашениям:

* ``ab_variants.is_control`` — контрольный вариант эксперимента задан явно
  (B34: раньше контролем считался первый по алфавиту, и знак эффекта
  переворачивался). Не больше одного контроля на эксперимент —
  ``uq_ab_variants_control``. Существующим экспериментам контроль ставится
  по порядку создания: первый вариант из ``ab_experiments.variants`` (тело
  ``POST /experiments`` в порядке запроса — колонки ``created_at`` у
  ``ab_variants`` нет); без него — вариант с именем ``control`` (так их пишет
  сидер), иначе — меньший ``variant_id``.
* ``uq_ab_events_conversion`` — конверсия назначения считается один раз
  (B34: дубли CONVERSION давали долю > 1 и 500 в z-тесте). Существующие
  дубли удаляются, остаётся самая ранняя конверсия.
* ``cashback_accruals.paid_at`` — когда начисление выплачено (PENDING → PAID
  ставит задача settlement campaign_manager, B33). Уже выплаченным —
  ``accrued_at``.
* индексы окон аналитики: ``recommendations (generated_at) INCLUDE
  (campaign_id, response_status, user_id)`` и ``cashback_accruals
  (accrued_at, campaign_id)``.

Блокировки — сразу и не дольше ``lock_timeout`` (как 009): ACCESS EXCLUSIVE
на cashback_accruals, ab_events, ab_variants (новые колонки, чистка дублей);
recommendations — только SHARE под индекс, чтение рекомендаций не ждёт.

Downgrade (ACCESS EXCLUSIVE на всех четырёх) возвращает схему 009; ``is_control`` и ``paid_at`` теряются,
удалённые дубли конверсий не возвращаются.

Revision ID: 010
Revises: 009
Create Date: 2026-10-18
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# От дочерних к родительским, как в 009; recommendations — последней и в
# SHARE: CREATE INDEX большего не требует.
LOCKS = (
    ("cashback_accruals, ab_events, ab_variants", "ACCESS EXCLUSIVE"),
    ("recommendations", "SHARE"),
)
LOCK_TIMEOUT = "5s"

# Контроль существующих экспериментов: порядок вариантов в теле создания,
# затем имя control, затем variant_id.
_BACKFILL_CONTROL_SQL = """
UPDATE ab_variants v
   SET is_control = true
  FROM (
        SELECT DISTINCT ON (av.experiment_id) av.variant_id
          FROM ab_variants av
          JOIN ab_experiments e ON e.experiment_id = av.experiment_id
          LEFT JOIN LATERAL (
                SELECT min(p.ord) AS ord
                  FROM jsonb_array_elements(
                           CASE WHEN jsonb_typeof(e.variants) = 'array'
                                THEN e.variants ELSE '[]'::jsonb END)
                       WITH ORDINALITY AS p(item, ord)
                 WHERE p.item ->> 'name' = av.name
          ) payload ON true
         ORDER BY av.experiment_id, payload.ord NULLS LAST,
                  (lower(av.name) = 'control') DESC, av.variant_id
       ) pick
 WHERE v.variant_id = pick.variant_id;
"""

# Дубли конверсий: остаётся самая ранняя (event_at, затем event_id).
_DEDUPE_CONVERSIONS_SQL = """
DELETE FROM ab_events e
 USING (
        SELECT event_id,
               row_number() OVER (PARTITION BY assignment_id
                                  ORDER BY event_at, event_id) AS n
          FROM ab_events
         WHERE event_type = 'CONVERSION'
       ) d
 WHERE e.event_id = d.event_id
   AND d.n > 1;
"""


def upgrade() -> None:
    # 0. все блокировки — сразу и не дольше lock_timeout
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}';")
    for tables, mode in LOCKS:
        op.execute(f"LOCK TABLE {tables} IN {mode} MODE;")

    # 1. явный контроль эксперимента, не больше одного
    op.add_column("ab_variants",
                  sa.Column("is_control", sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.execute(_BACKFILL_CONTROL_SQL)
    op.create_index("uq_ab_variants_control", "ab_variants", ["experiment_id"],
                    unique=True, postgresql_where=sa.text("is_control"))

    # 2. конверсия назначения — одна
    op.execute(_DEDUPE_CONVERSIONS_SQL)
    op.create_index("uq_ab_events_conversion", "ab_events", ["assignment_id"],
                    unique=True,
                    postgresql_where=sa.text("event_type = 'CONVERSION'"))

    # 3. момент выплаты начисления
    op.add_column("cashback_accruals",
                  sa.Column("paid_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.execute("UPDATE cashback_accruals SET paid_at = accrued_at "
               "WHERE status = 'PAID';")

    # 4. окна аналитики
    op.create_index("ix_recommendations_generated_window", "recommendations",
                    ["generated_at"],
                    postgresql_include=["campaign_id", "response_status", "user_id"])
    op.create_index("ix_accruals_accrued_campaign", "cashback_accruals",
                    ["accrued_at", "campaign_id"])


def downgrade() -> None:
    # DROP INDEX берёт ACCESS EXCLUSIVE и на recommendations — сразу его
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}';")
    op.execute("LOCK TABLE cashback_accruals, ab_events, ab_variants, recommendations "
               "IN ACCESS EXCLUSIVE MODE;")

    op.drop_index("ix_accruals_accrued_campaign", table_name="cashback_accruals")
    op.drop_index("ix_recommendations_generated_window", table_name="recommendations")
    op.drop_column("cashback_accruals", "paid_at")
    op.drop_index("uq_ab_events_conversion", table_name="ab_events")
    op.drop_index("uq_ab_variants_control", table_name="ab_variants")
    op.drop_column("ab_variants", "is_control")
