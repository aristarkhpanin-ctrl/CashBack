"""Build the training dataset by joining CH RFM features with PG recommendations."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import structlog

log = structlog.get_logger("ml.data_loader")


# All recommendation rows whose status is one of these are training-eligible
# (PENDING is excluded because the label is unknown).
TERMINAL_STATUSES: tuple[str, ...] = ("ACCEPTED", "DECLINED", "EXPIRED", "SNOOZE")


# Pulled from PostgreSQL: one row per (user_id, recommendation), with the
# recommendation's terminal response_status as label.
PG_RECOMMENDATIONS_SQL = """
    SELECT
        recommendation_id::text   AS recommendation_id,
        user_id::text             AS user_id,
        campaign_id::text         AS campaign_id,
        mcc_code,
        model_score,
        EXTRACT(EPOCH FROM (now() - generated_at))::float AS age_seconds,
        response_status::text     AS response_status
      FROM recommendations
     WHERE response_status = ANY(%s)
       AND generated_at >= now() - INTERVAL '180 days'
"""


# Pulled from ClickHouse: latest computed RFM features per user.
CH_RFM_SQL = """
    SELECT *
      FROM user_rfm_features
     WHERE computed_at >= now() - INTERVAL {window_days} DAY
"""


class TrainingDataLoader:
    """Materialises ``X`` (RFM features + recommendation context) and ``y``
    (binary accepted label) from ClickHouse + PostgreSQL.
    """

    def __init__(
        self,
        ch_client: Any,
        pg_dsn: str,
        *,
        rfm_window_days: int = 90,
        terminal_statuses: tuple[str, ...] = TERMINAL_STATUSES,
    ) -> None:
        self._ch = ch_client
        self._pg_dsn = pg_dsn
        self._rfm_window_days = rfm_window_days
        self._terminal_statuses = terminal_statuses

    # ------------------------------------------------------------------
    def _load_recommendations(self) -> pd.DataFrame:
        import psycopg2  # local import to keep top-level import light

        with psycopg2.connect(self._pg_dsn) as conn:
            df = pd.read_sql(
                PG_RECOMMENDATIONS_SQL,
                conn,
                params=(list(self._terminal_statuses),),
            )
        log.info("loaded_recommendations", rows=len(df))
        return df

    def _load_rfm(self) -> pd.DataFrame:
        sql = CH_RFM_SQL.format(window_days=self._rfm_window_days)
        df = self._ch.query_df(sql)
        if df is None or df.empty:
            log.warning("rfm_table_empty")
            return pd.DataFrame()
        # Keep the most recent row per user (ReplacingMergeTree may still
        # have duplicates before a merge runs).
        df = df.sort_values("computed_at").drop_duplicates(
            subset=["user_id"], keep="last"
        )
        df["user_id"] = df["user_id"].astype(str)
        log.info("loaded_rfm_features", rows=len(df), cols=df.shape[1])
        return df

    # ------------------------------------------------------------------
    def build_pairs(self) -> tuple[pd.DataFrame, pd.Series]:
        """Return ``(X, y)`` ready for LGBMTrainer.train()."""
        recs = self._load_recommendations()
        rfm = self._load_rfm()

        if recs.empty:
            raise RuntimeError(
                "no terminal-status recommendations found — "
                "run scripts/seed_recommendations.py first"
            )
        if rfm.empty:
            raise RuntimeError(
                "user_rfm_features is empty — run the daily ETL first"
            )

        merged = recs.merge(rfm, on="user_id", how="inner")
        if merged.empty:
            raise RuntimeError(
                "no users with both recommendations and RFM features — "
                "did you run seed-history → trigger-etl → seed-recommendations?"
            )
        log.info("merged_dataset", rows=len(merged), cols=merged.shape[1])

        merged["accepted"] = (merged["response_status"] == "ACCEPTED").astype(int)

        # Hash mcc_code into an integer for tree-based models.
        merged["mcc_code"] = merged["mcc_code"].astype(str).str.zfill(4)
        merged["mcc_code_int"] = (
            merged["mcc_code"].str.replace(r"\D", "0", regex=True).astype(int)
        )

        drop_cols = {
            "recommendation_id", "campaign_id", "mcc_code",
            "response_status", "user_id", "computed_at", "accepted",
        }
        feature_cols = [c for c in merged.columns if c not in drop_cols]

        X = merged[feature_cols].copy()
        # Convert numeric-like columns to float; coerce errors to 0.
        for c in X.columns:
            X[c] = pd.to_numeric(X[c], errors="coerce")
        X = X.fillna(0.0).astype("float32")

        y = merged["accepted"].astype(int)
        log.info(
            "built_pairs",
            rows=len(X), features=X.shape[1],
            positive_rate=float(y.mean()),
        )
        return X, y

    # ------------------------------------------------------------------
    def load_rfm_for_svd(self) -> pd.DataFrame:
        """Loader used by SVD++; returns only the RFM rows."""
        return self._load_rfm()
