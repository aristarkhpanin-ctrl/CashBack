"""Pydantic v2 schemas for the Mobile BFF (chapter 3.2, table 22)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
class MobileRecommendation(BaseModel):
    """Mobile-friendly recommendation payload — no model internals."""

    recommendation_id: uuid.UUID | None = None
    mcc_code: str
    category_name: str
    category_icon_url: str
    cashback_rate: str = Field(examples=["5%"])
    expires_at: datetime | None = None
    terms_summary: str = Field(max_length=140)
    deeplink: str
    campaign_id: uuid.UUID | None = None
    campaign_name: str | None = None


class MobileRecommendationsResponse(BaseModel):
    user_id: uuid.UUID
    recommendations: list[MobileRecommendation]
    model_version: str | None = None


# ---------------------------------------------------------------------------
class RespondAction(str, Enum):
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    SNOOZE = "SNOOZE"


class RespondRequest(BaseModel):
    action: RespondAction


class RespondResponse(BaseModel):
    recommendation_id: uuid.UUID
    action: RespondAction
    accepted_offer_key: str | None = None
    snooze_until: datetime | None = None


# ---------------------------------------------------------------------------
class CashbackHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    accrual_id: uuid.UUID
    transaction_id: str
    mcc_code: str
    category_name: str
    transaction_amount: Decimal
    cashback_amount: Decimal
    status: str
    accrued_at: datetime


class CashbackHistoryResponse(BaseModel):
    user_id: uuid.UUID
    period_from: datetime
    period_to: datetime
    items: list[CashbackHistoryItem]


class CashbackBalance(BaseModel):
    user_id: uuid.UUID
    pending_amount: Decimal
    available_amount: Decimal
    paid_amount: Decimal
    currency: str = "RUB"
