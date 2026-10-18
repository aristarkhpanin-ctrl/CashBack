"""money integrity (stage C — B45, B24, B21, B22, B25, B26, B38)

Деньги считаются в Postgres, и правила «кому что положено» живут в одном
месте — здесь:

* ``users.rfm_score`` — RFM-квинтиль клиента (5 — лучший), ``NULL`` —
  неизвестен. Пишет сидер (позже — ETL).
* ``cashback_campaigns.archived_at`` — архив вместо удаления (B24): кампанию
  с историей нельзя удалить, её архивируют.
* ``cashback_accruals.recommendation_id`` (FK ``ON DELETE RESTRICT`` +
  ``UNIQUE``) — принятый оффер оплачивается не больше одного раза (B45).
  Старые начисления привязываются к своему офферу (последний ACCEPTED того
  же клиента, кампании и MCC, принятый до начисления; на оффер и на покупку —
  только самое раннее), остальные — ``NULL``. ``uq_accrual_transaction`` —
  покупка оплачивается не больше одного раза (по любому офферу); строки без
  оффера (история до 009) он не ограничивает. Индекс
  ``ix_accruals_campaign_day`` — дневной расход кампании (B22).
* FK начислений и рекомендаций на кампанию — ``ON DELETE RESTRICT``: удаление
  кампании больше не стирает журнал начислений (B24).
  ``campaign_categories`` остаётся ``CASCADE`` (чистая конфигурация), FK на
  ``users`` не меняются.
* триггер ``trg_campaign_status_guard`` — последняя линия обороны FSM (B38):
  переход статуса не из ``campaign_manager/app/fsm.py`` (в том числе любой
  выход из терминального COMPLETED) → SQLSTATE 23514.
* ``calculate_cashback(campaign, mcc, amount)`` вместо двухаргументной: MCC
  не из категорий кампании → 0, минимум — ``COALESCE`` минимума категории и
  кампании (B21). Шкала ``rate_tiers`` и ставка — как в 001.
* ``eligible_campaigns(user, mcc, amount, at)`` — ЕДИНСТВЕННОЕ определение
  «кому положена кампания» (B21, B22, B25, B26): его вызывают rec_api,
  transaction_listener и ``GET /campaigns/applicable``.

Порядок выката (2-аргументной ``calculate_cashback`` больше нет, а
transaction_listener до этапа C на ошибке начисления сдвигает offset — покупка
теряется): сначала новый transaction_listener (на схеме 008 он повторяет
начисления, offset не двигает), потом ``alembic upgrade`` 009, потом
campaign_manager и recommendation_api (им нужна 009). Миграция держит
ACCESS EXCLUSIVE на users, cashback_campaigns, recommendations,
cashback_accruals и user_consents до COMMIT (секунды на миллион строк) —
только в тихое окно; ждать эти блокировки дольше ``lock_timeout`` она не
станет.

Downgrade возвращает схему 008 (в том числе ``calculate_cashback(campaign,
amount)`` из 001); данные новых колонок теряются.

Revision ID: 009
Revises: 008
Create Date: 2026-10-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Разрешённые переходы статуса — зеркало TRANSITIONS из
# services/campaign_manager/app/fsm.py (db_migrations/tests сверяют их).
# DRAFT → COMPLETED в FSM нет: черновик без истории удаляют (DELETE).
FSM_TRANSITIONS = (
    ("DRAFT", "ACTIVE"),
    ("ACTIVE", "PAUSED"),
    ("ACTIVE", "COMPLETED"),
    ("PAUSED", "ACTIVE"),
    ("PAUSED", "COMPLETED"),
)

# Таблицы, которые 009 меняет, — блокируются сразу, одним LOCK TABLE: от
# дочерних к родительским, как их берут сервисы (INSERT строки, затем FK-
# проверки родителей). Иначе миграция держала бы ACCESS EXCLUSIVE на users,
# ожидая recommendations, а INSERT рекомендации — наоборот: deadlock.
LOCKED_TABLES = ("cashback_accruals", "recommendations", "user_consents",
                 "cashback_campaigns", "users")
LOCK_TIMEOUT = "5s"

# Имена FK, которые Postgres дал в 001 (create_table без имени констрейнта).
ACCRUALS_CAMPAIGN_FK = "cashback_accruals_campaign_id_fkey"
RECOMMENDATIONS_CAMPAIGN_FK = "recommendations_campaign_id_fkey"
ACCRUALS_RECOMMENDATION_FK = "cashback_accruals_recommendation_id_fkey"


def _campaign_fk(name: str, table: str, ondelete: str) -> None:
    op.drop_constraint(name, table, type_="foreignkey")
    op.create_foreign_key(name, table, "cashback_campaigns",
                          ["campaign_id"], ["campaign_id"], ondelete=ondelete)


_STATUS_GUARD_SQL = """
CREATE FUNCTION campaign_status_guard() RETURNS trigger AS $$
BEGIN
    -- UPDATE без смены статуса (status = status) — не переход.
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;
    -- Из COMPLETED переходов нет: он терминален.
    IF (OLD.status::text, NEW.status::text) NOT IN ({transitions}) THEN
        RAISE EXCEPTION 'campaign status transition % -> % is not allowed',
                        OLD.status, NEW.status
              USING ERRCODE = '23514',
                    CONSTRAINT = 'trg_campaign_status_guard';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""".format(transitions=", ".join(f"('{old}', '{new}')" for old, new in FSM_TRANSITIONS))


# Шкала rate_tiers — как в 001; новое: MCC обязан быть категорией кампании,
# минимум — минимум категории, а если он не задан — минимум кампании (B21).
_CALCULATE_CASHBACK_SQL = """
CREATE FUNCTION calculate_cashback(
    p_campaign_id UUID,
    p_mcc TEXT,
    p_amount NUMERIC
) RETURNS NUMERIC AS $$
DECLARE
    v_tiers          JSONB;
    v_default_rate   NUMERIC;
    v_min            NUMERIC;
    v_in_category    BOOLEAN;
    v_cashback       NUMERIC := 0;
    v_tier           RECORD;
    v_segment        NUMERIC;
