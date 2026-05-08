"""Analytics endpoints — funnel, segment×MCC matrix, cohorts, top campaigns."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import (
    CampaignCategory,
    CashbackAccrual,
    CashbackCampaign,
    Recommendation,
    User,
)
from app.schemas import (
    CohortRetentionCell,
    FunnelResponse,
    FunnelStep,
    SegmentMatrixCell,
    TopCampaignItem,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])


def _safe_pct(child: int, parent: int) -> float:
    if parent == 0:
        return 0.0
    return round((parent - child) / parent * 100.0, 2)


# ---------------------------------------------------------------------------
# Funnel:
#   target_audience → received → opened → accepted → transacted → cashback_paid
# ---------------------------------------------------------------------------
@router.get("/funnel", response_model=FunnelResponse)
async def funnel(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365, description="period in days"),
    session: AsyncSession = Depends(get_session_dep),
) -> FunnelResponse:
    cutoff = datetime.now(UTC) - timedelta(days=period)

    # 1. Target audience — users in campaign's target_segment_ids (or all users
    #    when no campaign is given).
    if campaign_id is not None:
        camp = await session.get(CashbackCampaign, campaign_id)
        target_segments = list(camp.target_segment_ids) if camp else []
        if target_segments:
            target_q = select(func.count(User.user_id)).where(
                User.segment_id.in_(target_segments)
            )
        else:
            target_q = select(func.count(User.user_id))
    else:
        target_q = select(func.count(User.user_id))
    target = (await session.execute(target_q)).scalar_one() or 0

    rec_q = select(Recommendation).where(Recommendation.generated_at >= cutoff)
    if campaign_id is not None:
        rec_q = rec_q.where(Recommendation.campaign_id == campaign_id)

    received_q = select(func.count()).select_from(rec_q.subquery())
    received = (await session.execute(received_q)).scalar_one() or 0

    opened_q = select(func.count()).select_from(
        rec_q.where(Recommendation.response_status != "PENDING").subquery()
    )
    opened = (await session.execute(opened_q)).scalar_one() or 0

    accepted_q = select(func.count()).select_from(
        rec_q.where(Recommendation.response_status == "ACCEPTED").subquery()
    )
    accepted = (await session.execute(accepted_q)).scalar_one() or 0

    accrual_q = select(CashbackAccrual).where(CashbackAccrual.accrued_at >= cutoff)
    if campaign_id is not None:
        accrual_q = accrual_q.where(CashbackAccrual.campaign_id == campaign_id)

    transacted_q = select(func.count()).select_from(accrual_q.subquery())
    transacted = (await session.execute(transacted_q)).scalar_one() or 0

    cashback_paid_q = select(func.count()).select_from(
        accrual_q.where(CashbackAccrual.status == "PAID").subquery()
    )
    cashback_paid = (await session.execute(cashback_paid_q)).scalar_one() or 0

    raw = [
        ("target_audience", target),
        ("received",        received),
        ("opened",          opened),
        ("accepted",        accepted),
        ("transacted",      transacted),
        ("cashback_paid",   cashback_paid),
    ]
    steps: list[FunnelStep] = []
    prev = raw[0][1]
    for idx, (name, value) in enumerate(raw):
        steps.append(FunnelStep(
            name=name, count=int(value),
            drop_off_pct=_safe_pct(value, prev) if idx > 0 else 0.0,
        ))
        prev = value
    return FunnelResponse(campaign_id=campaign_id, period_days=period, steps=steps)


# ---------------------------------------------------------------------------
# Segment × MCC matrix with CTR
# ---------------------------------------------------------------------------
@router.get("/segment-matrix", response_model=list[SegmentMatrixCell])
async def segment_matrix(
    period: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session_dep),
) -> list[SegmentMatrixCell]:
    cutoff = datetime.now(UTC) - timedelta(days=period)
    sql = text(
        """
        SELECT u.segment_id::int               AS segment_id,
               r.mcc_code                      AS mcc_code,
               COUNT(*)                        AS impressions,
               SUM(CASE WHEN r.response_status = 'ACCEPTED' THEN 1 ELSE 0 END)
                                               AS accepted
          FROM recommendations r
          JOIN users u USING (user_id)
         WHERE r.generated_at >= :cutoff
           AND u.segment_id IS NOT NULL
         GROUP BY u.segment_id, r.mcc_code
         ORDER BY segment_id, mcc_code
        """
    )
    rows = (await session.execute(sql, {"cutoff": cutoff})).mappings().all()
    out: list[SegmentMatrixCell] = []
    for r in rows:
        impressions = int(r["impressions"])
        accepted = int(r["accepted"] or 0)
        ctr = float(accepted) / impressions if impressions else 0.0
        out.append(SegmentMatrixCell(
            segment_id=int(r["segment_id"]),
            mcc_code=str(r["mcc_code"]).strip(),
            impressions=impressions, accepted=accepted, ctr=round(ctr, 6),
        ))
    return out


# ---------------------------------------------------------------------------
# Cohort retention grid
# ---------------------------------------------------------------------------
@router.get("/cohort-retention", response_model=list[CohortRetentionCell])
async def cohort_retention(
    cohort_month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    periods: int = Query(default=6, ge=1, le=24, description="months out"),
    session: AsyncSession = Depends(get_session_dep),
) -> list[CohortRetentionCell]:
    sql = text(
        """
        WITH cohort AS (
            SELECT user_id::text AS user_id
              FROM users
             WHERE date_trunc('month', created_at) =
                   date_trunc('month', to_date(:cohort_month, 'YYYY-MM'))
        ),
        cohort_size AS (SELECT count(*) AS n FROM cohort),
        activity AS (
            SELECT date_part('month', age(date_trunc('month', a.accrued_at),
                                          to_date(:cohort_month, 'YYYY-MM')))::int
                   AS period,
                   COUNT(DISTINCT a.user_id::text) AS active_users
              FROM cashback_accruals a
              JOIN cohort c ON c.user_id = a.user_id::text
             WHERE a.accrued_at >=  to_date(:cohort_month, 'YYYY-MM')
               AND a.accrued_at <   to_date(:cohort_month, 'YYYY-MM')
                                    + (:periods || ' months')::interval
             GROUP BY 1
        )
        SELECT period, active_users,
               (SELECT n FROM cohort_size) AS cohort_n
          FROM activity
         ORDER BY period
        """
    )
    rows = (await session.execute(
        sql, {"cohort_month": cohort_month, "periods": periods},
    )).mappings().all()
    out: list[CohortRetentionCell] = []
    for r in rows:
        n = int(r["cohort_n"] or 0)
        active = int(r["active_users"] or 0)
        retention = float(active) / n if n else 0.0
        out.append(CohortRetentionCell(
            cohort_month=cohort_month, period=int(r["period"]),
            active_users=active, retention=round(retention, 6),
        ))
    return out


# ---------------------------------------------------------------------------
# Top campaigns by metric
# ---------------------------------------------------------------------------
@router.get("/top-campaigns", response_model=list[TopCampaignItem])
async def top_campaigns(
    metric: str = Query(default="roi",
                        pattern="^(roi|ctr|conversion_rate|cashback_paid)$"),
    limit: int = Query(default=5, ge=1, le=50),
    session: AsyncSession = Depends(get_session_dep),
) -> list[TopCampaignItem]:
    # Aggregate per-campaign accruals + recommendations into a single query.
    sql = text(
        """
        WITH recs AS (
            SELECT campaign_id,
                   COUNT(*) AS impressions,
                   SUM(CASE WHEN response_status = 'ACCEPTED' THEN 1 ELSE 0 END) AS accepted
              FROM recommendations
             GROUP BY campaign_id
        ),
        accruals AS (
            SELECT campaign_id,
                   COUNT(*)            AS transactions,
                   SUM(transaction_amount) AS revenue,
                   SUM(cashback_amount)    AS cashback_paid
              FROM cashback_accruals
             GROUP BY campaign_id
        )
        SELECT c.campaign_id::text     AS campaign_id,
               c.name                  AS name,
               COALESCE(r.impressions, 0)   AS impressions,
               COALESCE(r.accepted,    0)   AS accepted,
               COALESCE(a.transactions, 0)  AS transactions,
               COALESCE(a.revenue,      0)  AS revenue,
               COALESCE(a.cashback_paid, 0) AS cashback_paid
          FROM cashback_campaigns c
     LEFT JOIN recs     r USING (campaign_id)
     LEFT JOIN accruals a USING (campaign_id)
        """
    )
    rows = (await session.execute(sql)).mappings().all()
    items: list[tuple[str, str, float]] = []
    for r in rows:
        impressions = int(r["impressions"])
        accepted = int(r["accepted"])
        cashback_paid = Decimal(r["cashback_paid"] or 0)
        revenue = Decimal(r["revenue"] or 0)
        if metric == "ctr":
            value = (float(accepted) / impressions) if impressions else 0.0
        elif metric == "conversion_rate":
            value = (float(accepted) / impressions) if impressions else 0.0
        elif metric == "cashback_paid":
            value = float(cashback_paid)
        else:  # roi
            value = float((revenue - cashback_paid) / cashback_paid) \
                if cashback_paid > 0 else 0.0
        items.append((r["campaign_id"], r["name"], value))
    items.sort(key=lambda kv: kv[2], reverse=True)
    return [
        TopCampaignItem(campaign_id=uuid.UUID(cid), name=name, metric=round(v, 4))
        for cid, name, v in items[:limit]
    ]
