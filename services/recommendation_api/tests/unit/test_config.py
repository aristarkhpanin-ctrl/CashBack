"""Settings: обязательный сильный SERVICE_JWT_SECRET (B19) и CORS (B36)."""
from __future__ import annotations

import pytest
from app.config import Settings
from pydantic import ValidationError

STRONG = "x" * 32
# Публичные значения-заглушки из контракта stage B (§1).
KNOWN_DEFAULTS = ["dev-secret-change-me", "please-change-me", "change-me",
                  "changeme", "secret", "test-secret"]


@pytest.fixture()
def env(monkeypatch):
    """Чистое окружение: без SERVICE_JWT_SECRET/CORS_ORIGINS из conftest/CI."""
    for name in ("SERVICE_JWT_SECRET", "JWT_SECRET", "CORS_ORIGINS", "AUTH_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings() -> Settings:
    return Settings(_env_file=None)


# ── SERVICE_JWT_SECRET ───────────────────────────────────────────────────────
def test_missing_service_secret_refuses_to_start(env):
    with pytest.raises(ValidationError) as exc:
        _settings()
    assert "service_jwt_secret" in str(exc.value)


def test_jwt_secret_does_not_satisfy_rec_api(env):
    """rec_api не читает JWT_SECRET (секрет админских токенов)."""
    env.setenv("JWT_SECRET", STRONG)
    with pytest.raises(ValidationError):
        _settings()
    assert "jwt_secret" not in Settings.model_fields


def test_auth_enabled_flag_removed():
    assert "auth_enabled" not in Settings.model_fields


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_secret_rejected(env, value):
    env.setenv("SERVICE_JWT_SECRET", value)
    with pytest.raises(ValidationError, match="must not be empty"):
        _settings()


@pytest.mark.parametrize("value", KNOWN_DEFAULTS)
def test_known_default_secret_rejected(env, value):
    for variant in (value, value.upper(), f" {value} "):
        env.setenv("SERVICE_JWT_SECRET", variant)
        with pytest.raises(ValidationError, match="known default"):
            _settings()


def test_short_secret_rejected_by_utf8_bytes(env):
    # 31 байт → отказ; 16 кириллических символов = 32 байта → принято.
    env.setenv("SERVICE_JWT_SECRET", "x" * 31)
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        _settings()
    env.setenv("SERVICE_JWT_SECRET", "ж" * 15 + "x")      # 31 байт, 16 символов
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        _settings()
    env.setenv("SERVICE_JWT_SECRET", "ж" * 16)
    assert _settings().service_jwt_secret.get_secret_value() == "ж" * 16


def test_strong_secret_accepted_and_hidden(env):
    env.setenv("SERVICE_JWT_SECRET", STRONG)
    cfg = _settings()
    assert cfg.service_jwt_secret.get_secret_value() == STRONG
    assert STRONG not in repr(cfg)


def test_rejected_secret_not_echoed_in_error(env):
    weak = "short-but-secret-value"
    env.setenv("SERVICE_JWT_SECRET", weak)
    with pytest.raises(ValidationError) as exc:
        _settings()
    assert weak not in str(exc.value)


# ── CORS_ORIGINS ─────────────────────────────────────────────────────────────
def test_cors_default_is_empty(env):
    env.setenv("SERVICE_JWT_SECRET", STRONG)
    assert _settings().cors_origins == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:3000,http://localhost:5173",
         ["http://localhost:3000", "http://localhost:5173"]),
        (" http://a.example , http://b.example ,",
         ["http://a.example", "http://b.example"]),
        ('["http://localhost:3000", "http://localhost:5173"]',
         ["http://localhost:3000", "http://localhost:5173"]),
        ("", []),
    ],
)
def test_cors_origins_comma_or_json(env, raw, expected):
    env.setenv("SERVICE_JWT_SECRET", STRONG)
    env.setenv("CORS_ORIGINS", raw)
    assert _settings().cors_origins == expected


@pytest.mark.parametrize("raw", ["*", '["*"]', "http://localhost:3000,*"])
def test_cors_wildcard_rejected(env, raw):
    env.setenv("SERVICE_JWT_SECRET", STRONG)
    env.setenv("CORS_ORIGINS", raw)
    with pytest.raises(ValidationError, match="not allowed"):
        _settings()
