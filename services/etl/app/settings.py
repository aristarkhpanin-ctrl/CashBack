"""Centralised configuration via pydantic-settings."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Kafka
    kafka_bootstrap_servers: str = "kafka:9092"
    schema_registry_url: str = "http://schema-registry:8081"
    kafka_topic_transactions: str = "transactions.raw"
    kafka_consumer_group: str = "cashback-etl-group"

    # ClickHouse
    clickhouse_host: str = "clickhouse"
    clickhouse_http_port: int = 8123
    clickhouse_user: str = "cashback"
    clickhouse_password: str = "cashback"
    clickhouse_db: str = "cashback"

    # Postgres
    postgres_dsn: str = "postgresql://cashback:cashback@postgres:5432/cashback"

    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Storage
    staging_dir: str = "/data/staging"
    feature_ttl_seconds: int = 3600
    rfm_window_days: int = 90


def get_settings() -> Settings:
    return Settings()
