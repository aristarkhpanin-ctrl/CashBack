"""RFM feature computation — chapter 3.1, listing 3.6."""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

import structlog

log = structlog.get_logger("etl.feature_eng")


# Top-50 MCC codes — must match infrastructure/clickhouse/migrations/002
# (otherwise insert into user_rfm_features fails on column mismatch).
TOP50_MCC: tuple[str, ...] = (
    "5411", "5812", "5814", "5541", "5499", "5912", "5311", "5651",
    "5732", "5942", "5921", "5462", "4111", "4121", "4131", "4814",
    "4829", "4900", "7832", "7011", "7299", "7211", "7230", "7538",
    "7995", "8011", "8021", "8062", "8099", "8211", "8398", "8999",
    "5993", "5995", "5970", "5945", "5947", "5946", "5641", "5611",
    "5621", "5712", "5722", "5933", "5200", "5599", "5331", "5300",
    "4511", "5691",
)
assert len(TOP50_MCC) == 50 and len(set(TOP50_MCC)) == 50


def _freq_cols(codes: Iterable[str]) -> str:
    return ",\n    ".join(
        f"countIf(mcc_code = '{m}') AS freq_{m}" for m in codes
    )


def _amt_cols(codes: Iterable[str]) -> str:
    return ",\n    ".join(
        f"sumIf(amount, mcc_code = '{m}') AS amt_{m}" for m in codes
    )


RFM_QUERY: str = f"""
SELECT
    user_id,
    dateDiff('day', max(transaction_date), now())                       AS recency_days,
    count()                                                             AS frequency_total,
    sum(amount)                                                         AS monetary_total,
    avg(amount)                                                         AS avg_ticket,
    avg(toDayOfWeek(transaction_date) >= 6)                             AS weekend_ratio,
    avg(toHour(transaction_date) BETWEEN 18 AND 21)                     AS evening_ratio,
    uniq(mcc_code)                                                      AS distinct_mcc_count,
    {_freq_cols(TOP50_MCC)},
    {_amt_cols(TOP50_MCC)},
    dateDiff('month', min(transaction_date), now())                     AS tenure_months
FROM transactions_raw
WHERE transaction_date >= now() - INTERVAL {{window_days}} DAY
GROUP BY user_id
""".strip()


class RFMComputer:
    """Materialise the RFM feature store from ClickHouse, mirror to Redis."""

    FEATURE_TABLE: str = "user_rfm_features"
    REDIS_KEY_FMT: str = "features:{user_id}"

    def __init__(
        self,
        ch_client: Any,
        redis_client: Any,
        *,
        ttl_seconds: int = 3600,
    ) -> None:
        self._ch = ch_client
        self._redis = redis_client
        self._ttl = ttl_seconds

    # ------------------------------------------------------------------
    def _build_query(self, window_days: int) -> str:
        if window_days <= 0:
            raise ValueError("window_days must be > 0")
        return RFM_QUERY.format(window_days=window_days)

    def run(self, window_days: int = 90) -> int:
        """Compute RFM features over ``window_days`` and persist them.

        Returns
        -------
        int
            Number of user rows materialised.
        """
        sql = self._build_query(window_days)
        log.info("rfm_query_start", window_days=window_days)
        df = self._ch.query_df(sql)
        if df.empty:
            log.info("rfm_no_data")
            return 0

        # ClickHouse returns Decimal/float; null-fills keep the schema strict.
        numeric_cols = [c for c in df.columns if c != "user_id"]
        df[numeric_cols] = df[numeric_cols].fillna(0)

        # 1) materialise into ClickHouse (ReplacingMergeTree dedups by user_id).
        self._ch.insert_df(self.FEATURE_TABLE, df)
        log.info("rfm_inserted_clickhouse", rows=len(df), table=self.FEATURE_TABLE)

        # 2) push to Redis with TTL via a pipeline.
        wrote = self._push_to_redis(df)
        log.info("rfm_pushed_redis", keys=wrote, ttl=self._ttl)
        return len(df)

    def _push_to_redis(self, df) -> int:
        pipe = self._redis.pipeline()
        wrote = 0
        for record in df.to_dict(orient="records"):
            user_id = str(record["user_id"])
            payload = json.dumps(record, default=str, separators=(",", ":"))
            key = self.REDIS_KEY_FMT.format(user_id=user_id)
            pipe.set(key, payload, ex=self._ttl)
            wrote += 1
        pipe.execute()
        return wrote
