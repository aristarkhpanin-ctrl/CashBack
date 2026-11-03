"""Analytics endpoints — funnel, segment×MCC matrix, cohorts, top campaigns.

Определения (этап D, B33; docs/adr/0015-analytics-definitions.md):

* доставлено — есть строка ``recommendations`` (одна на показанный оффер);
* открыто (ответил) — ``response_status IN ('ACCEPTED','DECLINED','SNOOZE')``:
  EXPIRED и PENDING клиент не открывал;
* принято — ``ACCEPTED``;
* покупка — по офферу есть начисление (``cashback_accruals.recommendation_id``),
  в любом статусе; выплачено — начисление ``PAID`` (задача settle-accruals).

Стадии воронки считают различных клиентов (``COUNT(DISTINCT user_id)``),
поэтому после «получили» каждая стадия — подмножество предыдущей; отсев —
в пределах [0, 100] (оффер мог получить и клиент, сменивший сегмент).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session_dep
from app.models import (
    CashbackAccrual,
    CashbackCampaign,
    Recommendation,
    User,
)
from app.rbac import require_permission
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
from app.segments import SEGMENT_BUCKETS, bucket_of

# Право analytics (B35): раньше хватало логина — MARKETER без analytics
# получал все отчёты и UUID клиентов.
router = APIRouter(
    prefix="/analytics", tags=["analytics"],
    dependencies=[Depends(require_permission("analytics"))],
)

# Валидный сегмент-фильтр — витринная корзина (фаза 24).
_SEGMENT_PATTERN = "^(premium|mass|young|senior|business)$"

# Ответ клиента на оффер («открыто»): EXPIRED и PENDING — не открыто (B33).
_OPENED = ("ACCEPTED", "DECLINED", "SNOOZE")

# Тренд KPI по модулю не больше (B11): дальше — «>999%», а не ▲19400%.
TREND_CAP = 999.9


def _deciles_for(segment_id: str | None) -> list[int] | None:
    """Корзина (premium/…) → её децили, либо None (без фильтра)."""
    if not segment_id:
        return None
    return SEGMENT_BUCKETS.get(segment_id)


def _of_deciles(user_id_col, deciles: list[int]):
    """Клиент из децилей корзины (сегмент-фильтр)."""
    return user_id_col.in_(select(User.user_id).where(User.segment_id.in_(deciles)))


def _safe_pct(child: int, parent: int) -> float:
    """Отсев между стадиями, % в пределах [0, 100] (B33: был и отрицательным)."""
    if parent <= 0:
        return 0.0
    return round(min(100.0, max(0.0, (parent - child) / parent * 100.0)), 2)


def _trend(now: float, prev: float, base: int, min_base: int) -> float | None:
    """Изменение метрики к предыдущему окну, %.

    None — в предыдущем окне меньше ``min_base`` событий (``base``), ни одного
    или метрика там нулевая: процент от почти пустой базы бессмыслен (B11).
    Иначе — в пределах ±TREND_CAP.
    """
    if base <= 0 or base < min_base or prev == 0:
        return None
    delta = (now - prev) / prev * 100.0
    return round(max(-TREND_CAP, min(TREND_CAP, delta)), 1)


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

    # 1. Target audience — клиенты целевых сегментов кампании (пустой список —
    #    все клиенты, как в eligible_campaigns; нет кампании — никого) или все
    #    клиенты без кампании; сужение по сегмент-фильтру.
    seg_ids: list[int] | None = None
    if campaign_id is not None:
        camp = await session.get(CashbackCampaign, campaign_id)
        if camp is None:
            seg_ids = []
        elif camp.target_segment_ids:
            seg_ids = list(camp.target_segment_ids)
    if deciles is not None:
        seg_ids = ([s for s in seg_ids if s in deciles]
                   if seg_ids is not None else list(deciles))
    target_q = select(func.count(User.user_id))
    if seg_ids is not None:
        target_q = target_q.where(User.segment_id.in_(seg_ids))
    target = (await session.execute(target_q)).scalar_one() or 0

    # 2. Остальные стадии — различные клиенты по офферам окна; покупка и
    #    выплата — начисления по принятым офферам окна (recommendation_id,
    #    UNIQUE: соединение строк не размножает).
    users = func.count(func.distinct(Recommendation.user_id))
    accepted = Recommendation.response_status == "ACCEPTED"
    stages_q = (
        select(
            users,
            users.filter(Recommendation.response_status.in_(_OPENED)),
            users.filter(accepted),
            users.filter(accepted, CashbackAccrual.accrual_id.is_not(None)),
            users.filter(accepted, CashbackAccrual.status == "PAID"),
        )
        .select_from(Recommendation)
        .outerjoin(CashbackAccrual,
                   CashbackAccrual.recommendation_id == Recommendation.recommendation_id)
        .where(Recommendation.generated_at >= cutoff)
    )
    if campaign_id is not None:
        stages_q = stages_q.where(Recommendation.campaign_id == campaign_id)
    if deciles is not None:
        stages_q = stages_q.where(_of_deciles(Recommendation.user_id, deciles))
    received, opened, accepted_n, transacted, cashback_paid = (
        await session.execute(stages_q)).one()

    raw = [
        ("target_audience", target),
        ("received",        received),
        ("opened",          opened),
        ("accepted",        accepted_n),
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
            name=name, count=int(value or 0),
            pending=(idx >= 2 and delivered_no_response),
            drop_off_pct=_safe_pct(value or 0, prev or 0) if idx > 0 else 0.0,
        ))
        prev = value
    return FunnelResponse(campaign_id=campaign_id, period_days=period, steps=steps)


# ---------------------------------------------------------------------------
# Aggregated KPIs (фаза 24) — единый источник для дашборда и аналитики.
# ---------------------------------------------------------------------------
async def _offers(session, *, since, until, campaign_ids,
                  deciles) -> tuple[int, int, int]:
    """(доставлено офферов, охвачено клиентов, принято офферов) за окно."""
    q = (
        select(func.count(),
               func.count(func.distinct(Recommendation.user_id)),
               func.count().filter(Recommendation.response_status == "ACCEPTED"))
        .select_from(Recommendation)
        .where(Recommendation.generated_at >= since,
               Recommendation.generated_at < until,
               Recommendation.campaign_id.in_(campaign_ids))
    )
    if deciles is not None:
        q = q.where(_of_deciles(Recommendation.user_id, deciles))
    delivered, reached, accepted = (await session.execute(q)).one()
    return int(delivered or 0), int(reached or 0), int(accepted or 0)


async def _accruals(session, *, since, until, campaign_ids,
                    deciles) -> tuple[float, int]:
    """(сумма кэшбэка, число начислений) за окно."""
    q = select(func.coalesce(func.sum(CashbackAccrual.cashback_amount), 0),
               func.count()).where(
        CashbackAccrual.accrued_at >= since,
        CashbackAccrual.accrued_at < until,
        CashbackAccrual.campaign_id.in_(campaign_ids),
    )
    if deciles is not None:
        q = q.where(_of_deciles(CashbackAccrual.user_id, deciles))
    total, count = (await session.execute(q)).one()
    return float(total or 0), int(count or 0)


@router.get("/kpis", response_model=KpiResponse)
async def kpis(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365, description="period in days"),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> KpiResponse:
    min_base = get_settings().kpi_trend_min_base
    now = datetime.now(UTC)
    cur_since = now - timedelta(days=period)
    prev_since = now - timedelta(days=2 * period)
    deciles = _deciles_for(segment_id)

    # ---- Выборка кампаний: конкретная или активные+приостановленные --------
    # (без архивных: в работу они не вернутся и в списках скрыты)
    camp_q = select(CashbackCampaign)
    if campaign_id is not None:
        camp_q = camp_q.where(CashbackCampaign.campaign_id == campaign_id)
    else:
        camp_q = camp_q.where(CashbackCampaign.status.in_(["ACTIVE", "PAUSED"]),
                              CashbackCampaign.archived_at.is_(None))
    campaigns = (await session.execute(camp_q)).scalars().all()
    if campaign_id is None and not campaigns:
        # Работающих кампаний нет — нули, а не «все исторические» (B33).
        return KpiResponse(
            campaigns_count=0, reach=0, spent=Decimal("0"), budget=Decimal("0"),
            avg_ctr=0.0, trends=KpiTrends(), trend_window_days=period,
            has_data=False,
        )
    ids = [c.campaign_id for c in campaigns] if campaign_id is None else [campaign_id]

    budget = sum((c.budget_total for c in campaigns), Decimal("0"))
    spent = sum((c.budget_spent for c in campaigns), Decimal("0"))

    # ---- Охват, CTR, расход: текущее окно vs предыдущее той же длины -------
    delivered, reach, accepted = await _offers(
        session, since=cur_since, until=now, campaign_ids=ids, deciles=deciles)
    delivered_prev, reach_prev, accepted_prev = await _offers(
        session, since=prev_since, until=cur_since, campaign_ids=ids, deciles=deciles)
    spent_now, _ = await _accruals(
        session, since=cur_since, until=now, campaign_ids=ids, deciles=deciles)
    spent_prev, accruals_prev = await _accruals(
        session, since=prev_since, until=cur_since, campaign_ids=ids, deciles=deciles)

    ctr_now = accepted / delivered if delivered else 0.0
    ctr_prev = accepted_prev / delivered_prev if delivered_prev else 0.0
    avg_ctr = round(ctr_now, 6)
    # База тренда — события предыдущего окна: доставки для охвата и CTR,
    # начисления для расхода.
    trends = KpiTrends(
        reach=_trend(reach, reach_prev, delivered_prev, min_base),
        spent=_trend(spent_now, spent_prev, accruals_prev, min_base),
        ctr=_trend(ctr_now, ctr_prev, delivered_prev, min_base),
    )
    return KpiResponse(
        campaigns_count=len(campaigns),
        reach=reach, spent=spent, budget=budget, avg_ctr=avg_ctr,
        trends=trends, trend_window_days=period,
        has_data=(spent > 0 or avg_ctr > 0 or delivered > 0),
    )


# ---------------------------------------------------------------------------
# Segment × MCC matrix with CTR
# ---------------------------------------------------------------------------
@router.get("/segment-matrix", response_model=list[SegmentMatrixCell])
async def segment_matrix(
    campaign_id: uuid.UUID | None = Query(default=None),
    period: int = Query(default=30, ge=1, le=365),
    segment_id: str | None = Query(default=None, pattern=_SEGMENT_PATTERN),
    session: AsyncSession = Depends(get_session_dep),
) -> list[SegmentMatrixCell]:
    # campaign_id (этап D): матрица выбранной кампании — UI подписывает её
    # именем кампании, а без фильтра показывал бы офферы всех кампаний.
    cutoff = datetime.now(UTC) - timedelta(days=period)
    deciles = _deciles_for(segment_id)
    params: dict = {"cutoff": cutoff}
    filters = ""
    if deciles is not None:
        filters += " AND u.segment_id = ANY(:deciles)"
        params["deciles"] = deciles
    if campaign_id is not None:
        filters += " AND r.campaign_id = :campaign_id"
        params["campaign_id"] = campaign_id
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
           AND u.segment_id IS NOT NULL{filters}
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
# period — номер месяца от начала когорты: years*12 + months возраста (B33:
# date_part('month', age) сворачивал 12-й и дальше месяцы в 0–11). Окно —
# make_interval (B9: ``(:periods || ' months')::interval`` — int || text,
# asyncpg отвечал DataError → 500).
_COHORT_SQL = text(
    """
    WITH cohort AS (
        SELECT user_id
          FROM users
         WHERE date_trunc('month', created_at) =
               date_trunc('month', to_date(:cohort_month, 'YYYY-MM'))
    ),
    cohort_size AS (SELECT count(*) AS n FROM cohort),
    activity AS (
        SELECT (date_part('year', d.span) * 12 + date_part('month', d.span))::int
               AS period,
               COUNT(DISTINCT a.user_id) AS active_users
          FROM cashback_accruals a
          JOIN cohort c ON c.user_id = a.user_id
         CROSS JOIN LATERAL (
               SELECT age(date_trunc('month', a.accrued_at),
                          to_date(:cohort_month, 'YYYY-MM')) AS span
         ) d
         WHERE a.accrued_at >= to_date(:cohort_month, 'YYYY-MM')
           AND a.accrued_at <  to_date(:cohort_month, 'YYYY-MM')
                                + make_interval(months => :periods)
         GROUP BY 1
    )
    SELECT period, active_users,
           (SELECT n FROM cohort_size) AS cohort_n
      FROM activity
     ORDER BY period
    """
)


@router.get("/cohort-retention", response_model=list[CohortRetentionCell])
async def cohort_retention(
    cohort_month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    periods: int = Query(default=6, ge=1, le=24, description="months out"),
    session: AsyncSession = Depends(get_session_dep),
) -> list[CohortRetentionCell]:
    rows = (await session.execute(
        _COHORT_SQL, {"cohort_month": cohort_month, "periods": periods},
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
# За всё время, различные клиенты: ctr = принявшие / получившие,
# conversion_rate = купившие по принятому офферу / принявшие (B33: была
# копией ctr); cashback_paid — только выплаченные (PAID) начисления, как
# последняя стадия воронки; ROI — по всему начисленному кэшбэку.
_TOP_CAMPAIGNS_SQL = text(
    """
    WITH offers AS (
        SELECT r.campaign_id,
               COUNT(DISTINCT r.user_id) AS delivered,
               COUNT(DISTINCT r.user_id)
                   FILTER (WHERE r.response_status = 'ACCEPTED') AS accepted,
               COUNT(DISTINCT r.user_id)
                   FILTER (WHERE r.response_status = 'ACCEPTED'
                             AND a.accrual_id IS NOT NULL)      AS transacted
          FROM recommendations r
          LEFT JOIN cashback_accruals a ON a.recommendation_id = r.recommendation_id
         GROUP BY r.campaign_id
    ),
    accruals AS (
        SELECT campaign_id,
               SUM(transaction_amount) AS revenue,
               SUM(cashback_amount)    AS cashback_total,
               SUM(cashback_amount) FILTER (WHERE status = 'PAID') AS cashback_paid
          FROM cashback_accruals
         GROUP BY campaign_id
    )
    SELECT c.campaign_id::text            AS campaign_id,
           c.name                         AS name,
           COALESCE(o.delivered, 0)       AS delivered,
           COALESCE(o.accepted, 0)        AS accepted,
           COALESCE(o.transacted, 0)      AS transacted,
           COALESCE(a.revenue, 0)         AS revenue,
           COALESCE(a.cashback_total, 0)  AS cashback_total,
           COALESCE(a.cashback_paid, 0)   AS cashback_paid
      FROM cashback_campaigns c
      LEFT JOIN offers   o ON o.campaign_id = c.campaign_id
      LEFT JOIN accruals a ON a.campaign_id = c.campaign_id
     ORDER BY c.name, c.campaign_id
    """
)


@router.get("/top-campaigns", response_model=list[TopCampaignItem])
async def top_campaigns(
    metric: str = Query(default="roi",
                        pattern="^(roi|ctr|conversion_rate|cashback_paid)$"),
    limit: int = Query(default=5, ge=1, le=50),
    session: AsyncSession = Depends(get_session_dep),
) -> list[TopCampaignItem]:
    rows = (await session.execute(_TOP_CAMPAIGNS_SQL)).mappings().all()
    items: list[tuple[str, str, float]] = []
    for r in rows:
        delivered = int(r["delivered"])
        accepted = int(r["accepted"])
        transacted = int(r["transacted"])
        cashback_total = Decimal(r["cashback_total"] or 0)
        revenue = Decimal(r["revenue"] or 0)
        if metric == "ctr":
            value = (float(accepted) / delivered) if delivered else 0.0
        elif metric == "conversion_rate":
            value = (float(transacted) / accepted) if accepted else 0.0
        elif metric == "cashback_paid":
            value = float(r["cashback_paid"] or 0)
        else:  # roi
            value = float((revenue - cashback_total) / cashback_total) \
                if cashback_total > 0 else 0.0
        items.append((r["campaign_id"], r["name"], value))
    items.sort(key=lambda kv: kv[2], reverse=True)   # стабильно: равные — по имени
    return [
        TopCampaignItem(campaign_id=uuid.UUID(cid), name=name, metric=round(v, 4))
        for cid, name, v in items[:limit]
    ]


# ---------------------------------------------------------------------------
# Daily trend — принятые предложения по дням и сегментным корзинам (фаза 16).
# Источник: recommendations.responded_at (миграция 003), а не generated_at —
# кривая отражает момент реакции клиента, не момент генерации.
# Необязательные фильтры — CAST(:p AS …), а не :p::type (B9: text() не видит
# bind-параметр перед ``::``, в Postgres уходил буквальный «:campaign_id»).
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
       AND (CAST(:campaign_id AS uuid) IS NULL
            OR r.campaign_id = CAST(:campaign_id AS uuid))
       AND (CAST(:deciles AS int[]) IS NULL
            OR u.segment_id = ANY(CAST(:deciles AS int[])))
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
            {"cutoff": cutoff, "campaign_id": campaign_id,
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
# sent — все доставленные офферы канала (канал не записан → UNKNOWN), а не
# только ответившие (B33: open rate выходил 100% по построению); opened —
# клиент ответил; accepted — принял; converted — купил по принятому офферу.
# ---------------------------------------------------------------------------
_CHANNELS_SQL = text(
    """
    SELECT COALESCE(r.channel::text, 'UNKNOWN') AS channel,
           count(*)                             AS sent,
           count(*) FILTER (WHERE r.response_status IN ('ACCEPTED', 'DECLINED',
                                                        'SNOOZE')) AS opened,
           count(*) FILTER (WHERE r.response_status = 'ACCEPTED') AS accepted,
           count(*) FILTER (WHERE r.response_status = 'ACCEPTED'
                              AND a.accrual_id IS NOT NULL)      AS converted
      FROM recommendations r
      JOIN users u ON u.user_id = r.user_id
      LEFT JOIN cashback_accruals a ON a.recommendation_id = r.recommendation_id
     WHERE r.generated_at >= :cutoff
       AND (CAST(:campaign_id AS uuid) IS NULL
            OR r.campaign_id = CAST(:campaign_id AS uuid))
       AND (CAST(:deciles AS int[]) IS NULL
            OR u.segment_id = ANY(CAST(:deciles AS int[])))
     GROUP BY 1
     ORDER BY sent DESC, channel
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
            {"cutoff": cutoff, "campaign_id": campaign_id,
             "deciles": _deciles_for(segment_id)},
        )
    ).all()
    # pending (фаза 24): канал доставил, но откликов ещё нет.
    return [
        ChannelStats(channel=ch, sent=int(s), opened=int(o), accepted=int(a),
                     converted=int(c), pending=int(s) > 0 and int(o) == 0)
        for ch, s, o, a, c in rows
    ]
