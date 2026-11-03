"""Background scheduler — independent housekeeping jobs.

* ``complete-expired-campaigns`` — ACTIVE/PAUSED campaigns past ``end_date``
  become COMPLETED (compare-and-set in the UPDATE itself, B38);
* ``settle-accruals`` — PENDING accruals older than
  ``SETTLEMENT_DELAY_HOURS`` (default 24) become PAID with ``paid_at``: the
  model of the bank's settlement hold (B33: nothing ever set PAID, the
  funnel's last stage and mobile_api's "paid" balance stayed 0);
* ``export-budget-metrics`` / ``export-online-ctr`` — Prometheus gauges;
* ``republish-ml-limits`` — the ML-limits snapshot for BRE rule R7, every
  ``ML_LIMITS_REPUBLISH_SECONDS`` (B39: a failed publish after
  ``PUT /ml-limits`` is retried here, whatever the other jobs do).

Each job runs in its own try: one failing job neither stops the others nor
the scheduler, and every failure increments its own series
``scheduler_job_failures_total{job}``. All but the last run every
``SCHEDULING_INTERVAL_MINUTES`` (default 15).

There is no daily-limit pause any more (B22): ``daily_limit`` is enforced by
``eligible_campaigns`` and the accrual transaction — the campaign stops
paying for the rest of the UTC day, its status does not change, so nothing
has to resume it the next day.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from prometheus_client import Counter, Gauge
from sqlalchemy import text

log = structlog.get_logger("scheduling")

ML_LIMITS_REPUBLISH_SECONDS = 60

# Сбои фоновых задач по имени задачи (B39: раньше сбой любой задачи молча
# обрывал весь тик, в том числе переиздание снапшота ML-лимитов).
SCHEDULER_JOB_FAILURES = Counter(
    "scheduler_job_failures",
    "Failed runs of campaign_manager scheduler jobs",
    ["job"],
)

# Exported for Prometheus alerting (CampaignBudgetNearlyExhausted):
# ratio 0..1 per ACTIVE campaign, refreshed every scheduler tick.
BUDGET_UTILIZATION = Gauge(
    "campaign_budget_utilization_ratio",
    "budget_spent / budget_total for ACTIVE campaigns",
    ["campaign_id", "name"],
)

# Онлайн-качество продовой модели (фаза 18): доля принятых среди
# ответивших (ACCEPTED/DECLINED/SNOOZE — как «открыто» в аналитике, этап D;
# EXPIRED не ответ) за последние 7 дней, по версии модели из MLflow.
# Версия пишется в recommendations.model_version (миграция 005).
ML_ONLINE_CTR = Gauge(
    "ml_online_ctr",
    "accepted / responded recommendations over the last 7 days",
    ["model_version"],
)


# Compare-and-set: условие на статус — в самом UPDATE, под блокировкой строки;
# кампания, которую тем временем поставили на паузу или завершили через API,
# получает переход только из того статуса, в котором она сейчас (B38).
# end_date <= now(): кампания перестаёт быть положенной ровно в end_date
# (eligible_campaigns: p_at < end_date) — тогда же её и завершаем. Архивную
# не трогаем: статус архивной кампании не меняется (как и через API).
COMPLETE_EXPIRED_SQL = text(
    """
    UPDATE cashback_campaigns
       SET status = 'COMPLETED'
     WHERE status IN ('ACTIVE', 'PAUSED')
       AND archived_at IS NULL
       AND end_date <= now()
    RETURNING campaign_id
    """
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
             / NULLIF(count(*) FILTER (WHERE response_status IN ('ACCEPTED',
                                                                 'DECLINED',
                                                                 'SNOOZE')), 0)
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
    """Переиздать снапшот ml_limits в Redis: самовосстановление после
    рестарта Redis и после ``PUT /ml-limits``, ответившего 503 (B39)."""
    if redis_client is None:
        return
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.ml_limits import publish_snapshot

    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        await publish_snapshot(redis_client, session)


# Расчёт с банком (B33): начисление выплачено, когда прошёл холд. Сумма не
# меняется — budget_spent и инварианты этапа C не затрагиваются; баланс
# mobile_api (pending / paid) следует за статусом сам.
SETTLE_ACCRUALS_SQL = text(
    """
    UPDATE cashback_accruals
       SET status = 'PAID', paid_at = now()
     WHERE status = 'PENDING'
       AND accrued_at < now() - make_interval(hours => :delay_hours)
    """
)


async def settle_accruals(db_engine: Any, delay_hours: int) -> int:
    """PENDING старше ``delay_hours`` → PAID; число выплаченных начислений."""
    async with db_engine.begin() as conn:
        result = await conn.execute(SETTLE_ACCRUALS_SQL, {"delay_hours": delay_hours})
    if result.rowcount:
        log.info("settled_accruals", count=result.rowcount, delay_hours=delay_hours)
    return result.rowcount


async def complete_expired_campaigns(db_engine: Any) -> int:
    async with db_engine.begin() as conn:
        result = await conn.execute(COMPLETE_EXPIRED_SQL)
        rows = result.fetchall()
    if rows:
        log.info("auto_completed_campaigns", count=len(rows))
    return len(rows)


async def run_job(name: str, job: Callable[[], Awaitable[Any]]) -> None:
    """Выполнить одну задачу; сбой — в лог и в счётчик, но не дальше."""
    try:
        await job()
    except Exception as exc:  # noqa: BLE001
        SCHEDULER_JOB_FAILURES.labels(job=name).inc()
        log.warning("scheduler_job_failed", job=name, error=str(exc))


# ---------------------------------------------------------------------------
class CampaignScheduler:
    def __init__(
        self,
        db_engine: Any,
        *,
        redis_client: Any = None,
        interval_minutes: int = 15,
        republish_seconds: int = ML_LIMITS_REPUBLISH_SECONDS,
        settlement_delay_hours: int = 24,
    ) -> None:
        self._db = db_engine
        self._redis = redis_client
        self._interval = interval_minutes
        self._republish = republish_seconds
        self._settlement_delay = settlement_delay_hours
        self._scheduler: AsyncIOScheduler | None = None

    def jobs(self) -> list[tuple[str, Callable[[], Awaitable[Any]], dict[str, int]]]:
        """(имя, задача, интервал) — каждая задача планируется отдельно."""
        every = {"minutes": self._interval}
        return [
            ("complete-expired-campaigns",
             lambda: complete_expired_campaigns(self._db), every),
            ("settle-accruals",
             lambda: settle_accruals(self._db, self._settlement_delay), every),
            ("export-budget-metrics", lambda: export_budget_metrics(self._db), every),
            ("export-online-ctr", lambda: export_online_ctr(self._db), every),
            ("republish-ml-limits",
             lambda: republish_ml_limits_snapshot(self._db, self._redis),
             {"seconds": self._republish}),
        ]

    def start(self) -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        now = datetime.now(UTC)          # первый прогон каждой задачи — сразу
        for name, job, every in self.jobs():
            SCHEDULER_JOB_FAILURES.labels(job=name)     # серия с нуля в /metrics
            scheduler.add_job(
                run_job, "interval", args=(name, job), id=name,
                replace_existing=True, next_run_time=now,
                max_instances=1, coalesce=True, **every,
            )
        scheduler.start()
        self._scheduler = scheduler
        log.info("scheduler_started", interval_minutes=self._interval,
                 republish_seconds=self._republish,
                 settlement_delay_hours=self._settlement_delay)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
