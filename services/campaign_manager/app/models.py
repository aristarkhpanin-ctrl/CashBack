"""SQLAlchemy 2.0 ORM models — mirrors db_migrations/001_init_oltp.py."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, UUID
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# ---------------------------------------------------------------------------
# PostgreSQL ENUM types — created by Alembic migration 001; here we just
# attach to the existing types via ``create_type=False``.
# ---------------------------------------------------------------------------
campaign_status_enum = PGEnum(
    "DRAFT", "ACTIVE", "PAUSED", "COMPLETED",
    name="campaign_status", create_type=False,
)

recommendation_status_enum = PGEnum(
    "PENDING", "ACCEPTED", "DECLINED", "EXPIRED", "SNOOZE",
    name="recommendation_response_status", create_type=False,
)

consent_status_enum = PGEnum(
    "GRANTED", "REVOKED", "PENDING",
    name="consent_status", create_type=False,
)

accrual_status_enum = PGEnum(
    "PENDING", "PAID",
    name="accrual_status", create_type=False,
)

ab_experiment_status_enum = PGEnum(
    "DRAFT", "ACTIVE", "STOPPED",
    name="ab_experiment_status", create_type=False,
)

ab_event_type_enum = PGEnum(
    "IMPRESSION", "CLICK", "CONVERSION",
    name="ab_event_type", create_type=False,
)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    segment_id: Mapped[Optional[int]] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CashbackCampaign(Base):
    __tablename__ = "cashback_campaigns"

    campaign_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    target_segment_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer))
    cashback_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    min_transaction_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))
    budget_total: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    budget_spent: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0"))
    status: Mapped[str] = mapped_column(campaign_status_enum, default="DRAFT")
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    allowed_channels: Mapped[list[str]] = mapped_column(ARRAY(Text))
    require_existing_behavior: Mapped[bool] = mapped_column(Boolean, default=False)
    rate_tiers: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    categories: Mapped[list[CampaignCategory]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("budget_spent <= budget_total", name="ck_campaign_budget"),
        CheckConstraint("end_date >= start_date",       name="ck_campaign_dates"),
    )


class CampaignCategory(Base):
    __tablename__ = "campaign_categories"

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashback_campaigns.campaign_id", ondelete="CASCADE"),
        primary_key=True,
    )
    mcc_code: Mapped[str] = mapped_column(CHAR(4), primary_key=True)
    min_transaction_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2))

    campaign: Mapped[CashbackCampaign] = relationship(back_populates="categories")


class Recommendation(Base):
    __tablename__ = "recommendations"

    recommendation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashback_campaigns.campaign_id", ondelete="CASCADE"),
    )
    mcc_code: Mapped[str] = mapped_column(CHAR(4))
    model_score: Mapped[float] = mapped_column(REAL)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response_status: Mapped[str] = mapped_column(
        recommendation_status_enum, default="PENDING",
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserConsent(Base):
    __tablename__ = "user_consents"

    consent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
    )
    consent_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(consent_status_enum, default="PENDING")
    document_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CashbackAccrual(Base):
    __tablename__ = "cashback_accruals"

    accrual_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="CASCADE"),
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashback_campaigns.campaign_id", ondelete="CASCADE"),
    )
    transaction_id: Mapped[str] = mapped_column(String(128))
    mcc_code: Mapped[str] = mapped_column(CHAR(4))
    transaction_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    cashback_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2))
    status: Mapped[str] = mapped_column(accrual_status_enum, default="PENDING")
    accrued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("transaction_id", "campaign_id",
                         name="uq_accrual_transaction_campaign"),
    )


# ---------------------------------------------------------------------------
# A/B testing — table 20
# ---------------------------------------------------------------------------
class ABExperiment(Base):
    __tablename__ = "ab_experiments"

    experiment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(ab_experiment_status_enum, default="DRAFT")
    target_metric: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    variants: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)


class ABVariant(Base):
    __tablename__ = "ab_variants"

    variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ab_experiments.experiment_id", ondelete="CASCADE"),
    )
    name: Mapped[str] = mapped_column(String(64))
    traffic_weight: Mapped[float] = mapped_column(Float)
    strategy_class: Mapped[str] = mapped_column(String(255))
    strategy_params: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)


class ABAssignment(Base):
    __tablename__ = "ab_assignments"

    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ab_experiments.experiment_id", ondelete="CASCADE"),
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ab_variants.variant_id", ondelete="CASCADE"),
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ABEvent(Base):
    __tablename__ = "ab_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("ab_assignments.assignment_id", ondelete="CASCADE"),
    )
    event_type: Mapped[str] = mapped_column(ab_event_type_enum)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revenue: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
