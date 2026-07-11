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


class CampaignCreate(CampaignBase):
    mcc_codes: list[str] = Field(min_length=1)

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


class CampaignResponse(CampaignBase):
    campaign_id: uuid.UUID
    budget_spent: Decimal
    status: str
    mcc_codes: list[str] = Field(default_factory=list)


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
