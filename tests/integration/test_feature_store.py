"""Integration: FeatureStoreClient against a real Redis (testcontainers).

Six tests covering: cache hit, cache miss + ClickHouse fallback, ClickHouse
miss, ClickHouse timeout, payload corruption, and warm-up after fallback.

The ClickHouse fallback uses an in-memory stub — the goal here is to
validate the *Redis* hot-path semantics without spinning up ClickHouse.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Use the recommendation_api `app` namespace.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "recommendation_api"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration


@pytest.fixture()
async def redis_client(redis_url):
    pytest.importorskip("redis")
    from redis.asyncio import Redis
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    # Flush between tests so they don't leak.
    try:
        await client.flushdb()
        await client.aclose()
    except Exception:
        pass


@pytest.fixture()
def feature_store(redis_client):
    from app.feature_store import FeatureStoreClient
    return FeatureStoreClient(
        redis_client=redis_client, ch_client=None, timeout_ms=200,
    )


def _payload(user_id: str) -> dict[str, Any]:
    return {
        "user_id": user_id, "segment_id": 5,
        "recency_days": 1, "frequency_total": 30,
        "monetary_total": 50000.0, "avg_ticket": 1500.0,
        "weekend_ratio": 0.4, "evening_ratio": 0.6,
    }


# ---------------------------------------------------------------------------
async def test_redis_cache_hit_returns_payload(redis_client, feature_store):
    user_id = "u-cache-hit"
    await redis_client.set(f"features:{user_id}", json.dumps(_payload(user_id)))
    out = await feature_store.get(user_id)
    assert out is not None
    assert out["user_id"] == user_id
    assert out["segment_id"] == 5


async def test_redis_cache_miss_returns_none_when_no_ch_fallback(feature_store):
    out = await feature_store.get("u-not-cached")
    assert out is None


async def test_clickhouse_fallback_warms_redis(redis_client):
    """When CH returns a row, the result is written back to Redis."""
    from app.feature_store import FeatureStoreClient

    class _Result:
        def __init__(self, columns, row):
            self.column_names = columns
            self.result_rows = [row]

    class _CHStub:
        def query(self, sql, parameters=None):  # noqa: ARG002
            return _Result(
                ["user_id", "segment_id", "frequency_total"],
                ["u-fallback", 7, 42],
            )

    fs = FeatureStoreClient(
        redis_client=redis_client, ch_client=_CHStub(), timeout_ms=500,
    )
    out = await fs.get("u-fallback")
    assert out is not None
    assert out["segment_id"] == 7
    # And Redis is now warmed.
    cached = await redis_client.get("features:u-fallback")
    assert cached is not None


async def test_clickhouse_no_rows_returns_none(redis_client):
    from app.feature_store import FeatureStoreClient

    class _Result:
        column_names = ["user_id"]
        result_rows = []

    class _CHStub:
        def query(self, sql, parameters=None):  # noqa: ARG002
            return _Result()

    fs = FeatureStoreClient(redis_client=redis_client, ch_client=_CHStub())
    out = await fs.get("u-empty")
    assert out is None


async def test_redis_payload_corruption_returns_none(redis_client, feature_store):
    """Garbage in Redis must not crash the lookup."""
    await redis_client.set("features:u-corrupt", "{not-json}")
    out = await feature_store.get("u-corrupt")
    assert out is None


async def test_clickhouse_timeout_returns_none(redis_client):
    """Slow CH client must hit the timeout budget and return None."""
    from app.feature_store import FeatureStoreClient

    class _SlowCH:
        def query(self, sql, parameters=None):  # noqa: ARG002
            import time
            time.sleep(1.0)   # exceeds the 100 ms timeout below
            return SimpleNamespace(column_names=[], result_rows=[])

    fs = FeatureStoreClient(
        redis_client=redis_client, ch_client=_SlowCH(), timeout_ms=100,
    )
    out = await asyncio.wait_for(fs.get("u-slow"), timeout=2.0)
    assert out is None
