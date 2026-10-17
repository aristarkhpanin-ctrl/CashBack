"""Pydantic-settings configuration for the recommendation API."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# B19: публичные значения-заглушки (прежние дефолты config.py, compose,
# .env.example) — с ними сервис не стартует, даже если длина достаточна.
KNOWN_DEFAULT_SECRETS = frozenset({
    "dev-secret-change-me", "please-change-me", "change-me", "changeme",
    "secret", "test-secret",
})
MIN_SECRET_BYTES = 32


class Settings(BaseSettings):
    # hide_input_in_errors: отвергнутый секрет не попадает в текст ошибки
    # валидации (а значит, и в лог упавшего старта).
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", hide_input_in_errors=True,
    )

    # ---- API ---------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    log_level: str = "INFO"
    # CORS (B36): по умолчанию никаких origin — браузер к rec_api не ходит,
    # его вызывают только mobile_api и campaign_manager. CORS_ORIGINS —
    # через запятую или JSON-список; "*" запрещён (credentials разрешены).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

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

    # ---- Service-to-service auth (B19, B27) --------------------------
    # rec_api только ПРОВЕРЯЕТ service-токены mobile_api и campaign_manager
    # (app/security.py). JWT_SECRET (админские токены) сервису не нужен и
    # не читается. Поле обязательное, выключателя проверки нет.
    service_jwt_secret: SecretStr

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

    # ---- Validators -------------------------------------------------
    @field_validator("service_jwt_secret")
    @classmethod
    def _strong_secret(cls, value: SecretStr) -> SecretStr:
        """B19: пустой/короткий/известный ключ = подделываемый токен."""
        raw = value.get_secret_value()
        if not raw.strip():
            raise ValueError("must not be empty (generate: openssl rand -hex 32)")
        if raw.strip().lower() in KNOWN_DEFAULT_SECRETS:
            raise ValueError("is a known default value (generate: openssl rand -hex 32)")
        if len(raw.encode("utf-8")) < MIN_SECRET_BYTES:
            raise ValueError(f"must be at least {MIN_SECRET_BYTES} bytes (utf-8)")
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """CORS_ORIGINS из env: JSON-список или строка через запятую."""
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith("["):
                return json.loads(raw)
            return [part.strip() for part in raw.split(",") if part.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: list[str]) -> list[str]:
        # main.py разрешает credentials; "*" вместе с ними отдал бы ответы
        # любому сайту (B36) — только явные origin.
        if "*" in value:
            raise ValueError("'*' is not allowed (credentials are enabled); "
                             "list explicit origins")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
