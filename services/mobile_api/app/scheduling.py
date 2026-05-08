"""APScheduler wrapper for SNOOZE deferred jobs.

When the user picks SNOOZE on a recommendation we re-surface it 3 days
later (see :data:`Settings.snooze_days`). The actual reactivation logic
just resets the response_status back to ``PENDING`` so the next
recommendation request can return it again.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

log = structlog.get_logger("mobile.scheduling")


class SnoozeScheduler:
    def __init__(self, db_engine: Any) -> None:
        self._db = db_engine
        self._scheduler: AsyncIOScheduler | None = None

    def start(self) -> None:
        sched = AsyncIOScheduler(timezone="UTC")
        sched.start()
        self._scheduler = sched
        log.info("snooze_scheduler_started")

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def schedule(self, recommendation_id: str, days: int) -> datetime:
        run_at = datetime.now(UTC) + timedelta(days=days)
        if self._scheduler is None:
            log.warning("scheduler_not_running_executing_inline",
                        rec_id=recommendation_id)
            return run_at
        self._scheduler.add_job(
            self._reactivate,
            trigger="date",
            run_date=run_at,
            args=[recommendation_id],
            id=f"snooze:{recommendation_id}",
            replace_existing=True,
        )
        log.info("snooze_scheduled", rec_id=recommendation_id, run_at=str(run_at))
        return run_at

    async def _reactivate(self, recommendation_id: str) -> None:
        sql = text(
            """
            UPDATE recommendations
               SET response_status = 'PENDING'
             WHERE recommendation_id = :rid
               AND response_status   = 'SNOOZE'
            """
        )
        try:
            async with self._db.begin() as conn:
                await conn.execute(sql, {"rid": recommendation_id})
            log.info("snooze_reactivated", rec_id=recommendation_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("snooze_reactivate_failed",
                        rec_id=recommendation_id, error=str(exc))
