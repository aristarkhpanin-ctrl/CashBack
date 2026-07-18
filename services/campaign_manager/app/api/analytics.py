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
    ChannelStats,
    CohortRetentionCell,
    DailyTrendPoint,
    DailyTrendResponse,
    FunnelResponse,
    FunnelStep,
    KpiResponse,
    KpiTrends,
    SegmentMatrixCell,
    TopCampaignItem,
)
from app.security import get_current_user
from app.segments import SEGMENT_BUCKETS, bucket_of

router = APIRouter(
    prefix="/analytics", tags=["analytics"],
    dependencies=[Depends(get_current_user)],
)

# Валидный сегмент-фильтр — витринная корзина (фаза 24).
_SEGMENT_PATTERN = "^(premium|mass|young|senior|business)$"


def _deciles_for(segment_id: str | None) -> list[int] | None:
    """Корзина (premium/…) → её децили, либо None (без фильтра)."""
    if not segment_id:
        return None
    return SEGMENT_BUCKETS.get(segment_id)


def _safe_pct(child: int, parent: int) -> float:
    if parent == 0:
        return 0.0
    return round((parent - child) / parent * 100.0, 2)


def _delta_pct(now: float, prev: float) -> float:
    """Дельта текущего периода к предыдущему, %."""
    if prev <= 0:
        return 0.0
    return round((now - prev) / prev * 100.0, 1)


# ---------------------------------------------------------------------------
# Funnel:
#   target_audience → received → opened → accepted → transacted → cashback_paid
# ---------------------------------------------------------------------------
@router.get("/funnel", response_model=FunnelResponse)
async def funnel(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365, description="period in days"),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> FunnelResponse:
    cutoff = datetime.now(UTC) - timedelta(days=period)
    deciles = _deciles_for(segment_id)  # фильтр по корзине (фаза 24)

    # 1. Target audience — users in campaign's target_segment_ids (or all users
    #    when no campaign is given), сужение по сегмент-фильтру.
    seg_ids: list[int] | None = None
    if campaign_id is not None:
        camp = await session.get(CashbackCampaign, campaign_id)
        seg_ids = list(camp.target_segment_ids) if camp else []
    if deciles is not None:
        seg_ids = ([s for s in seg_ids if s in deciles]
                   if seg_ids is not None else list(deciles))
    target_q = select(func.count(User.user_id))
    if seg_ids is not None:
        target_q = target_q.where(User.segment_id.in_(seg_ids))
    target = (await session.execute(target_q)).scalar_one() or 0

    rec_q = select(Recommendation).where(Recommendation.generated_at >= cutoff)
    if campaign_id is not None:
        rec_q = rec_q.where(Recommendation.campaign_id == campaign_id)
    if deciles is not None:
        rec_q = rec_q.where(Recommendation.user_id.in_(
            select(User.user_id).where(User.segment_id.in_(deciles))))

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
    if deciles is not None:
        accrual_q = accrual_q.where(CashbackAccrual.user_id.in_(
            select(User.user_id).where(User.segment_id.in_(deciles))))

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
    # Pending (фаза 24): доставлено (received>0), но нет ни одного отклика
    # (opened==0) → пост-доставочные стадии «ждут данных».
    delivered_no_response = received > 0 and opened == 0
    steps: list[FunnelStep] = []
    prev = raw[0][1]
    for idx, (name, value) in enumerate(raw):
        steps.append(FunnelStep(
            name=name, count=int(value),
            pending=(idx >= 2 and delivered_no_response),
            drop_off_pct=_safe_pct(value, prev) if idx > 0 else 0.0,
        ))
        prev = value
    return FunnelResponse(campaign_id=campaign_id, period_days=period, steps=steps)


# ---------------------------------------------------------------------------
# Aggregated KPIs (фаза 24) — единый источник для дашборда и аналитики.
# ---------------------------------------------------------------------------
async def _rec_count(session, *, since, until, campaign_ids, deciles,
                     accepted=False) -> int:
    q = select(func.count()).select_from(Recommendation).where(
        Recommendation.generated_at >= since,
        Recommendation.generated_at < until,
    )
    if campaign_ids is not None:
        q = q.where(Recommendation.campaign_id.in_(campaign_ids))
    if accepted:
        q = q.where(Recommendation.response_status == "ACCEPTED")
    if deciles is not None:
        q = q.where(Recommendation.user_id.in_(
            select(User.user_id).where(User.segment_id.in_(deciles))))
    return int((await session.execute(q)).scalar_one() or 0)


