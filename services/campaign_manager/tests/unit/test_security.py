"""Unit tests for JWT auth primitives (phase 15, stage B: B19 / B35)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import pytest
from app.config import get_settings
from app.models import AdminUser
from app.security import (
    BCRYPT_ROUNDS,
    DUMMY_PASSWORD_HASH,
    AuthUser,
    authenticate_sse_token,
    decode_token,
    get_current_user,
    get_current_user_allow_pwd_change,
    hash_password,
    issue_service_token,
    issue_sse_token,
    make_access_token,
    make_refresh_token,
    password_policy_violation,
    require_role,
    verify_password,
)
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from tests.unit.fakes import FakeSession

UID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _row(*, role="ADMIN", is_active=True, token_version=0,
         must_change_password=False) -> AdminUser:
    return AdminUser(user_id=UID, email="a@b.ru", password_hash="x",
                     full_name="A", role=role, is_active=is_active,
                     created_at=datetime(2026, 1, 1, tzinfo=UTC),
                     token_version=token_version,
                     must_change_password=must_change_password)


def _session(*rows: AdminUser) -> FakeSession:
    s = FakeSession()
    s.put(*rows)
    return s


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _forge(key: str, **overrides) -> str:
    """Admin-токен «как настоящий», подписанный произвольным ключом."""
    now = datetime.now(UTC)
    claims = {
        "iss": "cashback-campaign-manager", "aud": "cashback-admin",
        "type": "access", "sub": str(UID), "email": "a@b.ru", "role": "ADMIN",
        "ver": 0, "jti": "j", "pwd_change": False,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="HS256")


def _status(exc: pytest.ExceptionInfo[HTTPException]) -> int:
    return exc.value.status_code


# ── Пароли ───────────────────────────────────────────────────────────────────
def test_password_roundtrip():
    hashed = hash_password("s3cret!")
    assert hashed != "s3cret!"
    assert verify_password("s3cret!", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_verify_password_survives_malformed_hash_and_overlong_password():
    assert verify_password("x", "not-a-bcrypt-hash") is False
    assert verify_password("x" * 100, DUMMY_PASSWORD_HASH) is False


def test_dummy_hash_costs_the_same_as_real_hashes():
    """Иначе время ответа для неизвестного email отличается (перечисление)."""
    assert DUMMY_PASSWORD_HASH.startswith(f"$2b${BCRYPT_ROUNDS:02d}$")
    assert hash_password("x").startswith(f"$2b${BCRYPT_ROUNDS:02d}$")
    bcrypt.checkpw(b"probe", DUMMY_PASSWORD_HASH.encode())   # валидный хэш


@pytest.mark.parametrize("new,problem", [
    ("short", "password must be at least 12 characters"),
    ("ж" * 40, "password must be at most 72 bytes"),
    ("Dev-Secret-Change-Me", "password is a known default"),
    ("user@bank.ru-2026", None),                 # содержит email — допустимо
    ("USER@bank.ru", "password must differ from email"),
    ("current-password", "new password must differ from the current one"),
    ("a-fine-new-passphrase", None),
])
def test_password_policy(new, problem):
    assert password_policy_violation(new, email="user@bank.ru",
                                     current="current-password") == problem


# ── Токены: выпуск и проверка ────────────────────────────────────────────────
def test_access_and_refresh_claims():
    user = _row(role="MARKETER", token_version=3, must_change_password=True)
    access = decode_token(make_access_token(user), expected_type="access")
    assert access["sub"] == str(UID) and access["role"] == "MARKETER"
    assert access["ver"] == 3 and access["pwd_change"] is True
    assert access["iss"] == "cashback-campaign-manager"
    assert access["aud"] == "cashback-admin"
    assert access["exp"] - access["iat"] == 15 * 60
    token, jti = make_refresh_token(user, fam="fam-1")
    refresh = decode_token(token, expected_type="refresh")
    assert refresh["jti"] == jti and refresh["fam"] == "fam-1"
    assert refresh["ver"] == 3 and "role" not in refresh
    assert refresh["exp"] - refresh["iat"] == 7 * 24 * 3600


def test_refresh_token_rejected_as_access():
    """Refresh-токен нельзя предъявить вместо access — иначе TTL 7 дней."""
    token, _ = make_refresh_token(_row(), fam="f")
    with pytest.raises(HTTPException) as exc:
        decode_token(token, expected_type="access")
    assert _status(exc) == 401


def test_token_signed_with_empty_key_is_rejected():
    """B19: python-jose подписывает HS256 пустым ключом — такой токен не
    должен проходить (раньше пустой JWT_SECRET в helm делал его валидным)."""
    with pytest.raises(HTTPException) as exc:
        decode_token(_forge(""), expected_type="access")
    assert _status(exc) == 401


def test_token_signed_with_service_secret_is_rejected():
    """mobile_api держит SERVICE_JWT_SECRET: admin-токен им не подписать."""
    service_key = get_settings().service_jwt_secret.get_secret_value()
    with pytest.raises(HTTPException) as exc:
        decode_token(_forge(service_key), expected_type="access")
    assert _status(exc) == 401


@pytest.mark.parametrize("override", [
    {"aud": "cashback-sse"},
    {"aud": "recommendation-api"},
    {"aud": None},
    {"iss": "mobile-api"},
    {"iss": None},
    {"exp": None},
    {"iat": None},
    {"sub": None},
    {"type": "refresh"},
    {"exp": int(datetime.now(UTC).timestamp()) - 60},
])
def test_token_with_wrong_or_missing_claims_is_rejected(override):
    key = get_settings().jwt_secret.get_secret_value()
    decode_token(_forge(key), expected_type="access")            # контроль
    with pytest.raises(HTTPException) as exc:
        decode_token(_forge(key, **override), expected_type="access")
    assert _status(exc) == 401


def test_unsigned_alg_none_token_is_rejected():
    header = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"           # {"alg":"none"}
    body = _forge("x").split(".")[1]
    with pytest.raises(HTTPException) as exc:
        decode_token(f"{header}.{body}.", expected_type="access")
    assert _status(exc) == 401


def test_tampered_token_rejected():
    token = make_access_token(_row(role="ANALYST"))
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    with pytest.raises(HTTPException) as exc:
        decode_token(tampered, expected_type="access")
    assert _status(exc) == 401


def test_service_token_for_recommendation_api():
    token = issue_service_token()
    key = get_settings().service_jwt_secret.get_secret_value()
    claims = jwt.decode(token, key, algorithms=["HS256"],
                        audience="recommendation-api", issuer="campaign-manager")
    assert claims["type"] == "service" and claims["sub"] == "campaign-manager"
    assert claims["exp"] - claims["iat"] == 300
    # не admin-токен: другой ключ, аудитория и издатель
    with pytest.raises(HTTPException):
        decode_token(token, expected_type="service")
    assert jwt.decode(issue_service_token("mobile-api"), key, algorithms=["HS256"],
                      audience="recommendation-api")["iss"] == "mobile-api"


# ── get_current_user: проверка по БД на каждом запросе ───────────────────────
async def test_get_current_user_requires_credentials():
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds=None, session=_session())
    assert _status(exc) == 401


async def test_get_current_user_uses_current_db_role_with_one_pk_lookup():
    token = make_access_token(_row(role="ADMIN"))
    session = _session(_row(role="ANALYST"))        # разжалован после выдачи
    user = await get_current_user(creds=_creds(token), session=session)
    assert user == AuthUser(user_id=str(UID), email="a@b.ru", role="ANALYST")
    assert session.gets == [(AdminUser, UID)]


@pytest.mark.parametrize("row", [
    None,                                           # удалён
    _row(is_active=False),                          # деактивирован
    _row(token_version=1),                          # роль/пароль менялись
])
async def test_get_current_user_rejects_revoked(row):
    token = make_access_token(_row())
    session = _session(row) if row is not None else _session()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds=_creds(token), session=session)
    assert _status(exc) == 401 and exc.value.detail == "token revoked"


@pytest.mark.parametrize("claims", [{"ver": None}, {"ver": "0"}, {"ver": True},
                                    {"sub": "not-a-uuid"}])
async def test_get_current_user_rejects_malformed_identity(claims):
    token = _forge(get_settings().jwt_secret.get_secret_value(), **claims)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds=_creds(token), session=_session(_row()))
    assert _status(exc) == 401


async def test_pwd_change_token_is_403_except_for_allow_variant():
    row = _row(must_change_password=True)
    token = make_access_token(row)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds=_creds(token), session=_session(row))
    assert _status(exc) == 403 and exc.value.detail == "password change required"
    user = await get_current_user_allow_pwd_change(creds=_creds(token),
                                                   session=_session(row))
    assert user.must_change_password is True


async def test_pwd_change_claim_alone_is_enough_to_gate():
    key = get_settings().jwt_secret.get_secret_value()
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds=_creds(_forge(key, pwd_change=True)),
                               session=_session(_row()))
    assert _status(exc) == 403


async def test_require_role_matrix():
    dep = require_role("ADMIN", "MARKETER")

    assert (await dep(user=AuthUser("1", "a@b.ru", "ADMIN"))).role == "ADMIN"
    assert (await dep(user=AuthUser("2", "m@b.ru", "MARKETER"))).role == "MARKETER"

    with pytest.raises(HTTPException) as exc:
        await dep(user=AuthUser("3", "d@b.ru", "ANALYST"))
    assert _status(exc) == 403
    assert "ANALYST" in exc.value.detail


# ── SSE-токены ───────────────────────────────────────────────────────────────
async def test_sse_token_roundtrip():
    row = _row(role="MARKETER", token_version=2)
    auth_user = AuthUser(str(UID), "a@b.ru", "MARKETER", token_version=2)
    token = issue_sse_token(auth_user)
    claims = decode_token(token, expected_type="sse", audience="cashback-sse")
    assert claims["exp"] - claims["iat"] == 60 and claims["ver"] == 2
    user = await authenticate_sse_token(token, _session(row))
    assert user.role == "MARKETER" and user.user_id == str(UID)


@pytest.mark.parametrize("row", [None, _row(is_active=False), _row(token_version=5)])
async def test_sse_token_rejected_for_revoked_user(row):
    token = issue_sse_token(AuthUser(str(UID), "a@b.ru", "ADMIN"))
    session = _session(row) if row is not None else _session()
    with pytest.raises(HTTPException) as exc:
        await authenticate_sse_token(token, session)
    assert _status(exc) == 401


async def test_sse_requires_sse_token():
    session = _session(_row())
    for token in (None, "", "garbage", make_access_token(_row())):
        with pytest.raises(HTTPException) as exc:
            await authenticate_sse_token(token, session)
        assert _status(exc) == 401
    # и SSE-токен не годится как access
    sse = issue_sse_token(AuthUser(str(UID), "a@b.ru", "ADMIN"))
    with pytest.raises(HTTPException):
        await get_current_user(creds=_creds(sse), session=session)


async def test_sse_token_of_user_who_must_change_password_is_403():
    token = issue_sse_token(AuthUser(str(UID), "a@b.ru", "ADMIN"))
    with pytest.raises(HTTPException) as exc:
        await authenticate_sse_token(token, _session(_row(must_change_password=True)))
    assert _status(exc) == 403
