"""Optimal Send Time (OST).

Stores per-user activity histograms in Redis (key ``ost:{user_id}``) and
defers notifications until the next "active hour" when sent outside of
the user's typical engagement window.

There are two collaborators here:

    * :class:`OSTUpdater` — invoked from an Airflow DAG, accepts a
      24-element histogram and persists it.
    * :class:`DeliveryScheduler` — used by the live notification
      consumer to decide whether to deliver *now* or defer.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import structlog

log = structlog.get_logger("ost")


class OSTUpdater:
    """Persists activity histograms used by the live scheduler."""

    DEFAULT_TTL_SECONDS: int = 30 * 24 * 3600

    def __init__(self, redis_client: Any, key_fmt: str = "ost:{user_id}") -> None:
        self._redis = redis_client
        self._key_fmt = key_fmt

    async def update(
        self,
        user_id: str,
        hourly_counts: Iterable[int],
        ttl_seconds: int | None = None,
    ) -> None:
        hours = list(hourly_counts)
        if len(hours) != 24:
            raise ValueError("hourly_counts must have exactly 24 entries")
        payload = json.dumps({"hours": hours,
                              "updated_at": datetime.now(UTC).isoformat()})
        await self._redis.set(
            self._key_fmt.format(user_id=user_id),
            payload,
            ex=int(ttl_seconds or self.DEFAULT_TTL_SECONDS),
        )
        log.info("ost_updated", user_id=user_id)


class DeliveryScheduler:
    """Decides when to deliver a notification given the user's OST."""

    def __init__(
        self,
        redis_client: Any,
        *,
        key_fmt: str = "ost:{user_id}",
        default_active_hours: Iterable[int] = tuple(range(8, 22)),
        max_defer_hours: int = 18,
    ) -> None:
        self._redis = redis_client
        self._key_fmt = key_fmt
        self._default_hours = set(int(h) for h in default_active_hours)
        self._max_defer = max(1, int(max_defer_hours))

    async def _load_active_hours(self, user_id: str) -> set[int]:
        try:
            raw = await self._redis.get(self._key_fmt.format(user_id=user_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("ost_lookup_failed", user_id=user_id, error=str(exc))
            return self._default_hours

        if not raw:
            return self._default_hours
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return self._default_hours

        hours = payload.get("hours") or []
        if not hours or sum(hours) == 0:
            return self._default_hours
        # Treat the upper-half hours (above mean) as "active".
        avg = sum(hours) / len(hours)
        return {h for h, c in enumerate(hours) if c > avg}

    async def next_send_time(
        self,
        user_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> datetime:
        now = now or datetime.now(UTC)
        active = await self._load_active_hours(user_id)
        if not active or now.hour in active:
            return now

        for offset in range(1, self._max_defer + 1):
            candidate = (now + timedelta(hours=offset)).replace(
                minute=0, second=0, microsecond=0
            )
            if candidate.hour in active:
                return candidate
        # Failed to find a window within the defer budget — send now.
        return now

    async def should_deliver_now(
        self,
        user_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        now = now or datetime.now(UTC)
        active = await self._load_active_hours(user_id)
        return not active or now.hour in active
