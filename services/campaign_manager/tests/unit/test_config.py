"""Settings: обязательные стойкие секреты (B19) и CORS без '*' (B36)."""
from __future__ import annotations

import pytest
from app.config import Settings
from pydantic import ValidationError

JWT = "j" * 32
SERVICE = "s" * 32


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{"jwt_secret": JWT,
                                       "service_jwt_secret": SERVICE,
                                       **overrides})


@pytest.mark.parametrize("var", ["JWT_SECRET", "SERVICE_JWT_SECRET"])
def test_secrets_are_required(monkeypatch, var):
    monkeypatch.setenv("JWT_SECRET", JWT)
    monkeypatch.setenv("SERVICE_JWT_SECRET", SERVICE)
    monkeypatch.delenv(var)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert var.lower() in str(exc.value)
    assert "missing" in str(exc.value).lower()


@pytest.mark.parametrize("field", ["jwt_secret", "service_jwt_secret"])
@pytest.mark.parametrize("value,reason", [
    ("", "is empty"),
    (" " * 40, "is empty"),                 # одни пробелы — тоже пусто
    ("\t\n " * 12, "is empty"),
    ("x" * 31, "at least 32 bytes"),
    ("dev-secret-change-me", "known default"),
    ("please-change-me", "known default"),
    ("  Change-Me  ", "known default"),
    ("changeme", "known default"),
    ("secret", "known default"),
    ("test-secret", "known default"),
])
def test_weak_secrets_are_rejected_without_echoing_them(field, value, reason):
    with pytest.raises(ValidationError) as exc:
        _settings(**{field: value})
    message = str(exc.value)
    assert reason in message and field.upper() in message
    # секрет не уходит в лог упавшего старта (pydantic печатает input_value)
    assert "input_value" not in message
    if len(value.strip()) > len("secret"):
        assert value.strip() not in message


def test_secret_length_is_counted_in_utf8_bytes():
    assert _settings(jwt_secret="я" * 16).jwt_secret.get_secret_value() == "я" * 16
    with pytest.raises(ValidationError):
        _settings(jwt_secret="я" * 15 + "x")        # 31 байт


def test_admin_and_service_secrets_must_differ():
    """SERVICE_JWT_SECRET есть у клиентского mobile_api — совпадение ключей
    вернуло бы ему возможность подписать ADMIN-токен."""
    with pytest.raises(ValidationError) as exc:
        _settings(service_jwt_secret=JWT)
    assert "must differ" in str(exc.value)
    assert JWT not in str(exc.value)


def test_secrets_are_masked_in_repr():
    s = _settings()
    assert JWT not in repr(s) and SERVICE not in repr(s)


def test_defaults_for_authz_sse_and_ml_proxy():
    s = _settings()
    assert s.cors_origins == []
    assert s.recommendation_api_url == "http://recommendation-api:8001"
    assert s.recommendation_api_timeout_seconds == 5.0
    assert s.sse_max_subscribers == 200 and s.sse_max_subscribers_per_user == 5
    assert (s.login_throttle_window_seconds, s.login_max_failures_per_email_ip,
            s.login_max_attempts_per_ip) == (900, 5, 30)
    assert (s.access_token_ttl_minutes, s.refresh_token_ttl_days) == (15, 7)


@pytest.mark.parametrize("raw,expected", [
    ("http://localhost:3000,http://localhost:5173",
     ["http://localhost:3000", "http://localhost:5173"]),
    (" http://a.ru , ,http://b.ru ", ["http://a.ru", "http://b.ru"]),
    ('["http://a.ru", "http://b.ru"]', ["http://a.ru", "http://b.ru"]),
    ("", []),
    ("[]", []),
])
def test_cors_origins_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("CORS_ORIGINS", raw)
    assert _settings().cors_origins == expected


@pytest.mark.parametrize("raw", ["*", " * ", "http://a.ru,*", '["*"]'])
def test_cors_wildcard_is_rejected(monkeypatch, raw):
    monkeypatch.setenv("CORS_ORIGINS", raw)
    with pytest.raises(ValidationError) as exc:
        _settings()
    assert "explicit origins" in str(exc.value)


def test_cors_wildcard_rejected_in_python_list_too():
    with pytest.raises(ValidationError):
        _settings(cors_origins=["http://a.ru", "*"])