BEGIN
    SELECT c.rate_tiers, c.cashback_rate,
           COALESCE(cc.min_transaction_amount, c.min_transaction_amount),
           cc.campaign_id IS NOT NULL
      INTO v_tiers, v_default_rate, v_min, v_in_category
      FROM cashback_campaigns c
      LEFT JOIN campaign_categories cc
             ON cc.campaign_id = c.campaign_id
            AND cc.mcc_code = p_mcc::bpchar
     WHERE c.campaign_id = p_campaign_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'campaign % not found', p_campaign_id;
    END IF;

    IF NOT v_in_category THEN
        RETURN 0;
    END IF;

    IF v_min IS NOT NULL AND p_amount < v_min THEN
        RETURN 0;
    END IF;

    IF v_tiers IS NULL OR jsonb_typeof(v_tiers) <> 'array'
       OR jsonb_array_length(v_tiers) = 0 THEN
        RETURN ROUND(p_amount * v_default_rate / 100, 2);
    END IF;

    FOR v_tier IN
        SELECT
            COALESCE((value->>'min_amount')::NUMERIC, 0) AS min_amt,
            NULLIF(value->>'max_amount', '')::NUMERIC    AS max_amt,
            (value->>'rate')::NUMERIC                    AS rate
          FROM jsonb_array_elements(v_tiers)
         ORDER BY 1
    LOOP
        IF p_amount <= v_tier.min_amt THEN
            EXIT;
        END IF;
        v_segment := LEAST(p_amount, COALESCE(v_tier.max_amt, p_amount))
                     - v_tier.min_amt;
        IF v_segment > 0 THEN
            v_cashback := v_cashback + v_segment * v_tier.rate / 100;
        END IF;
    END LOOP;

    RETURN ROUND(v_cashback, 2);
