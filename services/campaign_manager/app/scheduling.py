"""Background scheduler — auto-completes expired campaigns and pauses
those that have spent their allocated daily budget.

Runs via APScheduler's ``AsyncIOScheduler`` every
``SCHEDULING_INTERVAL_MINUTES`` (default 15).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

log = structlog.get_logger("scheduling")


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
                    SELECT budget_total, end_date, status
                      FROM cashback_campaigns
                     WHERE campaign_id = :cid
                       AND status = 'ACTIVE'
                """),
                {"cid": str(cid)},
            )).first()
            if row is None:
                continue
            days_left = max(
                (row.end_date - datetime.now(timezone.utc)).days, 1
            )
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
        interval_minutes: int = 15,
        threshold_ratio: float = 1.0,
    ) -> None:
        self._db = db_engine
        self._ch = ch_client
        self._interval = interval_minutes
        self._threshold = threshold_ratio
        self._scheduler: AsyncIOScheduler | None = None

    async def _tick(self) -> None:
        try:
            await complete_expired_campaigns(self._db)
            await pause_overspent_campaigns(self._db, self._ch, self._threshold)
        except Exception as exc:  # noqa: BLE001
            log.warning("scheduler_tick_failed", error=str(exc))

    def start(self) -> None:
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            self._tick, "interval", minutes=self._interval,
            id="campaign-housekeeping", replace_existing=True,
        )
        scheduler.start()
        self._scheduler = scheduler
        log.info("scheduler_started", interval_minutes=self._interval)

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
