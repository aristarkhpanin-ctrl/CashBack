"""Pydantic-settings configuration for the Mobile BFF."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- API --------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8003
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ---- Upstream services -----------------------------------------
    recommendation_api_url: str = "http://recommendation-api:8001"
    recommendation_timeout_seconds: float = 1.5
    recommendation_retry_attempts: int = 3
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: int = 30

    # ---- Backing services ------------------------------------------
    postgres_dsn: str = "postgresql+asyncpg://cashback:cashback@postgres:5432/cashback"
    redis_url: str = "redis://redis:6379/0"

    # ---- Kafka (offer.accepted publishing) -------------------------
    kafka_bootstrap_servers: str = "kafka:9092"
    offer_accepted_topic: str = "offer.accepted"

    # ---- Behaviour --------------------------------------------------
    accepted_offer_key_fmt: str = "accepted_offers:{user_id}:{mcc_code}"
    snooze_days: int = 3
    cdn_base_url: str = "https://cdn.cashback.example.com/icons"
    deeplink_template: str = "cashback://offer/{recommendation_id}"
    default_top_k: int = 5
    max_top_k: int = 20


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
