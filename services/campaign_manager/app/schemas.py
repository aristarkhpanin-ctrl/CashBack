"""Pydantic v2 request/response schemas for the Campaign Manager."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MCC_RE = re.compile(r"^\d{4}$")
_ALLOWED_CHANNELS = {"ONLINE", "POS", "ATM", "MOBILE"}


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
class CampaignBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(min_length=1, max_length=255)
    target_segment_ids: list[int] = Field(min_length=1)
    cashback_rate: Decimal = Field(ge=0, le=100)
    min_transaction_amount: Decimal | None = Field(default=None, ge=0)
    budget_total: Decimal = Field(gt=0)
    start_date: datetime
    end_date: datetime
    allowed_channels: list[str] = Field(default_factory=list)
    require_existing_behavior: bool = False
    rate_tiers: list[dict] | None = None
    # Поля визарда (фаза 22).
    daily_limit: Decimal | None = Field(default=None, gt=0)
    auto_pause: bool = True
    rfm_min: int | None = Field(default=None, ge=1, le=5)
    rfm_max: int | None = Field(default=None, ge=1, le=5)

    @field_validator("allowed_channels")
    @classmethod
    def _channels_are_known(cls, v: list[str]) -> list[str]:
        for ch in v:
            if ch not in _ALLOWED_CHANNELS:
                raise ValueError(f"unknown channel: {ch!r} "
                                 f"(allowed: {sorted(_ALLOWED_CHANNELS)})")
        return v

    @model_validator(mode="after")
    def _dates_consistent(self):
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be strictly after start_date")
        return self

    @model_validator(mode="after")
    def _wizard_fields_consistent(self):
        if (self.rfm_min is not None and self.rfm_max is not None
                and self.rfm_min > self.rfm_max):
            raise ValueError("rfm_min must be ≤ rfm_max")
        if (self.daily_limit is not None
                and self.daily_limit > self.budget_total):
            raise ValueError("daily_limit must not exceed budget_total")
        return self


class CampaignCreate(CampaignBase):
    mcc_codes: list[str] = Field(min_length=1)
    # Per-категорийная мин. сумма транзакции {mcc_code: amount} (фаза 22).
    # Отсутствующие категории берут общий ``min_transaction_amount``.
    min_tx_amounts: dict[str, Decimal] | None = None

    @field_validator("mcc_codes")
    @classmethod
    def _mcc_codes_valid(cls, v: list[str]) -> list[str]:
        for code in v:
            if not _MCC_RE.match(code):
                raise ValueError(f"mcc_code {code!r} must be 4 digits")
        return v


class CampaignUpdate(BaseModel):
    """Partial update for ``PATCH /campaigns/{id}`` — DRAFT campaigns only."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    target_segment_ids: list[int] | None = Field(default=None, min_length=1)
    cashback_rate: Decimal | None = Field(default=None, ge=0, le=100)
    min_transaction_amount: Decimal | None = Field(default=None, ge=0)
    budget_total: Decimal | None = Field(default=None, gt=0)
    start_date: datetime | None = None
    end_date: datetime | None = None
    allowed_channels: list[str] | None = None
    require_existing_behavior: bool | None = None
    rate_tiers: list[dict] | None = None
    mcc_codes: list[str] | None = Field(default=None, min_length=1)
    # Поля визарда (фаза 22).
    daily_limit: Decimal | None = Field(default=None, gt=0)
    auto_pause: bool | None = None
    rfm_min: int | None = Field(default=None, ge=1, le=5)
    rfm_max: int | None = Field(default=None, ge=1, le=5)
    min_tx_amounts: dict[str, Decimal] | None = None

    @field_validator("allowed_channels")
    @classmethod
    def _channels_are_known(cls, v: list[str] | None) -> list[str] | None:
        for ch in v or []:
            if ch not in _ALLOWED_CHANNELS:
                raise ValueError(f"unknown channel: {ch!r} "
                                 f"(allowed: {sorted(_ALLOWED_CHANNELS)})")
        return v

    @field_validator("mcc_codes")
    @classmethod
    def _mcc_codes_valid(cls, v: list[str] | None) -> list[str] | None:
        for code in v or []:
            if not _MCC_RE.match(code):
                raise ValueError(f"mcc_code {code!r} must be 4 digits")
        return v

    @model_validator(mode="after")
    def _rfm_consistent(self):
        if (self.rfm_min is not None and self.rfm_max is not None
                and self.rfm_min > self.rfm_max):
            raise ValueError("rfm_min must be ≤ rfm_max")
        return self


