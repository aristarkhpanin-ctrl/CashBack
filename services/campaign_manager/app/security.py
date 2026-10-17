"""JWT-аутентификация админ-панели (фаза 15; этап B — B19, B20, B35).

Дизайн:
* HS256, ключи — из ``Settings`` (обязательные, проверяются при старте,
  см. ``config.py``). ``JWT_SECRET`` подписывает admin access/refresh/sse
  и есть только у campaign_manager; ``SERVICE_JWT_SECRET`` — сервисные
  токены в recommendation_api (его держит и клиентский mobile_api, поэтому
  admin-токен им подписать нельзя).
* Каждый токен несёт ``iss``/``aud``/``type``/``iat``/``exp``; ``decode_token``
  проверяет всё сразу — токен другого типа или аудитории = 401.
* ``get_current_user`` на КАЖДОМ запросе читает строку ``admin_users`` по PK
  (один запрос, без кэша): нет пользователя, он неактивен или ``ver`` ≠
  ``token_version`` → 401. Роль для авторизации — текущая из БД, а не клейм
  токена. Смена роли, деактивация и смена/сброс пароля увеличивают
  ``token_version`` → все выданные access/refresh/sse-токены гаснут сразу.
* ``must_change_password`` → access-токен с ``pwd_change: true``; с ним
  доступны только ``GET /auth/me`` и ``POST /auth/change-password``
  (зависимость ``get_current_user_allow_pwd_change``), остальное — 403.
* Ротация refresh-токенов (одноразовые ``jti`` в Redis) и троттлинг логина —
  в ``api/auth.py``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import KNOWN_DEFAULT_SECRETS, get_settings
from app.db import get_session_dep
from app.models import AdminUser

log = structlog.get_logger("security")

ALGORITHM = "HS256"
ROLES = ("ADMIN", "MARKETER", "ANALYST")

ISSUER = "cashback-campaign-manager"
ADMIN_AUDIENCE = "cashback-admin"
SSE_AUDIENCE = "cashback-sse"
SSE_TOKEN_TTL_SECONDS = 60
SERVICE_ISSUER = "campaign-manager"
SERVICE_AUDIENCE = "recommendation-api"
SERVICE_TOKEN_TTL_SECONDS = 300

PASSWORD_CHANGE_REQUIRED = "password change required"

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Пароли
# ---------------------------------------------------------------------------
BCRYPT_ROUNDS = 12
# bcrypt случайной строки с той же стоимостью, что и ``hash_password``:
# для неизвестного email проверяется он, чтобы ответ занимал столько же
# времени, сколько для существующего (B35 — перечисление email по времени).
DUMMY_PASSWORD_HASH = "$2b$12$USjHm/0wd8jPzqu8OjInpu4Hge9HoWcHu5CXmD0fIleztcgnpS3Vm"

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_BYTES = 72          # предел bcrypt: длиннее — ValueError
WEAK_PASSWORDS = KNOWN_DEFAULT_SECRETS | {
    "admin", "password", "qwerty", "123456", "pass123", "admin123",
}


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except ValueError:
        return False


# bcrypt — ~0.25 с CPU: в event loop он останавливал весь процесс (B35).
async def hash_password_async(plain: str) -> str:
    return await run_in_threadpool(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    return await run_in_threadpool(verify_password, plain, hashed)


def password_policy_violation(new: str, *, email: str, current: str) -> str | None:
    """Причина отказа для нового пароля или ``None``, если он годится."""
    if len(new) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(new.encode()) > MAX_PASSWORD_BYTES:
        return f"password must be at most {MAX_PASSWORD_BYTES} bytes"
    if new.strip().lower() in WEAK_PASSWORDS:
        return "password is a known default"
    if new.strip().lower() == email.strip().lower():
        return "password must differ from email"
    if new == current:
        return "new password must differ from the current one"
    return None


# ---------------------------------------------------------------------------
# Токены
# ---------------------------------------------------------------------------
def _encode(claims: dict[str, Any], *, secret: SecretStr, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {
        **claims,
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    return jwt.encode(payload, secret.get_secret_value(), algorithm=ALGORITHM)


def refresh_token_ttl() -> timedelta:
    return timedelta(days=get_settings().refresh_token_ttl_days)


def make_access_token(user: Any) -> str:
    """Access-токен для строки ``admin_users`` (или объекта с теми же полями)."""
    cfg = get_settings()
    return _encode({
        "iss": ISSUER,
        "aud": ADMIN_AUDIENCE,
        "type": "access",
        "sub": str(user.user_id),
        "email": user.email,
        "role": str(user.role),
        "ver": int(user.token_version),
        "jti": uuid.uuid4().hex,
        "pwd_change": bool(user.must_change_password),
    }, secret=cfg.jwt_secret, ttl=timedelta(minutes=cfg.access_token_ttl_minutes))


def make_refresh_token(user: Any, *, fam: str) -> tuple[str, str]:
    """Refresh-токен семейства ``fam`` → ``(token, jti)``.

    ``jti`` одноразовый: api/auth.py кладёт его в Redis при выдаче и
    атомарно забирает (GETDEL) при обмене.
    """
    cfg = get_settings()
    jti = uuid.uuid4().hex
    token = _encode({
        "iss": ISSUER,
        "aud": ADMIN_AUDIENCE,
        "type": "refresh",
        "sub": str(user.user_id),
        "ver": int(user.token_version),
        "jti": jti,
        "fam": fam,
    }, secret=cfg.jwt_secret, ttl=refresh_token_ttl())
    return token, jti


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_token(token: str, *, expected_type: str,
                 audience: str = ADMIN_AUDIENCE) -> dict[str, Any]:
    """Подпись, exp, iss, aud и type — иначе 401."""
    cfg = get_settings()
    try:
        payload = jwt.decode(
            token, cfg.jwt_secret.get_secret_value(), algorithms=[ALGORITHM],
            audience=audience, issuer=ISSUER,
            options={"require_exp": True, "require_iat": True,
                     "require_aud": True, "require_iss": True,
                     "require_sub": True},
        )
    except JWTError as exc:
        raise _unauthorized("invalid or expired token") from exc
    if payload.get("type") != expected_type:
        raise _unauthorized(f"expected {expected_type} token")
    return payload


def issue_sse_token(user: AuthUser) -> str:
    """Короткий (60 с) токен для ``GET /events/stream?token=…``: EventSource
    не умеет слать заголовок Authorization."""
    return _encode({
        "iss": ISSUER,
        "aud": SSE_AUDIENCE,
        "type": "sse",
        "sub": user.user_id,
        "ver": int(user.token_version),
        "jti": uuid.uuid4().hex,
    }, secret=get_settings().jwt_secret,
        ttl=timedelta(seconds=SSE_TOKEN_TTL_SECONDS))


def issue_service_token(iss: str = SERVICE_ISSUER) -> str:
    """Сервисный токен для вызовов recommendation_api (``SERVICE_JWT_SECRET``)."""
    return _encode({
        "iss": iss,
        "aud": SERVICE_AUDIENCE,
        "sub": iss,
        "type": "service",
    }, secret=get_settings().service_jwt_secret,
        ttl=timedelta(seconds=SERVICE_TOKEN_TTL_SECONDS))


# ---------------------------------------------------------------------------
# Проверка по БД
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AuthUser:
    """Аутентифицированный пользователь; ``role`` — текущая роль из БД."""

    user_id: str
    email: str
    role: str
    token_version: int = 0
    must_change_password: bool = False


def _auth_user(user: AdminUser) -> AuthUser:
    return AuthUser(
        user_id=str(user.user_id),
        email=user.email,
        role=str(user.role),
        token_version=int(user.token_version),
        must_change_password=bool(user.must_change_password),
    )


async def load_token_user(session: AsyncSession, claims: dict[str, Any]) -> AdminUser:
    """Строка ``admin_users`` владельца токена: один запрос по PK.

    401, если пользователя нет, он деактивирован или ``ver`` токена устарел
    (роль/статус/пароль менялись после выдачи токена).
    """
    ver = claims.get("ver")
    try:
        user_id = uuid.UUID(str(claims.get("sub")))
    except ValueError as exc:
        raise _unauthorized("invalid or expired token") from exc
    if type(ver) is not int:
        raise _unauthorized("invalid or expired token")
    user = await session.get(AdminUser, user_id)
    if user is None or not user.is_active or user.token_version != ver:
        raise _unauthorized("token revoked")
    return user


def _password_change_required() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                         detail=PASSWORD_CHANGE_REQUIRED)


async def _authenticate(creds: HTTPAuthorizationCredentials | None,
                        session: AsyncSession, *,
                        allow_pwd_change: bool) -> AuthUser:
    if creds is None:
        raise _unauthorized("authentication required")
    claims = decode_token(creds.credentials, expected_type="access")
    user = await load_token_user(session, claims)
    if not allow_pwd_change and (user.must_change_password
                                 or claims.get("pwd_change")):
        raise _password_change_required()
    return _auth_user(user)


# ---------------------------------------------------------------------------
# FastAPI-зависимости
# ---------------------------------------------------------------------------
async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session_dep),
) -> AuthUser:
    """Access-токен + проверка по БД; ``pwd_change``-токен → 403."""
    return await _authenticate(creds, session, allow_pwd_change=False)


async def get_current_user_allow_pwd_change(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session_dep),
) -> AuthUser:
    """Как ``get_current_user``, но пускает с ``pwd_change`` — только для
    ``GET /auth/me`` и ``POST /auth/change-password``."""
    return await _authenticate(creds, session, allow_pwd_change=True)


def decode_sse_token(token: str | None) -> dict[str, Any]:
    """SSE-токен из ``?token=``: подпись, тип ``sse``, aud ``cashback-sse``."""
    if not token:
        raise _unauthorized("authentication required")
    return decode_token(token, expected_type="sse", audience=SSE_AUDIENCE)


async def authenticate_sse_claims(claims: dict[str, Any],
                                  session: AsyncSession) -> AuthUser:
    """Те же проверки по БД, что у ``get_current_user`` (``sub``/``ver``) —
    при подключении к потоку и при перепроверке уже открытого."""
    user = await load_token_user(session, claims)
    if user.must_change_password:
        raise _password_change_required()
    return _auth_user(user)


async def authenticate_sse_token(token: str | None,
                                 session: AsyncSession) -> AuthUser:
    """SSE-токен из ``?token=`` и проверки по БД."""
    return await authenticate_sse_claims(decode_sse_token(token), session)


def require_role(*roles: str):
    """Фабрика зависимостей: 401 без токена, 403 при недостаточной роли
    (роль — текущая из БД)."""
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
