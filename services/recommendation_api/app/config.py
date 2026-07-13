"""Pydantic-settings configuration for the recommendation API."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- API ---------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ---- MLflow / model registry ------------------------------------
    mlflow_tracking_uri: str = "http://mlflow:5000"
    model_name: str = "cashback_lgbm_ranker"
    retrieval_experiment: str = "cashback_retrieval"
    model_poll_interval_seconds: int = 60

    # ---- Holdout A/B (beyond-plan) ----------------------------------
    # Синхронный сплит: ~holdout_ratio пользователей обслуживаются
    # предыдущей (Archived) моделью — честный онлайн-uplift Production
    # против предшественника, атрибуция по model_version (миграция 005).
    holdout_enabled: bool = True
    holdout_ratio: float = 0.05
    holdout_salt: str = "cashback-holdout-v1"

    # ---- Postgres ---------------------------------------------------
    postgres_dsn: str = "postgresql+asyncpg://cashback:cashback@postgres:5432/cashback"

    # ---- ClickHouse -------------------------------------------------
    clickhouse_host: str = "clickhouse"
    clickhouse_http_port: int = 8123
    clickhouse_user: str = "cashback"
    clickhouse_password: str = "cashback"
    clickhouse_db: str = "cashback"

    # ---- Redis ------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # Service-to-service auth (beyond-plan): общий HS256-секрет.
    jwt_secret: str = "dev-secret-change-me"
    # Аварийный выключатель проверки токена (демо/локально).
    auth_enabled: bool = True

    # ---- Feature store ----------------------------------------------
    feature_store_timeout_ms: int = 200

    # ---- Retrieval --------------------------------------------------
    candidate_top_k: int = 20
    default_top_k: int = 5
    max_top_k: int = 20

    # ---- BRE --------------------------------------------------------
    anti_fatigue_ttl_days: int = 14
    frequency_gate_per_24h: int = 5
    min_award_threshold: float = 1.0     # roubles

    # ---- Kafka (publish recommendations.created events) -------------
    kafka_bootstrap_servers: str = "kafka:9092"
    recommendations_topic: str = "recommendations.created"

    # ---- OpenTelemetry ----------------------------------------------
    otel_service_name: str = "recommendation-api"
    otel_endpoint: str = ""               # disabled if empty


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
