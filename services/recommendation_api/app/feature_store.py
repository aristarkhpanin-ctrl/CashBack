"""FeatureStoreClient — Redis hot path with ClickHouse fallback."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)


_CH_FALLBACK_SQL = (
    "SELECT * FROM user_rfm_features "
    "WHERE user_id = {user_id:String} "
    "ORDER BY computed_at DESC LIMIT 1"
)


class FeatureStoreClient:
    """Lookup feature vector for a single user.

    1. Try ``features:{user_id}`` in Redis (sub-millisecond hit path).
    2. On cache miss, query ClickHouse with a hard ``timeout_ms`` budget.
    3. Both unavailable → ``None``.
    """

    def __init__(
        self,
        redis_client: Any,
        ch_client: Any,
        *,
        timeout_ms: int = 200,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self._redis = redis_client
        self._ch = ch_client
        self._timeout = timeout_ms / 1000.0
        self._cache_ttl = cache_ttl_seconds

    # ------------------------------------------------------------------
    @staticmethod
    def _key(user_id: str) -> str:
        return f"features:{user_id}"

    async def _from_redis(self, user_id: str) -> dict | None:
        try:
            payload = await asyncio.wait_for(
                self._redis.get(self._key(user_id)),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            log.warning("redis_timeout user=%s", user_id)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_error user=%s: %s", user_id, exc)
            return None

        if not payload:
            return None
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            log.warning("redis_payload_corrupt user=%s", user_id)
            return None

    async def _from_clickhouse(self, user_id: str) -> dict | None:
        if self._ch is None:
            return None

        def _query() -> dict | None:
            res = self._ch.query(_CH_FALLBACK_SQL, parameters={"user_id": user_id})
            if not res.result_rows:
                return None
            return dict(zip(res.column_names, res.result_rows[0]))

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_query), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            log.warning("clickhouse_timeout user=%s", user_id)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("clickhouse_error user=%s: %s", user_id, exc)
            return None

    # ------------------------------------------------------------------
    async def get(self, user_id: str) -> dict | None:
        cached = await self._from_redis(user_id)
        if cached is not None:
            return cached

        fresh = await self._from_clickhouse(user_id)
        if fresh is None:
            return None

        # Warm Redis on the miss path so subsequent hits are O(1).
        try:
            await self._redis.setex(
                self._key(user_id),
                self._cache_ttl,
                json.dumps(fresh, default=str, separators=(",", ":")),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_warmup_failed user=%s: %s", user_id, exc)
        return fresh