END;
$$ LANGUAGE plpgsql STABLE;
"""

# eligible_campaigns — одна строка на кампанию, которая положена клиенту
# p_user_id за покупку p_amount (NULL — сумма ещё неизвестна: оффер) с MCC
# p_mcc в момент p_at. Все условия — в WHERE; ни одно не «открыто при сбое»:
# нет клиента / согласия / RFM-оценки при RFM-фильтре — нет строк.
# spent_today — начисления кампании с полуночи UTC дня p_at, но не позже
# сегодняшней: начисление записывается сегодня (accrued_at = now()), и
# покупка с датой завтрашнего дня (часы продюсера) тоже видит сегодняшний
# расход. Верхней границы нет: позднее начисление за вчера учитывает и
# сегодняшние.
# Однооператорная STABLE SQL-функция встраивается планировщиком в
# вызывающий запрос (в том числе LATERAL по UNNEST в rec_api), если
# аргументы — параметры или столбцы, а не подзапросы.
_ELIGIBLE_CAMPAIGNS_SQL = """
CREATE FUNCTION eligible_campaigns(
    p_user_id UUID,
    p_mcc TEXT,
    p_amount NUMERIC,
    p_at TIMESTAMPTZ
) RETURNS TABLE (
    campaign_id            UUID,
    mcc_code               TEXT,
    cashback_rate          NUMERIC,
    rate_tiers             JSONB,
    min_transaction_amount NUMERIC,
    allowed_channels       TEXT[],
    budget_total           NUMERIC,
    budget_spent           NUMERIC,
    daily_limit            NUMERIC,
    spent_today            NUMERIC,
    start_date             TIMESTAMPTZ,
    end_date               TIMESTAMPTZ,
    name                   TEXT
) AS $$
    SELECT c.campaign_id,
           cc.mcc_code::text,
           c.cashback_rate::numeric,
           c.rate_tiers,
           COALESCE(cc.min_transaction_amount, c.min_transaction_amount)::numeric,
           c.allowed_channels,
           c.budget_total::numeric,
           c.budget_spent::numeric,
           c.daily_limit::numeric,
           spent.spent_today,
           c.start_date,
           c.end_date,
           c.name::text
      FROM users u
      JOIN campaign_categories cc
        ON cc.mcc_code = p_mcc::bpchar
      JOIN cashback_campaigns c
        ON c.campaign_id = cc.campaign_id
      -- последнее согласие клиента; нет записи — нет строки (не положено)
      CROSS JOIN LATERAL (
            SELECT uc.status
              FROM user_consents uc
             WHERE uc.user_id = u.user_id
               AND uc.consent_type = 'personalised_cashback'
             ORDER BY uc.created_at DESC, uc.consent_id DESC
             LIMIT 1
      ) consent
      CROSS JOIN LATERAL (
            SELECT COALESCE(sum(a.cashback_amount), 0)::numeric AS spent_today
              FROM cashback_accruals a
             WHERE a.campaign_id = c.campaign_id
               AND a.accrued_at >= date_trunc('day', LEAST(p_at, now())
                                                     AT TIME ZONE 'UTC')
                                   AT TIME ZONE 'UTC'
      ) spent
     WHERE u.user_id = p_user_id
       AND c.status = 'ACTIVE'
       AND c.archived_at IS NULL
       AND p_at >= c.start_date
       AND p_at <  c.end_date
       AND (p_amount IS NULL
            OR p_amount >= COALESCE(cc.min_transaction_amount,
                                    c.min_transaction_amount, 0))
       AND (c.target_segment_ids IS NULL
            OR cardinality(c.target_segment_ids) = 0
            OR u.segment_id = ANY(c.target_segment_ids))
       AND consent.status = 'GRANTED'
       AND ((c.rfm_min IS NULL AND c.rfm_max IS NULL)
            OR (u.rfm_score IS NOT NULL
                AND (c.rfm_min IS NULL OR u.rfm_score >= c.rfm_min)
                AND (c.rfm_max IS NULL OR u.rfm_score <= c.rfm_max)))
       AND c.budget_total - c.budget_spent > 0
       AND (c.daily_limit IS NULL OR spent.spent_today < c.daily_limit)
     ORDER BY c.cashback_rate DESC, c.start_date DESC, c.campaign_id
