"""Pydantic-settings configuration for the Campaign Manager."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Значения-заглушки из старых .env.example / compose / helm (B19): с любым из
# них токен подделывает каждый, кто читал репозиторий.
KNOWN_DEFAULT_SECRETS = frozenset({
    "dev-secret-change-me", "please-change-me", "change-me", "changeme",
    "secret", "test-secret",
})
MIN_SECRET_BYTES = 32


def check_secret(name: str, value: str) -> None:
    """Пустой (или из одних пробелов), короче 32 байт (utf-8) или известный
    дефолт → ValueError.

    Значение секрета в сообщение не попадает (оно уходит в лог старта).
    """
    if not value.strip():
        raise ValueError(f"{name} is empty")
    if value.strip().lower() in KNOWN_DEFAULT_SECRETS:
        raise ValueError(f"{name} is a known default value")
    if len(value.encode("utf-8")) < MIN_SECRET_BYTES:
        raise ValueError(f"{name} must be at least {MIN_SECRET_BYTES} bytes")


class Settings(BaseSettings):
    # hide_input_in_errors: ValidationError иначе печатает input_value —
    # то есть сам секрет — в лог упавшего старта.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore",
                                      hide_input_in_errors=True)

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8002
    log_level: str = "INFO"
    # CORS_ORIGINS: "https://a,https://b" или JSON-список; по умолчанию пусто —
    # фронт ходит same-origin через nginx. "*" запрещён (B36): CORS-мидлварь
    # разрешает credentials, а с ними wildcard открывает API любому сайту.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

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

    # Kafka (фаза 19) — источник SSE-событий (топик cashback.accrued).
    kafka_bootstrap_servers: str = "kafka:9092"
    # SSE (B36): лимит одновременных подписчиков на процесс → 503 сверх него,
    # и на одного пользователя → 429 (иначе один логин занимал все места).
    sse_max_subscribers: int = Field(default=200, ge=1)
    sse_max_subscribers_per_user: int = Field(default=5, ge=1)

    # Auth (B19): секреты обязательны, без дефолта в коде. JWT_SECRET
    # подписывает admin access/refresh/sse-токены и есть только здесь;
    # SERVICE_JWT_SECRET — сервисные вызовы в recommendation_api (его же
    # держит клиентский mobile_api, поэтому ключи обязаны различаться).
    jwt_secret: SecretStr
    service_jwt_secret: SecretStr
    access_token_ttl_minutes: int = Field(default=15, ge=1)
    refresh_token_ttl_days: int = Field(default=7, ge=1)

    # Троттлинг /auth/login (B20): окно и лимиты счётчиков в Redis.
    login_throttle_window_seconds: int = Field(default=900, ge=1)
    login_max_failures_per_email_ip: int = Field(default=5, ge=1)
    login_max_attempts_per_ip: int = Field(default=30, ge=1)

    # recommendation_api — объяснения ML без побочных эффектов (B27).
    recommendation_api_url: str = "http://recommendation-api:8001"
    recommendation_api_timeout_seconds: float = Field(default=5.0, gt=0)

    # Business knobs
    min_award_threshold: float = 1.0
    budget_reservation_default_ttl: int = 300       # seconds
    scheduling_interval_minutes: int = 15
    daily_budget_pause_threshold: float = 1.0       # 100 % of total budget by default

    # A/B testing
    ab_significance_alpha: float = 0.05
    ab_trending_alpha: float = 0.20
    ab_min_observations: int = 200

    @field_validator("jwt_secret", "service_jwt_secret")
    @classmethod
    def _strong_secret(cls, v: SecretStr, info) -> SecretStr:
        check_secret(info.field_name.upper(), v.get_secret_value())
        return v

    @model_validator(mode="after")
    def _distinct_secrets(self) -> Settings:
        if (self.jwt_secret.get_secret_value()
                == self.service_jwt_secret.get_secret_value()):
            raise ValueError("JWT_SECRET and SERVICE_JWT_SECRET must differ")
        return self

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: Any) -> Any:
        if isinstance(v, str):
            raw = v.strip()
            v = json.loads(raw) if raw.startswith("[") else raw.split(",")
        if isinstance(v, list | tuple):
            v = [str(o).strip() for o in v if str(o).strip()]
            if "*" in v:
                raise ValueError(
                    "CORS_ORIGINS must list explicit origins: '*' is not "
                    "allowed together with credentials"
                )
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
