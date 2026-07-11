"""JWT-аутентификация и RBAC (фаза 15).

Дизайн:
* HS256 + общий секрет (``JWT_SECRET``) — один выпускающий сервис
  (campaign_manager), внешний IdP для демо-стенда избыточен (ADR-подход:
  минимум движущихся частей при полной демонстрации механики RBAC).
* access-токен 15 минут / refresh 7 дней; тип зашит в клейм ``type``,
  чтобы refresh-токен нельзя было предъявить как access.
* ``get_current_user`` доверяет клеймам access-токена и не ходит в БД —
  это горячий путь каждого запроса; БД трогают только /auth/login,
  /auth/refresh (проверка is_active) и /auth/me.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import bcrypt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings

log = structlog.get_logger("security")

ALGORITHM = "HS256"
ROLES = ("ADMIN", "MARKETER", "ANALYST")

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------
def _make_token(*, sub: str, email: str, role: str,
                token_type: str, ttl: timedelta) -> str:
    cfg = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": sub,
        "email": email,
        "role": role,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, cfg.jwt_secret, algorithm=ALGORITHM)


def issue_token_pair(*, user_id: str, email: str, role: str) -> dict[str, str]:
    cfg = get_settings()
    return {
        "access_token": _make_token(
            sub=user_id, email=email, role=role, token_type="access",
            ttl=timedelta(minutes=cfg.access_token_ttl_minutes),
        ),
        "refresh_token": _make_token(
            sub=user_id, email=email, role=role, token_type="refresh",
            ttl=timedelta(days=cfg.refresh_token_ttl_days),
        ),
        "token_type": "bearer",
    }


def decode_token(token: str, *, expected_type: str) -> dict:
    cfg = get_settings()
    try:
        payload = jwt.decode(token, cfg.jwt_secret, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if payload.get("type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"expected {expected_type} token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


# ---------------------------------------------------------------------------
# FastAPI-зависимости
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthUser:
    user_id: str
    email: str
    role: str


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthUser:
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(creds.credentials, expected_type="access")
    return AuthUser(
        user_id=str(payload.get("sub", "")),
        email=str(payload.get("email", "")),
        role=str(payload.get("role", "")),
    )


def require_role(*roles: str):
    """Фабрика зависимостей: 401 без токена, 403 при недостаточной роли."""
    allowed = set(roles)

    async def _dep(user: AuthUser = Depends(get_current_user)) -> AuthUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {user.role} is not allowed "
                       f"(need one of: {sorted(allowed)})",
            )
        return user

    return _dep
