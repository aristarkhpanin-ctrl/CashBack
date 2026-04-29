"""Pydantic-settings configuration for the transaction listener."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- General ----------------------------------------------------
    log_level: str = "INFO"
    metrics_port: int = 9100

    # ---- Kafka ------------------------------------------------------
    kafka_bootstrap_servers: str = "kafka:9092"
    schema_registry_url: str = "http://schema-registry:8081"

    transactions_topic: str = "transactions.raw"
    accruals_topic: str = "cashback.accrued"
    recommendations_topic: str = "recommendations.created"

    consumer_group_accrual: str = "cashback-accrual-group"
    consumer_group_notify: str = "cashback-notify-group"

    poll_timeout_ms: int = 100
    batch_size: int = 500

    # ---- Postgres ---------------------------------------------------
    postgres_dsn: str = "postgresql+asyncpg://cashback:cashback@postgres:5432/cashback"

    # ---- Redis ------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"
    accepted_offer_key_fmt: str = "accepted_offers:{user_id}:{mcc_code}"

    # ---- Notification ----------------------------------------------
    template_dir: str = "/app/app/notification/templates"
    sent_emails_dir: str = "/tmp/sent_emails"
    fcm_endpoint: str = "https://fcm.googleapis.com/fcm/send"
    fcm_token: str = ""           # demo deployment uses an empty token
    sms_endpoint: str = ""        # stub
    notification_default_channels: list[str] = Field(
        default_factory=lambda: ["push", "in_app", "email", "sms"]
    )

    # ---- OST -------------------------------------------------------
    ost_key_fmt: str = "ost:{user_id}"
    ost_default_active_hours: list[int] = Field(
        default_factory=lambda: list(range(8, 22))   # 08:00 — 22:00 local
    )
    ost_max_defer_hours: int = 18


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
