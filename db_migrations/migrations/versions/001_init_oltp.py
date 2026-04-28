"""init OLTP schema (chapter 2.2, table 9)

Revision ID: 001
Revises:
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CAMPAIGN_STATUS_VALUES = ("DRAFT", "ACTIVE", "PAUSED", "COMPLETED")
RECOMMENDATION_RESPONSE_VALUES = ("PENDING", "ACCEPTED", "DECLINED", "EXPIRED", "SNOOZE")
CONSENT_STATUS_VALUES = ("GRANTED", "REVOKED", "PENDING")
ACCRUAL_STATUS_VALUES = ("PENDING", "PAID")
AB_EXPERIMENT_STATUS_VALUES = ("DRAFT", "ACTIVE", "STOPPED")
AB_EVENT_TYPE_VALUES = ("IMPRESSION", "CLICK", "CONVERSION")


def _enum(name: str, values: Sequence[str]) -> postgresql.ENUM:
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    postgresql.ENUM(*CAMPAIGN_STATUS_VALUES, name="campaign_status").create(bind, checkfirst=True)
    postgresql.ENUM(*RECOMMENDATION_RESPONSE_VALUES, name="recommendation_response_status").create(bind, checkfirst=True)
    postgresql.ENUM(*CONSENT_STATUS_VALUES, name="consent_status").create(bind, checkfirst=True)
    postgresql.ENUM(*ACCRUAL_STATUS_VALUES, name="accrual_status").create(bind, checkfirst=True)
    postgresql.ENUM(*AB_EXPERIMENT_STATUS_VALUES, name="ab_experiment_status").create(bind, checkfirst=True)
    postgresql.ENUM(*AB_EVENT_TYPE_VALUES, name="ab_event_type").create(bind, checkfirst=True)

    # ------------------------------------------------------------------
    # users
    # ------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("external_id", sa.String(64), nullable=False, unique=True),
        sa.Column("segment_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    # ------------------------------------------------------------------
    # user_segments — segment history (SCD-2 style)
    # ------------------------------------------------------------------
    op.create_table(
        "user_segments",
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("segment_id", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("user_id", "segment_id", "valid_from"),
    )
    op.create_index("ix_user_segments_user_id", "user_segments", ["user_id"])

    # ------------------------------------------------------------------
    # cashback_campaigns
    # ------------------------------------------------------------------
    op.create_table(
        "cashback_campaigns",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("target_segment_ids", postgresql.ARRAY(sa.Integer()), nullable=False,
                  server_default=sa.text("ARRAY[]::INTEGER[]")),
        sa.Column("cashback_rate", sa.Numeric(5, 2), nullable=False),
        sa.Column("min_transaction_amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("budget_total", sa.Numeric(15, 2), nullable=False),
        sa.Column("budget_spent", sa.Numeric(15, 2), nullable=False, server_default="0"),
        sa.Column("status", _enum("campaign_status", CAMPAIGN_STATUS_VALUES),
                  nullable=False, server_default="DRAFT"),
        sa.Column("start_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("allowed_channels", postgresql.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("ARRAY[]::TEXT[]")),
        sa.Column("require_existing_behavior", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("rate_tiers", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("budget_spent <= budget_total", name="ck_campaign_budget"),
        sa.CheckConstraint("end_date >= start_date", name="ck_campaign_dates"),
    )
    op.create_index(
        "ix_cashback_campaigns_status_active",
        "cashback_campaigns",
        ["status", "start_date", "end_date"],
    )

    # ------------------------------------------------------------------
    # campaign_categories — campaign × MCC mapping
    # ------------------------------------------------------------------
    op.create_table(
        "campaign_categories",
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cashback_campaigns.campaign_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("mcc_code", sa.CHAR(4), nullable=False),
        sa.Column("min_transaction_amount", sa.Numeric(15, 2), nullable=True),
        sa.PrimaryKeyConstraint("campaign_id", "mcc_code"),
    )
    op.create_index("ix_campaign_categories_mcc", "campaign_categories", ["mcc_code"])

    # ------------------------------------------------------------------
    # recommendations
    # ------------------------------------------------------------------
    op.create_table(
        "recommendations",
        sa.Column("recommendation_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cashback_campaigns.campaign_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("mcc_code", sa.CHAR(4), nullable=False),
        sa.Column("model_score", postgresql.REAL(), nullable=False),
        sa.Column("generated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("response_status",
                  _enum("recommendation_response_status", RECOMMENDATION_RESPONSE_VALUES),
                  nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    # Index per chapter 2.2: hot lookup of latest recommendations per user.
    op.execute(
        "CREATE INDEX ix_recommendations_user_generated "
        "ON recommendations (user_id, generated_at DESC);"
    )
    # Partial index for the queue of unanswered recommendations.
    op.execute(
        "CREATE INDEX ix_recommendations_pending "
        "ON recommendations (user_id, expires_at) "
        "WHERE response_status = 'PENDING';"
    )
    op.create_index("ix_recommendations_campaign", "recommendations", ["campaign_id"])

    # ------------------------------------------------------------------
    # user_consents
    # ------------------------------------------------------------------
    op.create_table(
        "user_consents",
        sa.Column("consent_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("consent_type", sa.String(64), nullable=False),
        sa.Column("status", _enum("consent_status", CONSENT_STATUS_VALUES),
                  nullable=False, server_default="PENDING"),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    # INCLUDE-индекс из главы 2.2: точечный lookup статуса согласия.
    op.execute(
        "CREATE INDEX ix_user_consents_user_type "
        "ON user_consents (user_id, consent_type) INCLUDE (status);"
    )

    # ------------------------------------------------------------------
    # cashback_accruals
    # ------------------------------------------------------------------
    op.create_table(
        "cashback_accruals",
        sa.Column("accrual_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("cashback_campaigns.campaign_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("transaction_id", sa.String(128), nullable=False),
        sa.Column("mcc_code", sa.CHAR(4), nullable=False),
        sa.Column("transaction_amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("cashback_amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("status", _enum("accrual_status", ACCRUAL_STATUS_VALUES),
                  nullable=False, server_default="PENDING"),
        sa.Column("accrued_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("transaction_id", "campaign_id",
                            name="uq_accrual_transaction_campaign"),
    )
    op.create_index("ix_cashback_accruals_user_accrued",
                    "cashback_accruals", ["user_id", "accrued_at"])
    op.create_index("ix_cashback_accruals_status", "cashback_accruals", ["status"])

    # ------------------------------------------------------------------
    # ab_experiments / ab_variants / ab_assignments / ab_events
    # ------------------------------------------------------------------
    op.create_table(
        "ab_experiments",
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("status", _enum("ab_experiment_status", AB_EXPERIMENT_STATUS_VALUES),
                  nullable=False, server_default="DRAFT"),
        sa.Column("target_metric", sa.String(64), nullable=False),
        sa.Column("start_date", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("end_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("variants", postgresql.JSONB(), nullable=True),
    )

    op.create_table(
        "ab_variants",
        sa.Column("variant_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_experiments.experiment_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("traffic_weight", sa.Float(), nullable=False),
        sa.Column("strategy_class", sa.String(255), nullable=False),
        sa.Column("strategy_params", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint("experiment_id", "name", name="uq_variant_name_per_experiment"),
        sa.CheckConstraint("traffic_weight >= 0 AND traffic_weight <= 1",
                           name="ck_variant_weight"),
    )

    op.create_table(
        "ab_assignments",
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("experiment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_experiments.experiment_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("variant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_variants.variant_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "experiment_id",
                            name="uq_user_experiment"),
    )
    # Hot-path index per chapter 2.2.
    op.create_index("ix_ab_assignments_user_experiment",
                    "ab_assignments", ["user_id", "experiment_id"])

    op.create_table(
        "ab_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("ab_assignments.assignment_id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("event_type", _enum("ab_event_type", AB_EVENT_TYPE_VALUES),
                  nullable=False),
        sa.Column("event_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("revenue", sa.Numeric(15, 2), nullable=True),
    )
    op.create_index("ix_ab_events_assignment_type",
                    "ab_events", ["assignment_id", "event_type"])

    # ------------------------------------------------------------------
    # calculate_cashback(p_campaign_id UUID, p_amount NUMERIC) RETURNS NUMERIC
    # Progressive (tiered) rate scale based on rate_tiers JSONB:
    #   [
    #     {"min_amount": 0,     "max_amount": 5000,  "rate": 1.0},
    #     {"min_amount": 5000,  "max_amount": 20000, "rate": 2.0},
    #     {"min_amount": 20000, "max_amount": null,  "rate": 3.0}
    #   ]
    # When rate_tiers is empty/NULL, falls back to flat cashback_rate.
    # ------------------------------------------------------------------
    op.execute(
        """
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
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS calculate_cashback(UUID, NUMERIC);")

    op.drop_index("ix_ab_events_assignment_type", table_name="ab_events")
    op.drop_table("ab_events")
    op.drop_index("ix_ab_assignments_user_experiment", table_name="ab_assignments")
    op.drop_table("ab_assignments")
    op.drop_table("ab_variants")
    op.drop_table("ab_experiments")

    op.drop_index("ix_cashback_accruals_status", table_name="cashback_accruals")
    op.drop_index("ix_cashback_accruals_user_accrued", table_name="cashback_accruals")
    op.drop_table("cashback_accruals")

    op.execute("DROP INDEX IF EXISTS ix_user_consents_user_type;")
    op.drop_table("user_consents")

    op.drop_index("ix_recommendations_campaign", table_name="recommendations")
    op.execute("DROP INDEX IF EXISTS ix_recommendations_pending;")
    op.execute("DROP INDEX IF EXISTS ix_recommendations_user_generated;")
    op.drop_table("recommendations")

    op.drop_index("ix_campaign_categories_mcc", table_name="campaign_categories")
    op.drop_table("campaign_categories")

    op.drop_index("ix_cashback_campaigns_status_active", table_name="cashback_campaigns")
    op.drop_table("cashback_campaigns")

    op.drop_index("ix_user_segments_user_id", table_name="user_segments")
    op.drop_table("user_segments")
    op.drop_table("users")

    bind = op.get_bind()
    for enum_name in (
        "ab_event_type",
        "ab_experiment_status",
        "accrual_status",
        "consent_status",
        "recommendation_response_status",
        "campaign_status",
    ):
        postgresql.ENUM(name=enum_name).drop(bind, checkfirst=True)
