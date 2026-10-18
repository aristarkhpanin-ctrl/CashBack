"""009: ``calculate_cashback(campaign, mcc, amount)`` (B21, §1.6)."""
from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

UNDEFINED_FUNCTION = "42883"
TIERS = [
    {"min_amount": 0, "max_amount": 5000, "rate": 1.0},
    {"min_amount": 5000, "max_amount": 20000, "rate": 2.0},
    {"min_amount": 20000, "max_amount": None, "rate": 3.0},
]


def _cashback(pg, campaign_id, mcc, amount):
    return pg.value("SELECT calculate_cashback(%s, %s, %s)", campaign_id, mcc, amount)


def test_flat_rate(pg):
    campaign_id = pg.campaign(rate=5.0)
    assert _cashback(pg, campaign_id, "5411", 1500) == Decimal("75.00")


def test_progressive_tiers_unchanged(pg):
    campaign_id = pg.campaign(tiers=TIERS)
    # 5000·1 % + 15000·2 % + 5000·3 % = 50 + 300 + 150
    assert _cashback(pg, campaign_id, "5411", 25000) == Decimal("500.00")
    assert _cashback(pg, campaign_id, "5411", 3000) == Decimal("30.00")


def test_mcc_outside_campaign_categories_pays_nothing(pg):
    campaign_id = pg.campaign(mccs={"5411": None, "5812": None})
    assert _cashback(pg, campaign_id, "5812", 1000) == Decimal("50.00")
    assert _cashback(pg, campaign_id, "5541", 1000) == 0
    assert _cashback(pg, campaign_id, "54110", 1000) == 0      # не обрезается до 5411
    assert _cashback(pg, campaign_id, None, 1000) == 0


@pytest.mark.parametrize(("amount", "expected"), [
    (499.99, Decimal("0")),
    (500, Decimal("25.00")),
    (800, Decimal("40.00")),
])
def test_category_minimum_wins_over_lower_campaign_minimum(pg, amount, expected):
    """Фронт ставит общий минимум = наименьшему из категорий (B21)."""
    campaign_id = pg.campaign(min_amount=100, mccs={"5411": 500, "5812": 100})
    assert _cashback(pg, campaign_id, "5411", amount) == expected
    assert _cashback(pg, campaign_id, "5812", 200) == Decimal("10.00")


def test_category_minimum_below_campaign_minimum_applies(pg):
    campaign_id = pg.campaign(min_amount=1000, mccs={"5411": 100})
    assert _cashback(pg, campaign_id, "5411", 200) == Decimal("10.00")
    assert _cashback(pg, campaign_id, "5411", 99) == 0


def test_campaign_minimum_applies_when_category_has_none(pg):
    campaign_id = pg.campaign(min_amount=300, mccs={"5411": None})
    assert _cashback(pg, campaign_id, "5411", 299.99) == 0
    assert _cashback(pg, campaign_id, "5411", 300) == Decimal("15.00")


def test_no_minimum_at_all(pg):
    campaign_id = pg.campaign(min_amount=None, mccs={"5411": None})
    assert _cashback(pg, campaign_id, "5411", 1) == Decimal("0.05")


def test_category_minimum_also_gates_tiers(pg):
    campaign_id = pg.campaign(tiers=TIERS, mccs={"5411": 10000})
    assert _cashback(pg, campaign_id, "5411", 9999) == 0
    assert _cashback(pg, campaign_id, "5411", 10000) == Decimal("150.00")


def test_unknown_campaign_raises(pg):
    exc = pg.error("SELECT calculate_cashback(%s, '5411', 100)",
                   "00000000-0000-0000-0000-000000000002")
    assert exc.diag.message_primary == (
        "campaign 00000000-0000-0000-0000-000000000002 not found")


def test_two_argument_version_is_gone(pg):
    """Без MCC минимум категории не проверить — старой сигнатуры нет."""
    campaign_id = pg.campaign()
    exc = pg.error("SELECT calculate_cashback(%s::uuid, 100::numeric)", campaign_id)
    assert exc.pgcode == UNDEFINED_FUNCTION
    assert pg.rows("SELECT pg_get_function_identity_arguments(oid) FROM pg_proc "
                   "WHERE proname = 'calculate_cashback'") == [
        ("p_campaign_id uuid, p_mcc text, p_amount numeric",)]
