"""Centralised configuration for the ML training service."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mlflow_tracking_uri: str
    clickhouse_host: str
    clickhouse_http_port: int
    clickhouse_user: str
    clickhouse_password: str
    clickhouse_db: str
    postgres_dsn: str
    rfm_window_days: int
    svd_factors: int
    svd_iterations: int
    artifact_dir: str


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def get_settings() -> Settings:
    return Settings(
        mlflow_tracking_uri=_env("MLFLOW_TRACKING_URI", "http://mlflow:5000"),
        clickhouse_host=_env("CLICKHOUSE_HOST", "clickhouse"),
        clickhouse_http_port=int(_env("CLICKHOUSE_HTTP_PORT", "8123")),
        clickhouse_user=_env("CLICKHOUSE_USER", "cashback"),
        clickhouse_password=_env("CLICKHOUSE_PASSWORD", "cashback"),
        clickhouse_db=_env("CLICKHOUSE_DB", "cashback"),
        postgres_dsn=_env(
            "POSTGRES_DSN",
            "postgresql://cashback:cashback@postgres:5432/cashback",
        ),
        rfm_window_days=int(_env("RFM_WINDOW_DAYS", "90")),
        svd_factors=int(_env("SVD_FACTORS", "64")),
        svd_iterations=int(_env("SVD_ITERATIONS", "15")),
        artifact_dir=_env("ARTIFACT_DIR", "/data/ml-artifacts"),
    )
