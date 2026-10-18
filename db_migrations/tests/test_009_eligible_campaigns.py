"""009: ``eligible_campaigns(user, mcc, amount, at)`` — кому положена кампания.

Единственное определение eligibility для rec_api, transaction_listener и
``/campaigns/applicable`` (B21, B22, B25, B26, §1.7). Каждое правило —
положительный и отрицательный случай; всё остальное в фабриках разрешено.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from dbm_support import NOW

pytestmark = pytest.mark.integration

COLUMNS = ["campaign_id", "mcc_code", "cashback_rate", "rate_tiers",
           "min_transaction_amount", "allowed_channels", "budget_total",
           "budget_spent", "daily_limit", "spent_today", "start_date", "end_date",
           "name"]


def _eligible(pg, user_id, mcc="5411", amount=None, at=NOW):
    with pg.conn.cursor() as cur:
        cur.execute("SELECT * FROM eligible_campaigns(%s, %s, %s, %s)",
                    (user_id, mcc, amount, at))
        names = [d.name for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def _ids(pg, user_id, mcc="5411", amount=None, at=NOW):
    return [str(row["campaign_id"]) for row in _eligible(pg, user_id, mcc, amount, at)]


# ── форма результата ─────────────────────────────────────────────────────────
def test_row_carries_what_offers_and_accruals_need(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(rate=7.5, min_amount=100, mccs={"5411": 250},
                              budget_total=5000, budget_spent=1200, daily_limit=900,
                              tiers=[{"min_amount": 0, "max_amount": None, "rate": 7.5}],
                              name="Продукты")
    pg.accrual(user_id, campaign_id, cashback=40)
    (row,) = _eligible(pg, user_id)
    assert list(row) == COLUMNS
    assert row == {
        "campaign_id": row["campaign_id"],
        "mcc_code": "5411",
        "cashback_rate": Decimal("7.50"),
        "rate_tiers": [{"min_amount": 0, "max_amount": None, "rate": 7.5}],
        "min_transaction_amount": Decimal("250.00"),     # минимум категории
        "allowed_channels": ["PUSH", "IN_APP"],
        "budget_total": Decimal("5000.00"),
        "budget_spent": Decimal("1200.00"),
        "daily_limit": Decimal("900.00"),
        "spent_today": Decimal("40.00"),
        "start_date": NOW - timedelta(days=30),
        "end_date": NOW + timedelta(days=30),
        "name": "Продукты",
    }
    assert str(row["campaign_id"]) == campaign_id


def test_effective_minimum_falls_back_to_campaign_minimum(pg):
    user_id = pg.user()
    pg.campaign(min_amount=300, mccs={"5411": None})
    (row,) = _eligible(pg, user_id)
    assert row["min_transaction_amount"] == Decimal("300.00")


def test_effective_minimum_is_null_without_any_minimum(pg):
    user_id = pg.user()
    pg.campaign(min_amount=None, mccs={"5411": None})
    (row,) = _eligible(pg, user_id)
    assert row["min_transaction_amount"] is None and row["spent_today"] == 0


def test_function_is_stable_sql(pg):
    assert pg.rows("SELECT p.provolatile::text, l.lanname FROM pg_proc p "
                   "JOIN pg_language l ON l.oid = p.prolang "
                   "WHERE p.proname = 'eligible_campaigns'") == [("s", "sql")]


# ── статус и архив ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("status", "eligible"), [
    ("ACTIVE", True), ("DRAFT", False), ("PAUSED", False), ("COMPLETED", False),
])
def test_only_active_campaigns(pg, status, eligible):
    user_id = pg.user()
    campaign_id = pg.campaign(status=status)
    assert _ids(pg, user_id) == ([campaign_id] if eligible else [])


def test_archived_campaign_is_not_eligible(pg):
    user_id = pg.user()
    live = pg.campaign()
    pg.campaign(archived_at=NOW - timedelta(days=1))
    assert _ids(pg, user_id) == [live]


# ── окно дат (B25) ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(("at", "eligible"), [
    (NOW - timedelta(seconds=1), False),          # до start_date
    (NOW, True),                                  # ровно start_date
    (NOW + timedelta(days=1), True),
    (NOW + timedelta(days=2) - timedelta(microseconds=1), True),
    (NOW + timedelta(days=2), False),             # ровно end_date — уже нет
    (NOW + timedelta(days=3), False),
])
def test_date_window_is_start_inclusive_end_exclusive(pg, at, eligible):
    user_id = pg.user()
    campaign_id = pg.campaign(start=NOW, end=NOW + timedelta(days=2))
    assert _ids(pg, user_id, at=at) == ([campaign_id] if eligible else [])


def test_null_moment_is_not_eligible(pg):
    user_id = pg.user()
    pg.campaign()
    assert _ids(pg, user_id, at=None) == []


# ── MCC ──────────────────────────────────────────────────────────────────────
def test_mcc_must_be_a_campaign_category(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(mccs={"5411": None, "5812": 300})
    (row,) = _eligible(pg, user_id, mcc="5812")
    assert str(row["campaign_id"]) == campaign_id and row["mcc_code"] == "5812"
    assert row["min_transaction_amount"] == Decimal("300.00")
    assert _ids(pg, user_id, mcc="5541") == []
    assert _ids(pg, user_id, mcc="54110") == []
    assert _ids(pg, user_id, mcc=None) == []


# ── минимум по категории (B21) ───────────────────────────────────────────────
@pytest.mark.parametrize(("amount", "eligible"), [
    (None, True),                 # оффер: сумма покупки ещё неизвестна
    (Decimal("499.99"), False),
    (Decimal("500"), True),
    (Decimal("10000"), True),
])
def test_category_minimum(pg, amount, eligible):
    user_id = pg.user()
    campaign_id = pg.campaign(min_amount=100, mccs={"5411": 500})
    assert _ids(pg, user_id, amount=amount) == ([campaign_id] if eligible else [])


def test_category_minimum_below_campaign_minimum_wins(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(min_amount=1000, mccs={"5411": 100})
    assert _ids(pg, user_id, amount=Decimal("100")) == [campaign_id]
    assert _ids(pg, user_id, amount=Decimal("99.99")) == []


def test_campaign_minimum_when_category_has_none(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(min_amount=300, mccs={"5411": None})
    assert _ids(pg, user_id, amount=Decimal("300")) == [campaign_id]
    assert _ids(pg, user_id, amount=Decimal("299.99")) == []


def test_without_minimums_any_positive_amount_qualifies(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(min_amount=None, mccs={"5411": None})
    assert _ids(pg, user_id, amount=Decimal("0.01")) == [campaign_id]


# ── целевые сегменты ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(("segments", "user_segment", "eligible"), [
    ([], 3, True),                # без таргетинга — всем
    ([], None, True),
    ([1, 3, 5], 3, True),
    ([1, 5], 3, False),
    ([1, 3], None, False),        # сегмент клиента неизвестен — не положено
])
def test_target_segments(pg, segments, user_segment, eligible):
    user_id = pg.user(segment_id=user_segment)
    campaign_id = pg.campaign(segments=segments)
    assert _ids(pg, user_id) == ([campaign_id] if eligible else [])


# ── согласие: решает ПОСЛЕДНЯЯ запись (B26) ──────────────────────────────────
def _consents(pg, *statuses):
    """Клиент с историей согласий: каждое следующее — на день позже."""
    user_id = pg.user(consent=None)
    for days, status in enumerate(statuses):
        pg.consent(user_id, status, NOW - timedelta(days=30 - days))
    return user_id


@pytest.mark.parametrize(("history", "eligible"), [
    ((), False),                                   # нет записи
    (("GRANTED",), True),
    (("GRANTED", "REVOKED"), False),               # отзыв после согласия
    (("REVOKED", "GRANTED"), True),                # повторное согласие
    (("GRANTED", "REVOKED", "GRANTED", "REVOKED"), False),
    (("GRANTED", "PENDING"), False),
    (("PENDING",), False),
])
def test_latest_consent_decides(pg, history, eligible):
    campaign_id = pg.campaign()
    user_id = _consents(pg, *history)
    assert _ids(pg, user_id) == ([campaign_id] if eligible else [])


def test_latest_consent_is_by_created_at_not_insert_order(pg):
    campaign_id = pg.campaign()
    user_id = pg.user(consent=None)
    pg.consent(user_id, "GRANTED", NOW - timedelta(days=1))
    pg.consent(user_id, "REVOKED", NOW - timedelta(days=5))     # вставлено позже, но старше
    assert _ids(pg, user_id) == [campaign_id]


@pytest.mark.parametrize(("granted_id", "revoked_id", "eligible"), [
    ("ffffffff-0000-0000-0000-000000000000", "00000000-0000-0000-0000-00000000000f", True),
    ("00000000-0000-0000-0000-00000000000f", "ffffffff-0000-0000-0000-000000000000", False),
])
def test_same_created_at_tie_is_broken_by_consent_id(pg, granted_id, revoked_id,
                                                     eligible):
    campaign_id = pg.campaign()
    user_id = pg.user(consent=None)
    at = NOW - timedelta(days=1)
    pg.consent(user_id, "GRANTED", at, consent_id=granted_id)
    pg.consent(user_id, "REVOKED", at, consent_id=revoked_id)
    assert _ids(pg, user_id) == ([campaign_id] if eligible else [])


def test_other_consent_types_do_not_count(pg):
    campaign_id = pg.campaign()
    user_id = pg.user(consent=None)
    pg.consent(user_id, "GRANTED", NOW - timedelta(days=1), consent_type="marketing_sms")
    assert _ids(pg, user_id) == []
    pg.consent(user_id, "GRANTED", NOW - timedelta(days=2))
    pg.consent(user_id, "REVOKED", NOW - timedelta(hours=1), consent_type="marketing_sms")
    assert _ids(pg, user_id) == [campaign_id]


def test_latest_consent_lookup_uses_its_index(pg):
    assert pg.value("SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'ix_user_consents_latest'") == (
        "CREATE INDEX ix_user_consents_latest ON public.user_consents USING btree "
        "(user_id, consent_type, created_at DESC, consent_id DESC)")


# ── RFM (fail-closed) ────────────────────────────────────────────────────────
@pytest.mark.parametrize(("rfm_min", "rfm_max", "score", "eligible"), [
    (None, None, None, True),     # фильтра нет — оценка не нужна
    (None, None, 1, True),
    (2, 4, None, False),          # фильтр есть, оценка неизвестна — не положено
    (2, 4, 1, False),
    (2, 4, 2, True),              # границы включительно
    (2, 4, 3, True),
    (2, 4, 4, True),
    (2, 4, 5, False),
    (4, None, 3, False),          # только нижняя граница
    (4, None, 5, True),
    (4, None, None, False),
    (None, 2, 3, False),          # только верхняя граница
    (None, 2, 1, True),
    (None, 2, None, False),
])
def test_rfm_range(pg, rfm_min, rfm_max, score, eligible):
    user_id = pg.user(rfm_score=score)
    campaign_id = pg.campaign(rfm_min=rfm_min, rfm_max=rfm_max)
    assert _ids(pg, user_id) == ([campaign_id] if eligible else [])


# ── бюджет ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("spent", "eligible"), [
    (0, True), (999.99, True), (1000, False),
])
def test_remaining_budget_must_be_positive(pg, spent, eligible):
    user_id = pg.user()
    campaign_id = pg.campaign(budget_total=1000, budget_spent=spent)
    assert _ids(pg, user_id) == ([campaign_id] if eligible else [])


# ── дневной лимит (B22) ──────────────────────────────────────────────────────
@pytest.mark.parametrize(("spent_today", "eligible"), [
    ((), True),
    ((100, 199.99), True),
    ((100, 200), False),          # ровно лимит — сегодня больше не платим
    ((350,), False),
])
def test_daily_limit(pg, spent_today, eligible):
    user_id = pg.user()
    campaign_id = pg.campaign(daily_limit=300)
    for cashback in spent_today:
        pg.accrual(user_id, campaign_id, cashback=cashback,
                   accrued_at=NOW - timedelta(hours=1))
    rows = _eligible(pg, user_id)
    assert [str(r["campaign_id"]) for r in rows] == ([campaign_id] if eligible else [])
    if eligible:
        assert rows[0]["spent_today"] == sum(Decimal(str(c)) for c in spent_today)


def test_without_daily_limit_spending_today_does_not_matter(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(daily_limit=None)
    pg.accrual(user_id, campaign_id, cashback=5000, accrued_at=NOW)
    (row,) = _eligible(pg, user_id)
    assert row["spent_today"] == Decimal("5000.00")


def test_spent_today_counts_from_utc_midnight_of_the_moment(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(daily_limit=300)
    midnight = NOW.replace(hour=0)
    pg.accrual(user_id, campaign_id, cashback=290, accrued_at=midnight - timedelta(seconds=1))
    pg.accrual(user_id, campaign_id, cashback=10, accrued_at=midnight)
    (row,) = _eligible(pg, user_id)
    assert row["spent_today"] == Decimal("10.00")
    # тот же момент «вчера»: вчерашние 290 + сегодняшние 10 = лимит
    assert _ids(pg, user_id, at=midnight - timedelta(hours=1)) == []


def test_moment_after_today_still_counts_todays_spending(pg):
    """Покупка с датой завтрашнего дня (часы продюсера) начисляется сегодня
    (accrued_at = now()): лимит дня, исчерпанный сегодня, не обнуляется."""
    now = pg.value("SELECT now()")
    user_id = pg.user()
    campaign_id = pg.campaign(daily_limit=300, start=now - timedelta(days=1),
                              end=now + timedelta(days=5))
    pg.accrual(user_id, campaign_id, cashback=300, accrued_at=now)
    tomorrow = now + timedelta(days=1)
    assert _ids(pg, user_id, at=tomorrow) == []
    pg.rows("UPDATE cashback_campaigns SET daily_limit = 400 WHERE campaign_id = %s",
            campaign_id)
    (row,) = _eligible(pg, user_id, at=tomorrow)
    assert row["spent_today"] == Decimal("300.00")


def test_utc_day_does_not_depend_on_session_time_zone(pg):
    user_id = pg.user()
    campaign_id = pg.campaign(daily_limit=300)
    # 23:30 UTC 16-го = 02:30 МСК 17-го: по Москве это «сегодня», по UTC — вчера
    pg.accrual(user_id, campaign_id, cashback=300,
               accrued_at=NOW.replace(hour=0) - timedelta(minutes=30))
    pg.rows("SET LOCAL TIME ZONE 'Europe/Moscow'")
    (row,) = _eligible(pg, user_id)
    assert row["spent_today"] == 0


def test_spent_today_is_per_campaign(pg):
    user_id = pg.user()
    capped = pg.campaign(daily_limit=100, rate=9)
    other = pg.campaign(daily_limit=100, rate=1)
    pg.accrual(user_id, other, cashback=100, accrued_at=NOW)
    rows = _eligible(pg, user_id)
    assert [(str(r["campaign_id"]), r["spent_today"]) for r in rows] == [(capped, 0)]


# ── порядок и неизвестный клиент ─────────────────────────────────────────────
def test_ordered_by_rate_then_newest_start(pg):
    user_id = pg.user()
    low = pg.campaign(rate=3)
    high_old = pg.campaign(rate=7, start=NOW - timedelta(days=20))
    high_new = pg.campaign(rate=7, start=NOW - timedelta(days=2))
    mid = pg.campaign(rate=5)
    pg.campaign(rate=10, mccs={"5812": None})                  # другой MCC
    assert _ids(pg, user_id) == [high_new, high_old, mid, low]


def test_unknown_user_gets_nothing(pg):
    pg.campaign()
    assert _ids(pg, "00000000-0000-0000-0000-000000000003") == []
    assert _ids(pg, None) == []


def test_all_rules_together_select_exactly_the_eligible_campaign(pg):
    """Клиент сегмента 4, RFM 3, с согласием; из шести кампаний положена одна."""
    user_id = pg.user(segment_id=4, rfm_score=3)
    good = pg.campaign(segments=[4], rfm_min=2, rfm_max=4, daily_limit=500,
                       mccs={"5411": 200})
    pg.campaign(segments=[5])                                    # чужой сегмент
    pg.campaign(rfm_min=4)                                       # RFM ниже порога
    pg.campaign(start=NOW + timedelta(days=1))                   # ещё не началась
    pg.campaign(end=NOW - timedelta(days=1), start=NOW - timedelta(days=9))  # закончилась
    pg.campaign(mccs={"5411": 2000})                             # покупка ниже минимума
    assert _ids(pg, user_id, amount=Decimal("1500")) == [good]