async def _accrual_sum(session, *, since, until, campaign_ids, deciles) -> float:
    q = select(func.coalesce(func.sum(CashbackAccrual.cashback_amount), 0)).where(
        CashbackAccrual.accrued_at >= since,
        CashbackAccrual.accrued_at < until,
    )
    if campaign_ids is not None:
        q = q.where(CashbackAccrual.campaign_id.in_(campaign_ids))
    if deciles is not None:
        q = q.where(CashbackAccrual.user_id.in_(
            select(User.user_id).where(User.segment_id.in_(deciles))))
    return float((await session.execute(q)).scalar_one() or 0)


@router.get("/kpis", response_model=KpiResponse)
async def kpis(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365, description="period in days"),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> KpiResponse:
    now = datetime.now(UTC)
    cur_since = now - timedelta(days=period)
    prev_since = now - timedelta(days=2 * period)
    deciles = _deciles_for(segment_id)

    # ---- Выборка кампаний: конкретная или активные+приостановленные --------
    camp_q = select(CashbackCampaign)
    if campaign_id is not None:
        camp_q = camp_q.where(CashbackCampaign.campaign_id == campaign_id)
    else:
        camp_q = camp_q.where(CashbackCampaign.status.in_(["ACTIVE", "PAUSED"]))
    campaigns = (await session.execute(camp_q)).scalars().all()
    campaign_ids = [c.campaign_id for c in campaigns]

    budget = sum((c.budget_total for c in campaigns), Decimal("0"))
    spent = sum((c.budget_spent for c in campaigns), Decimal("0"))

    # ---- Reach: уникальные пользователи целевых сегментов ∩ фильтра --------
    seg_union: set[int] = set()
    for c in campaigns:
        seg_union.update(c.target_segment_ids or [])
    reach_segments = (
        (seg_union & set(deciles)) if deciles is not None else seg_union
    ) if seg_union else (set(deciles) if deciles is not None else None)
    reach_q = select(func.count(func.distinct(User.user_id)))
    if reach_segments is not None:
        reach_q = reach_q.where(User.segment_id.in_(list(reach_segments)))
    reach = int((await session.execute(reach_q)).scalar_one() or 0)

    # ---- CTR + тренды: текущий период vs предыдущий ------------------------
    ids = campaign_ids if campaign_id is None else [campaign_id]
    ids_filter = ids if ids else None
    recv_now = await _rec_count(session, since=cur_since, until=now,
                                campaign_ids=ids_filter, deciles=deciles)
    acc_now = await _rec_count(session, since=cur_since, until=now,
                               campaign_ids=ids_filter, deciles=deciles,
                               accepted=True)
    recv_prev = await _rec_count(session, since=prev_since, until=cur_since,
                                 campaign_ids=ids_filter, deciles=deciles)
    acc_prev = await _rec_count(session, since=prev_since, until=cur_since,
                                campaign_ids=ids_filter, deciles=deciles,
                                accepted=True)
    spent_now = await _accrual_sum(session, since=cur_since, until=now,
                                   campaign_ids=ids_filter, deciles=deciles)
    spent_prev = await _accrual_sum(session, since=prev_since, until=cur_since,
                                    campaign_ids=ids_filter, deciles=deciles)

    avg_ctr = round(acc_now / recv_now, 6) if recv_now else 0.0
    ctr_prev = acc_prev / recv_prev if recv_prev else 0.0
    trends = KpiTrends(
        reach=_delta_pct(recv_now, recv_prev),
        spent=_delta_pct(spent_now, spent_prev),
        ctr=_delta_pct(avg_ctr, ctr_prev),
    )
    return KpiResponse(
        campaigns_count=len(campaigns),
        reach=reach, spent=spent, budget=budget, avg_ctr=avg_ctr,
        trends=trends,
        has_data=(spent > 0 and avg_ctr > 0),
    )


