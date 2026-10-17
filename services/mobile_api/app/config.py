"""Pydantic-settings configuration for the Mobile BFF."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # ---- API --------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8003
    log_level: str = "INFO"
    # CORS (B36): по умолчанию никаких origin — мобильное приложение не
    # браузер, веб-клиенты ходят same-origin через nginx. CORS_ORIGINS —
    # через запятую или JSON-список; "*" запрещён (credentials разрешены).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # ---- Upstream services -----------------------------------------
    recommendation_api_url: str = "http://recommendation-api:8001"
    recommendation_timeout_seconds: float = 1.5
    recommendation_retry_attempts: int = 3
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_reset_seconds: int = 30

    # ---- Аутентификация (B12, B19) ---------------------------------
    # Клиентский BFF держит ровно два ключа и НЕ держит JWT_SECRET: взлом
    # mobile_api не должен давать подделку админских токенов.
    # MOBILE_JWT_SECRET — только проверка customer-токенов (их выпускает
    # IdP банка; в демо — scripts/mint_customer_token.py).
    mobile_jwt_secret: SecretStr
    mobile_jwt_issuer: str = Field(default="cashback-demo-idp", min_length=1)
    # SERVICE_JWT_SECRET — подпись service-токена для recommendation_api
    # (iss mobile-api). Поля обязательные, выключателя проверки нет.
    service_jwt_secret: SecretStr
    service_token_ttl_seconds: int = 300

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

    # ---- Validators -------------------------------------------------
    @field_validator("mobile_jwt_secret", "service_jwt_secret")
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

    @model_validator(mode="after")
    def _distinct_secrets(self) -> Settings:
        # MOBILE_JWT_SECRET знает и IdP (а в демо — каждый, у кого есть .env
        # для минтинга): совпади ключи, он подписал бы service-токен и ходил
        # бы в recommendation_api за любого пользователя мимо проверок BFF.
        if (self.mobile_jwt_secret.get_secret_value()
                == self.service_jwt_secret.get_secret_value()):
            raise ValueError("MOBILE_JWT_SECRET and SERVICE_JWT_SECRET must differ")
        return self

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