$$ LANGUAGE sql STABLE;
"""

# calculate_cashback(p_campaign_id, p_amount) из 001 — для downgrade
# (миграции не импортируют друг друга: каждая — снимок на момент написания).
_CALCULATE_CASHBACK_V1_SQL = """
        CREATE OR REPLACE FUNCTION calculate_cashback(
            p_campaign_id UUID,
            p_amount NUMERIC
        ) RETURNS NUMERIC AS $$
        DECLARE
            v_tiers          JSONB;
            v_default_rate   NUMERIC;
            v_min            NUMERIC;
            v_status         campaign_status;
            v_cashback       NUMERIC := 0;
            v_tier           RECORD;
            v_segment        NUMERIC;
        BEGIN
            SELECT rate_tiers, cashback_rate, min_transaction_amount, status
              INTO v_tiers, v_default_rate, v_min, v_status
              FROM cashback_campaigns
             WHERE campaign_id = p_campaign_id;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'campaign % not found', p_campaign_id;
            END IF;

            IF v_min IS NOT NULL AND p_amount < v_min THEN
                RETURN 0;
            END IF;

            IF v_tiers IS NULL OR jsonb_typeof(v_tiers) <> 'array'
               OR jsonb_array_length(v_tiers) = 0 THEN
                RETURN ROUND(p_amount * v_default_rate / 100, 2);
            END IF;

            FOR v_tier IN
                SELECT
                    COALESCE((value->>'min_amount')::NUMERIC, 0) AS min_amt,
                    NULLIF(value->>'max_amount', '')::NUMERIC    AS max_amt,
                    (value->>'rate')::NUMERIC                    AS rate
                  FROM jsonb_array_elements(v_tiers)
                 ORDER BY 1
            LOOP
                IF p_amount <= v_tier.min_amt THEN
                    EXIT;
                END IF;
                v_segment := LEAST(p_amount, COALESCE(v_tier.max_amt, p_amount))
                             - v_tier.min_amt;
                IF v_segment > 0 THEN
                    v_cashback := v_cashback + v_segment * v_tier.rate / 100;
                END IF;
            END LOOP;

            RETURN ROUND(v_cashback, 2);
        END;
        $$ LANGUAGE plpgsql STABLE;
        """


# Старые начисления (до 009) — к офферу, который они оплатили: последний
# ACCEPTED того же клиента, кампании и MCC, принятый не позже начисления
# (его держал ключ accepted_offers, когда старый listener платил). Без этого
# новый движок не видит, что оффер уже оплачен, и платит его второй раз,
# если ключ пережил DEL (B45). На покупку и на оффер — только самое раннее
# начисление: UNIQUE обоих ключей; остальные (двойные выплаты старого
# кода) остаются без оффера.
_BACKFILL_RECOMMENDATION_SQL = """
WITH linked AS (
    SELECT DISTINCT ON (a.transaction_id)
           a.accrual_id, a.accrued_at, offer.recommendation_id
      FROM cashback_accruals a
      CROSS JOIN LATERAL (
            SELECT r.recommendation_id
              FROM recommendations r
             WHERE r.user_id = a.user_id
               AND r.campaign_id = a.campaign_id
               AND r.mcc_code = a.mcc_code
               AND r.response_status = 'ACCEPTED'
               AND r.responded_at <= a.accrued_at
             ORDER BY r.responded_at DESC, r.recommendation_id DESC
             LIMIT 1
      ) offer
     ORDER BY a.transaction_id, a.accrued_at, a.accrual_id
), first_per_offer AS (
    SELECT DISTINCT ON (recommendation_id) accrual_id, recommendation_id
      FROM linked
     ORDER BY recommendation_id, accrued_at, accrual_id
)
UPDATE cashback_accruals a
   SET recommendation_id = f.recommendation_id
  FROM first_per_offer f
 WHERE a.accrual_id = f.accrual_id;
