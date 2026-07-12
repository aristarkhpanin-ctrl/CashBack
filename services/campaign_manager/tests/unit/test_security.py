"""Unit tests for JWT auth / RBAC primitives (phase 15)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from app.security import (
    AuthUser,
    _make_token,
    decode_token,
    get_current_user,
    hash_password,
    issue_token_pair,
    require_role,
    verify_password,
)
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials


# ── Пароли ───────────────────────────────────────────────────────────────────
def test_password_roundtrip():
    hashed = hash_password("s3cret!")
    assert hashed != "s3cret!"
    assert verify_password("s3cret!", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_verify_password_survives_malformed_hash():
    assert verify_password("x", "not-a-bcrypt-hash") is False


# ── Токены ───────────────────────────────────────────────────────────────────
def test_token_pair_roundtrip():
    pair = issue_token_pair(user_id="u1", email="a@b.ru", role="ADMIN")
    access = decode_token(pair["access_token"], expected_type="access")
    refresh = decode_token(pair["refresh_token"], expected_type="refresh")
    assert access["sub"] == "u1" and access["role"] == "ADMIN"
    assert refresh["type"] == "refresh"


def test_refresh_token_rejected_as_access():
    """Refresh-токен нельзя предъявить вместо access — иначе TTL 7 дней."""
    pair = issue_token_pair(user_id="u1", email="a@b.ru", role="ADMIN")
    with pytest.raises(HTTPException) as exc:
        decode_token(pair["refresh_token"], expected_type="access")
    assert exc.value.status_code == 401


def test_expired_token_rejected():
    token = _make_token(sub="u1", email="a@b.ru", role="ADMIN",
                        token_type="access", ttl=timedelta(seconds=-5))
    with pytest.raises(HTTPException) as exc:
        decode_token(token, expected_type="access")
    assert exc.value.status_code == 401


def test_tampered_token_rejected():
    pair = issue_token_pair(user_id="u1", email="a@b.ru", role="ANALYST")
    tampered = pair["access_token"][:-4] + "AAAA"
    with pytest.raises(HTTPException) as exc:
        decode_token(tampered, expected_type="access")
    assert exc.value.status_code == 401


# ── FastAPI-зависимости ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_get_current_user_requires_credentials():
    with pytest.raises(HTTPException) as exc:
        await get_current_user(creds=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_get_current_user_decodes_claims():
    pair = issue_token_pair(user_id="u42", email="m@b.ru", role="MARKETER")
    creds = HTTPAuthorizationCredentials(
        scheme="Bearer", credentials=pair["access_token"],
    )
    user = await get_current_user(creds=creds)
    assert user == AuthUser(user_id="u42", email="m@b.ru", role="MARKETER")


@pytest.mark.asyncio
async def test_require_role_matrix():
    dep = require_role("ADMIN", "MARKETER")

    assert (await dep(user=AuthUser("1", "a@b.ru", "ADMIN"))).role == "ADMIN"
    assert (await dep(user=AuthUser("2", "m@b.ru", "MARKETER"))).role == "MARKETER"

    with pytest.raises(HTTPException) as exc:
        await dep(user=AuthUser("3", "d@b.ru", "ANALYST"))
    assert exc.value.status_code == 403
    assert "ANALYST" in exc.value.detail
