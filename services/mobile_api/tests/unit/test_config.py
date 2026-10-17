"""Settings: сервис не стартует со слабыми секретами и CORS "*" (B19, B36)."""
from __future__ import annotations

import pytest
from app.config import Settings
from pydantic import ValidationError

from tests.conftest import ADMIN_JWT_SECRET

STRONG_MOBILE = "m" * 32
STRONG_SERVICE = "s" * 32


def _settings(monkeypatch, **env: str | None) -> Settings:
    """Settings только из переданного окружения (без .env и чужих переменных)."""
    for name in ("MOBILE_JWT_SECRET", "SERVICE_JWT_SECRET", "MOBILE_JWT_ISSUER",
                 "JWT_SECRET", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        if value is not None:
            monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def _strong(**override: str | None) -> dict[str, str | None]:
    return {"MOBILE_JWT_SECRET": STRONG_MOBILE,
            "SERVICE_JWT_SECRET": STRONG_SERVICE, **override}


def test_strong_secrets_are_accepted(monkeypatch):
    s = _settings(monkeypatch, **_strong())
    assert s.mobile_jwt_secret.get_secret_value() == STRONG_MOBILE
    assert s.service_jwt_secret.get_secret_value() == STRONG_SERVICE
    assert s.mobile_jwt_issuer == "cashback-demo-idp"
    assert s.cors_origins == []


@pytest.mark.parametrize("missing", ["MOBILE_JWT_SECRET", "SERVICE_JWT_SECRET"])
def test_each_secret_is_required(monkeypatch, missing):
    with pytest.raises(ValidationError) as err:
        _settings(monkeypatch, **_strong(**{missing: None}))
    assert missing.lower() in str(err.value)


def test_jwt_secret_is_not_read_and_cannot_stand_in(monkeypatch):
    """Клиентский BFF не держит JWT_SECRET (B19): прежняя конфигурация, где
    был только он, не стартует, а сам ключ в Settings не попадает."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, JWT_SECRET=ADMIN_JWT_SECRET)

    s = _settings(monkeypatch, **_strong(JWT_SECRET=ADMIN_JWT_SECRET))
    assert not hasattr(s, "jwt_secret")
    assert ADMIN_JWT_SECRET not in repr(s.model_dump())


@pytest.mark.parametrize("field", ["MOBILE_JWT_SECRET", "SERVICE_JWT_SECRET"])
@pytest.mark.parametrize("bad", [
    "",
    " " * 40,
    "dev-secret-change-me",
    "  DEV-SECRET-CHANGE-ME  ",
    "please-change-me", "change-me", "changeme", "secret", "test-secret",
    # Заглушка, добитая пробелами до 32+ байт, проходит проверку длины —
    # её отсекает только сверка с известными значениями.
    "dev-secret-change-me" + " " * 20,
    "\tCHANGEME" + " " * 30,
    "x" * 31,
    "й" * 15,  # 30 байт utf-8
])
def test_weak_secret_is_rejected_without_echoing_it(monkeypatch, field, bad):
    with pytest.raises(ValidationError) as err:
        _settings(monkeypatch, **_strong(**{field: bad}))
    assert field.lower() in str(err.value)
    # hide_input_in_errors: значение не попадает в ошибку (а с ней — в лог).
    assert "input_value" not in str(err.value)


def test_secret_length_is_counted_in_utf8_bytes(monkeypatch):
    s = _settings(monkeypatch, **_strong(MOBILE_JWT_SECRET="й" * 16))  # 32 байта
    assert len(s.mobile_jwt_secret.get_secret_value()) == 16


def test_mobile_and_service_secrets_must_differ(monkeypatch):
    """Тот, кто держит MOBILE_JWT_SECRET (IdP, .env для минтинга), не должен
    уметь подписать service-токен в recommendation_api."""
    with pytest.raises(ValidationError) as err:
        _settings(monkeypatch, **_strong(SERVICE_JWT_SECRET=STRONG_MOBILE))
    assert "must differ" in str(err.value)
    assert STRONG_MOBILE not in str(err.value)


def test_issuer_comes_from_env(monkeypatch):
    s = _settings(monkeypatch, **_strong(MOBILE_JWT_ISSUER="bank-idp"))
    assert s.mobile_jwt_issuer == "bank-idp"
    with pytest.raises(ValidationError):
        _settings(monkeypatch, **_strong(MOBILE_JWT_ISSUER=""))


@pytest.mark.parametrize(("raw", "expected"), [
    ("http://localhost:3000,http://localhost:5173",
     ["http://localhost:3000", "http://localhost:5173"]),
    (" http://a.example , ,http://b.example ", ["http://a.example", "http://b.example"]),
    ('["http://a.example", "http://b.example"]', ["http://a.example", "http://b.example"]),
    ("", []),
])
def test_cors_origins_parsing(monkeypatch, raw, expected):
    assert _settings(monkeypatch, **_strong(CORS_ORIGINS=raw)).cors_origins == expected


@pytest.mark.parametrize("raw", ["*", "http://a.example,*", '["*"]'])
def test_cors_wildcard_is_rejected(monkeypatch, raw):
    with pytest.raises(ValidationError) as err:
        _settings(monkeypatch, **_strong(CORS_ORIGINS=raw))
    assert "cors_origins" in str(err.value)
