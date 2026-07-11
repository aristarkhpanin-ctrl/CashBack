"""Pydantic-settings configuration for the Campaign Manager."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8002
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # Postgres
    postgres_dsn: str = "postgresql+asyncpg://cashback:cashback@postgres:5432/cashback"

    # ClickHouse
    clickhouse_host: str = "clickhouse"
    clickhouse_http_port: int = 8123
    clickhouse_user: str = "cashback"
    clickhouse_password: str = "cashback"
    clickhouse_db: str = "cashback"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Auth (фаза 15) — общий HS256-секрет; в k8s подставляется из Secret.
    jwt_secret: str = "dev-secret-change-me"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7

    # Business knobs
    min_award_threshold: float = 1.0
    budget_reservation_default_ttl: int = 300       # seconds
    scheduling_interval_minutes: int = 15
    daily_budget_pause_threshold: float = 1.0       # 100 % of total budget by default

    # A/B testing
    ab_significance_alpha: float = 0.05
    ab_trending_alpha: float = 0.20
    ab_min_observations: int = 200


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
