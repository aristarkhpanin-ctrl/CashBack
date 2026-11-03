"""Pydantic v2 schemas for the Mobile BFF (chapter 3.2, table 22)."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
class MobileRecommendation(BaseModel):
    """Mobile-friendly recommendation payload — no model internals."""

    # Строка recommendations оффера (её пишет recommendation_api, B29) — по
    # этому id клиент откликается; оффера без строки в ответе нет.
    recommendation_id: uuid.UUID
    mcc_code: str
    category_name: str
    category_icon_url: str
    cashback_rate: str = Field(examples=["5%"])
    # Срок оффера: LEAST(конец кампании, выдача + OFFER_TTL_DAYS).
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
    # Канал, в котором клиент увидел предложение (по словам клиента;
    # мобильное приложение по умолчанию — IN_APP). Только в лог: канал
    # доставки в recommendations.channel пишут выдача и transaction_listener
    # (B29), отклик его не перезаписывает.
    channel: Literal["PUSH", "SMS", "EMAIL", "IN_APP"] = "IN_APP"


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