# ---------------------------------------------------------------------------
# Segment × MCC matrix with CTR
# ---------------------------------------------------------------------------
@router.get("/segment-matrix", response_model=list[SegmentMatrixCell])
async def segment_matrix(
    period: int = Query(default=30, ge=1, le=365),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> list[SegmentMatrixCell]:
    cutoff = datetime.now(UTC) - timedelta(days=period)
    deciles = _deciles_for(segment_id)
    params: dict = {"cutoff": cutoff}
    seg_clause = ""
    if deciles is not None:
        seg_clause = "AND u.segment_id = ANY(:deciles)"
        params["deciles"] = deciles
    sql = text(
        f"""
        SELECT u.segment_id::int               AS segment_id,
               r.mcc_code                      AS mcc_code,
               COUNT(*)                        AS impressions,
               SUM(CASE WHEN r.response_status = 'ACCEPTED' THEN 1 ELSE 0 END)
                                               AS accepted
          FROM recommendations r
          JOIN users u USING (user_id)
         WHERE r.generated_at >= :cutoff
           AND u.segment_id IS NOT NULL
           {seg_clause}
         GROUP BY u.segment_id, r.mcc_code
         ORDER BY segment_id, mcc_code
        """
    )
    rows = (await session.execute(sql, params)).mappings().all()
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


# ---------------------------------------------------------------------------
# Daily trend — принятые предложения по дням и сегментным корзинам (фаза 16).
# Источник: recommendations.responded_at (миграция 003), а не generated_at —
# кривая отражает момент реакции клиента, не момент генерации.
# ---------------------------------------------------------------------------
_DAILY_TREND_SQL = text(
    """
    SELECT date_trunc('day', r.responded_at)::date AS day,
           u.segment_id                            AS segment_id,
           count(*)                                AS accepted
      FROM recommendations r
      JOIN users u ON u.user_id = r.user_id
     WHERE r.response_status = 'ACCEPTED'
       AND r.responded_at IS NOT NULL
       AND r.responded_at >= :cutoff
       AND (:campaign_id::uuid IS NULL OR r.campaign_id = :campaign_id::uuid)
       AND (:deciles::int[] IS NULL OR u.segment_id = ANY(:deciles))
     GROUP BY 1, 2
     ORDER BY 1
    """
)


@router.get("/daily-trend", response_model=DailyTrendResponse)
async def daily_trend(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365, description="period in days"),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> DailyTrendResponse:
    cutoff = datetime.now(UTC) - timedelta(days=period)
    rows = (
        await session.execute(
            _DAILY_TREND_SQL,
            {"cutoff": cutoff,
             "campaign_id": str(campaign_id) if campaign_id else None,
             "deciles": _deciles_for(segment_id)},
        )
    ).all()

    # Свёртка децилей в 5 корзин на стороне API — фронтенд получает готовые
    # группы и не знает о децилях.
    agg: dict[tuple[str, str], int] = {}
    for day, segment_id, accepted in rows:
        bucket = bucket_of(segment_id)
        if bucket is None:
            continue
        key = (day.isoformat(), bucket)
        agg[key] = agg.get(key, 0) + int(accepted)

    points = [
        DailyTrendPoint(date=d, segment_bucket=b, accepted=n)
        for (d, b), n in sorted(agg.items())
    ]
    return DailyTrendResponse(
        campaign_id=campaign_id, period_days=period, points=points,
    )


# ---------------------------------------------------------------------------
# Channels — эффективность каналов доставки (фаза 16).
# sent: все рекомендации канала; opened: клиент отреагировал (≠ PENDING);
# converted: принял. Семантика «opened» для push-канала — факт реакции,
# delivery-receipt в контуре не моделируется.
# ---------------------------------------------------------------------------
_CHANNELS_SQL = text(
    """
    SELECT r.channel::text AS channel,
           count(*)        AS sent,
           count(*) FILTER (WHERE r.response_status <> 'PENDING') AS opened,
           count(*) FILTER (WHERE r.response_status = 'ACCEPTED') AS converted
      FROM recommendations r
      JOIN users u ON u.user_id = r.user_id
     WHERE r.channel IS NOT NULL
       AND r.generated_at >= :cutoff
       AND (:campaign_id::uuid IS NULL OR r.campaign_id = :campaign_id::uuid)
       AND (:deciles::int[] IS NULL OR u.segment_id = ANY(:deciles))
     GROUP BY r.channel
     ORDER BY sent DESC
    """
)


@router.get("/channels", response_model=list[ChannelStats])
async def channels(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365, description="period in days"),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> list[ChannelStats]:
    cutoff = datetime.now(UTC) - timedelta(days=period)
    rows = (
        await session.execute(
            _CHANNELS_SQL,
            {"cutoff": cutoff,
             "campaign_id": str(campaign_id) if campaign_id else None,
             "deciles": _deciles_for(segment_id)},
        )
    ).all()
    # pending (фаза 24): канал доставил, но откликов ещё нет.
    return [
        ChannelStats(channel=ch, sent=int(s), opened=int(o), converted=int(c),
                     pending=int(s) > 0 and int(o) == 0)
        for ch, s, o, c in rows
    ]