"""


def upgrade() -> None:
    # 0. все блокировки — сразу и не дольше lock_timeout (SET LOCAL — до
    #    конца транзакции alembic)
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}';")
    op.execute(f"LOCK TABLE {', '.join(LOCKED_TABLES)} IN ACCESS EXCLUSIVE MODE;")

    # 1. RFM-квинтиль клиента
    op.add_column("users", sa.Column("rfm_score", sa.SmallInteger(), nullable=True))
    op.create_check_constraint("ck_users_rfm_score", "users",
                               "rfm_score BETWEEN 1 AND 5")

    # 2. архив кампаний
    op.add_column("cashback_campaigns",
                  sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True))

    # 3. оффер и покупка оплачиваются один раз; дневной расход кампании
    op.add_column("cashback_accruals",
                  sa.Column("recommendation_id", postgresql.UUID(as_uuid=True),
                            nullable=True))
    op.execute(_BACKFILL_RECOMMENDATION_SQL)
    op.create_foreign_key(ACCRUALS_RECOMMENDATION_FK, "cashback_accruals",
                          "recommendations", ["recommendation_id"],
                          ["recommendation_id"], ondelete="RESTRICT")
    op.create_unique_constraint("uq_accrual_recommendation", "cashback_accruals",
                                ["recommendation_id"])
    op.create_index("uq_accrual_transaction", "cashback_accruals",
                    ["transaction_id"], unique=True,
                    postgresql_where=sa.text("recommendation_id IS NOT NULL"))
    op.create_index("ix_accruals_campaign_day", "cashback_accruals",
                    ["campaign_id", "accrued_at"])

    # 4. удаление кампании не стирает журнал начислений и рекомендаций
    _campaign_fk(ACCRUALS_CAMPAIGN_FK, "cashback_accruals", "RESTRICT")
    _campaign_fk(RECOMMENDATIONS_CAMPAIGN_FK, "recommendations", "RESTRICT")

    # 5. FSM статусов — последняя линия обороны
    op.execute(_STATUS_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER trg_campaign_status_guard "
        "BEFORE UPDATE OF status ON cashback_campaigns "
        "FOR EACH ROW EXECUTE FUNCTION campaign_status_guard();"
    )

    # 6. кэшбэк с учётом MCC и минимума категории
    op.execute("DROP FUNCTION IF EXISTS calculate_cashback(UUID, NUMERIC);")
    op.execute(_CALCULATE_CASHBACK_SQL)

    # 7. единая функция eligibility
    op.execute(_ELIGIBLE_CAMPAIGNS_SQL)

    # 8. последнее согласие клиента — один шаг по индексу
    op.execute(
        "CREATE INDEX ix_user_consents_latest "
        "ON user_consents (user_id, consent_type, created_at DESC, consent_id DESC);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_user_consents_latest;")

    op.execute("DROP FUNCTION IF EXISTS "
               "eligible_campaigns(UUID, TEXT, NUMERIC, TIMESTAMPTZ);")

    op.execute("DROP FUNCTION IF EXISTS calculate_cashback(UUID, TEXT, NUMERIC);")
    op.execute(_CALCULATE_CASHBACK_V1_SQL)

    op.execute("DROP TRIGGER IF EXISTS trg_campaign_status_guard ON cashback_campaigns;")
    op.execute("DROP FUNCTION IF EXISTS campaign_status_guard();")

    _campaign_fk(RECOMMENDATIONS_CAMPAIGN_FK, "recommendations", "CASCADE")
    _campaign_fk(ACCRUALS_CAMPAIGN_FK, "cashback_accruals", "CASCADE")

    op.drop_index("ix_accruals_campaign_day", table_name="cashback_accruals")
    op.drop_index("uq_accrual_transaction", table_name="cashback_accruals")
    op.drop_constraint("uq_accrual_recommendation", "cashback_accruals", type_="unique")
    op.drop_constraint(ACCRUALS_RECOMMENDATION_FK, "cashback_accruals",
                       type_="foreignkey")
    op.drop_column("cashback_accruals", "recommendation_id")

    op.drop_column("cashback_campaigns", "archived_at")

    op.drop_constraint("ck_users_rfm_score", "users", type_="check")
    op.drop_column("users", "rfm_score")