class CampaignResponse(CampaignBase):
    campaign_id: uuid.UUID
    budget_spent: Decimal
    status: str
    mcc_codes: list[str] = Field(default_factory=list)
    created_by: uuid.UUID | None = None
    # Per-категорийная мин. сумма {mcc_code: amount} (фаза 22).
    min_tx_amounts: dict[str, Decimal] = Field(default_factory=dict)


class CampaignSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    campaign_id: uuid.UUID
    name: str
    status: str
    budget_total: Decimal
    budget_spent: Decimal
    cashback_rate: Decimal
    start_date: datetime
    end_date: datetime
    mcc_codes: list[str] = Field(default_factory=list)


class StatusActionResponse(BaseModel):
    campaign_id: uuid.UUID
    previous: str
    current: str
    action: str


class BudgetCheckRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    ttl_seconds: int = Field(default=300, gt=0, le=3600)


class BudgetCheckResponse(BaseModel):
    campaign_id: uuid.UUID
    reserved: bool
    request_id: str
    remaining_budget: Decimal
    reason: str | None = None


class CampaignStats(BaseModel):
    campaign_id: uuid.UUID
    impressions: int = 0
    clicks: int = 0
    accepted: int = 0
    transactions: int = 0
    revenue: Decimal = Decimal("0")
    cashback_paid: Decimal = Decimal("0")
    ctr: float = 0.0
    conversion_rate: float = 0.0
    roi: float = 0.0


class AudienceEstimateResponse(BaseModel):
    estimated_users: int
    segments: list[int]
    rfm_filters: dict[str, Any]


# ---------------------------------------------------------------------------
# Reference dictionaries (фаза 21)
# ---------------------------------------------------------------------------
class SegmentRef(BaseModel):
    """Сегментная корзина для UI: id, витринное имя, живой размер аудитории и
    децильная раскладка (чтобы фронт и rec_api читали маппинг из одного ответа)."""
    id: str
    name: str
    count: int
    deciles: list[int]


class MccCategoryRef(BaseModel):
    code: str
    name: str
    icon: str  # имя Lucide-иконки (не emoji) — см. reference_data.py


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
class FunnelStep(BaseModel):
    name: str
    count: int
    drop_off_pct: float = 0.0


class FunnelResponse(BaseModel):
    campaign_id: uuid.UUID | None = None
    period_days: int
    steps: list[FunnelStep]


class SegmentMatrixCell(BaseModel):
    segment_id: int
    mcc_code: str
    impressions: int
    accepted: int
    ctr: float


class CohortRetentionCell(BaseModel):
    cohort_month: str
    period: int
    active_users: int
    retention: float


class TopCampaignItem(BaseModel):
    campaign_id: uuid.UUID
    name: str
    metric: float


# ---------------------------------------------------------------------------
# A/B testing
# ---------------------------------------------------------------------------
class ABVariantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    traffic_weight: float = Field(ge=0.0, le=1.0)
    strategy_class: str = Field(min_length=1, max_length=255)
    strategy_params: dict[str, Any] | None = None


class ABExperimentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    target_metric: str = Field(min_length=1, max_length=64)
    start_date: datetime
    end_date: datetime | None = None
    variants: list[ABVariantCreate] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def _weights_sum_to_one(self):
        total = sum(v.traffic_weight for v in self.variants)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"traffic_weight values must sum to 1.0 (got {total:g})"
            )
        return self


class ABVariantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    variant_id: uuid.UUID
    name: str
    traffic_weight: float
    strategy_class: str
    strategy_params: dict[str, Any] | None = None


class ABExperimentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    experiment_id: uuid.UUID
    name: str
    status: str
    target_metric: str
    start_date: datetime
    end_date: datetime | None
    variants: list[ABVariantResponse] = Field(default_factory=list)


class ABVariantStats(BaseModel):
    variant_id: uuid.UUID
    name: str
    n: int = 0
    successes: int = 0
    rate: float = 0.0


class ABResults(BaseModel):
    experiment_id: uuid.UUID
    target_metric: str
    control: ABVariantStats
    treatment: ABVariantStats
    diff: float
    z: float | None = None
    p_value: float | None = None
    confidence_interval: tuple[float, float] | None = None
    significance: str  # significant | trending | no_data


class ABAssignmentResponse(BaseModel):
    user_id: uuid.UUID
    experiment_id: uuid.UUID
    variant_id: uuid.UUID
    variant_name: str
    assigned_at: datetime


# ---------------------------------------------------------------------------
# Auth / admin users (фаза 15)
# ---------------------------------------------------------------------------
_ADMIN_ROLES = {"ADMIN", "MARKETER", "ANALYST"}


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AdminUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class AdminUserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=6, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    role: str = "ANALYST"

    @field_validator("role")
    @classmethod
    def _role_known(cls, v: str) -> str:
        if v not in _ADMIN_ROLES:
            raise ValueError(f"unknown role: {v!r} (allowed: {sorted(_ADMIN_ROLES)})")
        return v


class AdminUserUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=128)

    @field_validator("role")
    @classmethod
    def _role_known(cls, v: str | None) -> str | None:
        if v is not None and v not in _ADMIN_ROLES:
            raise ValueError(f"unknown role: {v!r} (allowed: {sorted(_ADMIN_ROLES)})")
        return v


# ---------------------------------------------------------------------------
# Analytics: daily trend + channels (фаза 16)
# ---------------------------------------------------------------------------
class DailyTrendPoint(BaseModel):
    date: str                    # YYYY-MM-DD
    segment_bucket: str          # premium|mass|young|senior|business
    accepted: int


class DailyTrendResponse(BaseModel):
    campaign_id: uuid.UUID | None = None
    period_days: int
    points: list[DailyTrendPoint]


class ChannelStats(BaseModel):
    channel: str                 # PUSH|SMS|EMAIL|IN_APP
    sent: int
    opened: int                  # response_status != PENDING
    converted: int               # response_status == ACCEPTED


# ---------------------------------------------------------------------------
# ML limits (фаза 18) — бизнес-ограничения ML-рекомендаций
# ---------------------------------------------------------------------------
_SEGMENT_BUCKETS = {"premium", "mass", "young", "senior", "business"}
_RISK_LEVELS = {"low", "medium", "high"}


class MlLimitItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    segment_bucket: str
    min_rate: Decimal = Field(ge=0, le=100)
    max_rate: Decimal = Field(ge=0, le=100)
    daily_budget: Decimal = Field(ge=0)
    auto_approve: bool = False
    risk_level: str = "medium"

    @field_validator("segment_bucket")
    @classmethod
    def _bucket_known(cls, v: str) -> str:
        if v not in _SEGMENT_BUCKETS:
            raise ValueError(
                f"unknown segment bucket: {v!r} (allowed: {sorted(_SEGMENT_BUCKETS)})"
            )
        return v

    @field_validator("risk_level")
    @classmethod
    def _risk_known(cls, v: str) -> str:
        if v not in _RISK_LEVELS:
            raise ValueError(f"unknown risk level: {v!r} (allowed: {sorted(_RISK_LEVELS)})")
        return v

    @model_validator(mode="after")
    def _rates_consistent(self):
        if self.min_rate > self.max_rate:
            raise ValueError("min_rate must not exceed max_rate")
        return self


class MlLimitsResponse(BaseModel):
    global_enabled: bool
    limits: list[MlLimitItem]
    updated_by: str | None = None
    updated_at: datetime | None = None


class MlLimitsUpdate(BaseModel):
    global_enabled: bool | None = None
    limits: list[MlLimitItem] | None = Field(default=None, min_length=1)
