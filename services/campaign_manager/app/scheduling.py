"""Background scheduler — auto-completes expired campaigns and pauses
those that have spent their allocated daily budget.

Runs via APScheduler's ``AsyncIOScheduler`` every
``SCHEDULING_INTERVAL_MINUTES`` (default 15).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timezone
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Gauge
from sqlalchemy import text

log = structlog.get_logger("scheduling")

# Exported for Prometheus alerting (CampaignBudgetNearlyExhausted):
# ratio 0..1 per ACTIVE campaign, refreshed every scheduler tick.
BUDGET_UTILIZATION = Gauge(
    "campaign_budget_utilization_ratio",
    "budget_spent / budget_total for ACTIVE campaigns",
    ["campaign_id", "name"],
)

# Онлайн-качество продовой модели (фаза 18): доля принятых среди
# отреагировавших за последние 7 дней, по версии модели из MLflow.
# Версия пишется в recommendations.model_version (миграция 005).
ML_ONLINE_CTR = Gauge(
    "ml_online_ctr",
    "accepted / responded recommendations over the last 7 days",
    ["model_version"],
)


COMPLETE_EXPIRED_SQL = text(
    """
    UPDATE cashback_campaigns
       SET status = 'COMPLETED'
     WHERE status IN ('ACTIVE', 'PAUSED')
       AND end_date < now()
    RETURNING campaign_id
    """
)

# Pause campaigns that spent the daily budget cap (computed in CH).
DAILY_SPENT_SQL = (
    "SELECT campaign_id, sum(cashback_amount) AS spent_today "
    "FROM cashback_accruals "
    "WHERE accrued_at >= toStartOfDay(now()) "
    "GROUP BY campaign_id"
)


BUDGET_EXPORT_SQL = text(
    """
    SELECT campaign_id::text AS campaign_id,
           name,
           budget_total,
           budget_spent
      FROM cashback_campaigns
     WHERE status = 'ACTIVE'
    """
)


async def export_budget_metrics(db_engine: Any) -> int:
    """Refresh the budget-utilization gauge for all ACTIVE campaigns.

    Labels are cleared first so campaigns that finished (or were paused)
    disappear from the exposition instead of freezing at the last value.
    """
    async with db_engine.connect() as conn:
        rows = (await conn.execute(BUDGET_EXPORT_SQL)).fetchall()

    BUDGET_UTILIZATION.clear()
    for row in rows:
        total = float(row.budget_total or 0)
        ratio = float(row.budget_spent or 0) / total if total > 0 else 0.0
        BUDGET_UTILIZATION.labels(
            campaign_id=row.campaign_id, name=row.name,
        ).set(round(ratio, 4))
    return len(rows)


_ONLINE_CTR_SQL = text(
    """
    SELECT model_version,
           count(*) FILTER (WHERE response_status = 'ACCEPTED')::float
             / NULLIF(count(*) FILTER (WHERE response_status <> 'PENDING'), 0)
             AS ctr
      FROM recommendations
     WHERE generated_at >= now() - interval '7 days'
       AND model_version IS NOT NULL
     GROUP BY model_version
    """
)


async def export_online_ctr(db_engine: Any) -> int:
    """Онлайн-CTR по версиям модели за 7 дней → gauge ml_online_ctr."""
    async with db_engine.connect() as conn:
        rows = (await conn.execute(_ONLINE_CTR_SQL)).fetchall()
    ML_ONLINE_CTR.clear()
    n = 0
    for row in rows:
        if row.ctr is None:
            continue
        ML_ONLINE_CTR.labels(model_version=str(row.model_version)).set(
            round(float(row.ctr), 6))
        n += 1
    return n


async def republish_ml_limits_snapshot(db_engine: Any, redis_client: Any) -> None:
    """Переиздать снапшот ml_limits в Redis (self-healing после рестарта)."""
    if redis_client is None:
        return
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.ml_limits import publish_snapshot

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await publish_snapshot(redis_client, session)


async def complete_expired_campaigns(db_engine: Any) -> int:
    async with db_engine.begin() as conn:
        result = await conn.execute(COMPLETE_EXPIRED_SQL)
        rows = result.fetchall()
    if rows:
        log.info("auto_completed_campaigns", count=len(rows))
    return len(rows)


async def pause_overspent_campaigns(
    db_engine: Any, ch_client: Any, threshold_ratio: float = 1.0,
) -> int:
    """Pause campaigns whose today's accruals reached the daily threshold.

    The daily cap is approximated as ``budget_total / max(days_remaining, 1)``;
    pause when today's accruals * (1/threshold_ratio) ≥ that cap.
    """
    if ch_client is None:
        return 0

    def _query():
        return ch_client.query(DAILY_SPENT_SQL)
    try:
        result = await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        log.warning("ch_query_failed", error=str(exc))
        return 0

    if not result.result_rows:
        return 0

    paused = 0
    async with db_engine.begin() as conn:
        for cid, spent_today in result.result_rows:
            row = (await conn.execute(
                text("""
                    SELECT budget_total, end_date, status,
                           daily_limit, auto_pause
                      FROM cashback_campaigns
                     WHERE campaign_id = :cid
                       AND status = 'ACTIVE'
                """),
                {"cid": str(cid)},
            )).first()
            if row is None:
                continue
            # Фаза 22: кампания с выключенной авто-приостановкой не паузится.
            if not row.auto_pause:
                continue
            # Явный дневной лимит (фаза 22) — приоритетнее выведенного из бюджета.
            if row.daily_limit is not None:
                daily_cap = float(row.daily_limit)
            else:
                days_left = max((row.end_date - datetime.now(UTC)).days, 1)
                daily_cap = float(row.budget_total) / float(days_left)
            if float(spent_today) >= daily_cap * threshold_ratio:
                await conn.execute(
                    text("""
                        UPDATE cashback_campaigns
                           SET status = 'PAUSED'
                         WHERE campaign_id = :cid
                    """),
                    {"cid": str(cid)},
                )
                paused += 1
    if paused:
        log.info("auto_paused_campaigns", count=paused)
    return paused


# ---------------------------------------------------------------------------
class CampaignScheduler:
    def __init__(
        self,
        db_engine: Any,
        ch_client: Any,
        *,
        redis_client: Any = None,
        interval_minutes: int = 15,
        threshold_ratio: float = 1.0,
    ) -> None:
        self._db = db_engine
        self._ch = ch_client
        self._redis = redis_client
        self._interval = interval_minutes
        self._threshold = threshold_ratio
        self._scheduler: AsyncIOScheduler | None = None

    async def _tick(self) -> None:
        try:
            await complete_expired_campaigns(self._db)
            await pause_overspent_campaigns(self._db, self._ch, self._threshold)
            await export_budget_metrics(self._db)
            await export_online_ctr(self._db)
            await republish_ml_limits_snapshot(self._db, self._redis)
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler_tick_failed", error=str(exc))

    def start(self) -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            self._tick, "interval", minutes=self._interval,
            id="campaign-housekeeping", replace_existing=True,
            next_run_time=datetime.now(UTC),  # первый экспорт метрик сразу
        )
        scheduler.start()
        self._scheduler = scheduler
        log.info("scheduler_started", interval_minutes=self._interval)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
