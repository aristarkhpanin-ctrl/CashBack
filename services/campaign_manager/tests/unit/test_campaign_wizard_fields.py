"""Unit tests for phase-22 campaign wizard fields.

Валидация схем (rfm_min≤rfm_max, daily_limit≤budget_total). Пауза кампании
по daily_limit из планировщика убрана (этап C, B22): лимит соблюдают
eligible_campaigns и транзакция начисления — см. test_scheduling_jobs.py.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import CampaignCreate, CampaignUpdate  # isort: skip

_BASE = dict(
    name="Test",
    target_segment_ids=[9, 10],
    cashback_rate="5.0",
    budget_total="1000000",
    start_date="2026-06-01T00:00:00Z",
    end_date="2026-06-30T23:59:59Z",
    allowed_channels=["ONLINE"],
    mcc_codes=["5411"],
)


# ── schema validation ─────────────────────────────────────────────────────────
def test_create_accepts_wizard_fields():
    c = CampaignCreate(**_BASE, daily_limit="50000", auto_pause=False,
                       rfm_min=2, rfm_max=4,
                       min_tx_amounts={"5411": "700"})
    assert c.daily_limit == 50000
    assert c.auto_pause is False
    assert c.min_tx_amounts["5411"] == 700


def test_create_rejects_rfm_inverted():
    with pytest.raises(ValidationError, match="rfm_min must be"):
        CampaignCreate(**_BASE, rfm_min=5, rfm_max=2)


def test_create_rejects_daily_limit_over_budget():
    with pytest.raises(ValidationError, match="daily_limit must not exceed"):
        CampaignCreate(**_BASE, daily_limit="2000000")  # > budget 1M


def test_create_rejects_rfm_out_of_range():
    with pytest.raises(ValidationError):
        CampaignCreate(**_BASE, rfm_min=0)   # квантили 1..5
    with pytest.raises(ValidationError):
        CampaignCreate(**_BASE, rfm_max=6)


def test_update_rfm_validation():
    with pytest.raises(ValidationError, match="rfm_min must be"):
        CampaignUpdate(rfm_min=5, rfm_max=1)
    ok = CampaignUpdate(daily_limit="10", auto_pause=True, rfm_min=1, rfm_max=3)
    assert ok.auto_pause is True
