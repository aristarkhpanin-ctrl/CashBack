"""009: колонки, одноразовость оффера и журнал начислений (B45, B24, §1.1–1.4)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from dbm_support import NOW

pytestmark = pytest.mark.integration

FOREIGN_KEY_VIOLATION = "23503"
UNIQUE_VIOLATION = "23505"
CHECK_VIOLATION = "23514"


# ── users.rfm_score ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("score", [None, 1, 3, 5])
def test_rfm_score_accepts_quintiles_and_unknown(pg, score):
    user_id = pg.user(rfm_score=score)
    assert pg.value("SELECT rfm_score FROM users WHERE user_id = %s", user_id) == score


@pytest.mark.parametrize("score", [0, 6, -1])
def test_rfm_score_outside_quintiles_is_rejected(pg, score):
    exc = pg.error("INSERT INTO users (external_id, rfm_score) VALUES ('bad', %s)", score)
    assert exc.pgcode == CHECK_VIOLATION
    assert exc.diag.constraint_name == "ck_users_rfm_score"


def test_rfm_score_defaults_to_unknown(pg):
    pg.rows("INSERT INTO users (external_id) VALUES ('no-score')")
    assert pg.value("SELECT rfm_score FROM users WHERE external_id = 'no-score'") is None


# ── cashback_campaigns.archived_at ───────────────────────────────────────────
def test_archived_at_is_nullable_timestamptz_defaulting_to_null(pg):
    campaign_id = pg.campaign()
    assert pg.value("SELECT archived_at FROM cashback_campaigns WHERE campaign_id = %s",
                    campaign_id) is None
    assert pg.rows("SELECT data_type, is_nullable FROM information_schema.columns "
                   "WHERE table_name = 'cashback_campaigns' "
                   "AND column_name = 'archived_at'") == [
        ("timestamp with time zone", "YES")]
    pg.rows("UPDATE cashback_campaigns SET archived_at = %s WHERE campaign_id = %s",
            NOW, campaign_id)
    assert pg.value("SELECT archived_at FROM cashback_campaigns WHERE campaign_id = %s",
                    campaign_id) == NOW


# ── cashback_accruals.recommendation_id: оффер платит один раз ───────────────
def test_accepted_offer_pays_at_most_once(pg):
    user_id = pg.user()
    campaign_id = pg.campaign()
    rec_id = pg.recommendation(user_id, campaign_id)
    pg.accrual(user_id, campaign_id, recommendation_id=rec_id)

    # вторая покупка по тому же офферу — другая транзакция, тот же оффер
    exc = pg.error(pg.ACCRUAL_SQL, user_id, campaign_id, "tx-second", 50, NOW, rec_id)
    assert exc.pgcode == UNIQUE_VIOLATION
    assert exc.diag.constraint_name == "uq_accrual_recommendation"
    assert pg.value("SELECT count(*) FROM cashback_accruals "
                    "WHERE recommendation_id = %s", rec_id) == 1


def test_duplicate_offer_is_a_conflict_for_on_conflict_do_nothing(pg):
    """Ветка ``duplicate`` слушателя: ON CONFLICT ловит и повтор оффера."""
    user_id = pg.user()
    campaign_id = pg.campaign()
    rec_id = pg.recommendation(user_id, campaign_id)
    pg.accrual(user_id, campaign_id, recommendation_id=rec_id)
    inserted = pg.rows(pg.ACCRUAL_SQL + " ON CONFLICT DO NOTHING RETURNING accrual_id",
                       user_id, campaign_id, "tx-redelivered", 50, NOW, rec_id)
    assert inserted == []


# ── покупка оплачивается один раз — по любому офферу ────────────────────────
def test_purchase_pays_at_most_once_across_campaigns(pg):
    """Повтор покупки из Kafka, когда ключ клиента×MCC держит уже оффер
    другой кампании: (transaction_id, campaign_id) различны, оффер новый —
    отвергает только UNIQUE по transaction_id."""
    user_id = pg.user()
    first_campaign, second_campaign = pg.campaign(), pg.campaign()
    first = pg.recommendation(user_id, first_campaign)
    second = pg.recommendation(user_id, second_campaign)
    pg.accrual(user_id, first_campaign, recommendation_id=first, transaction_id="tx-once")

    exc = pg.error(pg.ACCRUAL_SQL, user_id, second_campaign, "tx-once", 50, NOW, second)
    assert exc.pgcode == UNIQUE_VIOLATION
    assert exc.diag.constraint_name == "uq_accrual_transaction"
    # … и для ON CONFLICT DO NOTHING слушателя это тоже конфликт
    assert pg.rows(pg.ACCRUAL_SQL + " ON CONFLICT DO NOTHING RETURNING accrual_id",
                   user_id, second_campaign, "tx-once", 50, NOW, second) == []


def test_history_without_offers_may_repeat_a_purchase(pg):
    """Двойные выплаты старого кода (до 009, без оффера) — история: UNIQUE
    по покупке их не касается, миграция на такой БД не падает."""
    user_id = pg.user()
    first_campaign, second_campaign = pg.campaign(), pg.campaign()
    pg.accrual(user_id, first_campaign, transaction_id="tx-legacy")
    pg.accrual(user_id, second_campaign, transaction_id="tx-legacy")
    assert pg.value("SELECT count(*) FROM cashback_accruals "
                    "WHERE transaction_id = 'tx-legacy'") == 2
    assert pg.value("SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'uq_accrual_transaction'") == (
        "CREATE UNIQUE INDEX uq_accrual_transaction ON public.cashback_accruals "
        "USING btree (transaction_id) WHERE (recommendation_id IS NOT NULL)")


def test_different_offers_and_legacy_rows_coexist(pg):
    user_id = pg.user()
    campaign_id = pg.campaign()
    first = pg.recommendation(user_id, campaign_id)
    second = pg.recommendation(user_id, campaign_id)
    pg.accrual(user_id, campaign_id, recommendation_id=first)
    pg.accrual(user_id, campaign_id, recommendation_id=second)
    # старые / сидированные начисления без оффера — сколько угодно
    pg.accrual(user_id, campaign_id)
    pg.accrual(user_id, campaign_id)
    assert pg.value("SELECT count(*) FROM cashback_accruals WHERE campaign_id = %s",
                    campaign_id) == 4
    assert pg.value("SELECT count(*) FROM cashback_accruals WHERE campaign_id = %s "
                    "AND recommendation_id IS NULL", campaign_id) == 2


def test_accrual_must_reference_an_existing_recommendation(pg):
    user_id = pg.user()
    campaign_id = pg.campaign()
    exc = pg.error(pg.ACCRUAL_SQL, user_id, campaign_id, "tx-ghost", 50, NOW,
                   "00000000-0000-0000-0000-000000000001")
    assert exc.pgcode == FOREIGN_KEY_VIOLATION
    assert exc.diag.constraint_name == "cashback_accruals_recommendation_id_fkey"


def test_paid_recommendation_cannot_be_deleted(pg):
    user_id = pg.user()
    campaign_id = pg.campaign()
    paid = pg.recommendation(user_id, campaign_id)
    unpaid = pg.recommendation(user_id, campaign_id)
    pg.accrual(user_id, campaign_id, recommendation_id=paid)

    exc = pg.error("DELETE FROM recommendations WHERE recommendation_id = %s", paid)
    assert exc.pgcode == FOREIGN_KEY_VIOLATION
    pg.rows("DELETE FROM recommendations WHERE recommendation_id = %s", unpaid)
    assert pg.value("SELECT count(*) FROM recommendations WHERE campaign_id = %s",
                    campaign_id) == 1


def test_daily_spend_index_covers_campaign_and_time(pg):
    assert pg.value("SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_accruals_campaign_day'") == (
        "CREATE INDEX ix_accruals_campaign_day ON public.cashback_accruals "
        "USING btree (campaign_id, accrued_at)")


# ── удаление кампании не стирает историю (B24) ───────────────────────────────
def test_campaign_with_accruals_cannot_be_deleted_and_ledger_survives(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(budget_spent=100)
    rec_id = pg.recommendation(user_id, campaign_id)
    pg.accrual(user_id, campaign_id, cashback=50, recommendation_id=rec_id)
    pg.accrual(user_id, campaign_id, cashback=50)

    exc = pg.error("DELETE FROM cashback_campaigns WHERE campaign_id = %s", campaign_id)
    assert exc.pgcode == FOREIGN_KEY_VIOLATION
    assert pg.rows("SELECT count(*), sum(cashback_amount) FROM cashback_accruals "
                   "WHERE campaign_id = %s", campaign_id) == [(2, 100)]
    assert pg.value("SELECT count(*) FROM campaign_categories WHERE campaign_id = %s",
                    campaign_id) == 1


def test_campaign_with_recommendations_cannot_be_deleted(pg):
    user_id = pg.user()
    campaign_id = pg.campaign()
    pg.recommendation(user_id, campaign_id, status="PENDING")
    exc = pg.error("DELETE FROM cashback_campaigns WHERE campaign_id = %s", campaign_id)
    assert exc.pgcode == FOREIGN_KEY_VIOLATION
    assert exc.diag.constraint_name == "recommendations_campaign_id_fkey"


def test_campaign_without_history_is_deleted_with_its_categories(pg):
    campaign_id = pg.campaign(status="DRAFT", mccs={"5411": None, "5812": 300})
    pg.rows("DELETE FROM cashback_campaigns WHERE campaign_id = %s", campaign_id)
    assert pg.value("SELECT count(*) FROM campaign_categories WHERE campaign_id = %s",
                    campaign_id) == 0


def test_campaign_foreign_keys_delete_rules(pg):
    rules = dict(pg.rows(
        """
        SELECT conrelid::regclass::text || '.' || a.attname, c.confdeltype::text
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
         WHERE c.contype = 'f'
           AND c.confrelid IN ('cashback_campaigns'::regclass, 'users'::regclass,
                               'recommendations'::regclass)
        """))
    assert rules == {
        # r = RESTRICT, c = CASCADE
        "cashback_accruals.campaign_id": "r",
        "recommendations.campaign_id": "r",
        "campaign_categories.campaign_id": "c",
        "cashback_accruals.recommendation_id": "r",
        # FK на users не менялись
        "cashback_accruals.user_id": "c",
        "recommendations.user_id": "c",
        "user_consents.user_id": "c",
        "user_segments.user_id": "c",
    }


def test_deleting_a_user_still_cascades_offers_and_accruals(pg):
    """RESTRICT между начислением и оффером не мешает каскаду от клиента."""
    user_id = pg.user()
    campaign_id = pg.campaign()
    rec_id = pg.recommendation(user_id, campaign_id)
    pg.accrual(user_id, campaign_id, recommendation_id=rec_id)
    pg.accrual(user_id, campaign_id, accrued_at=NOW - timedelta(days=3))

    pg.rows("DELETE FROM users WHERE user_id = %s", user_id)
    assert pg.value("SELECT count(*) FROM recommendations WHERE user_id = %s", user_id) == 0
    assert pg.value("SELECT count(*) FROM cashback_accruals WHERE user_id = %s",
                    user_id) == 0
