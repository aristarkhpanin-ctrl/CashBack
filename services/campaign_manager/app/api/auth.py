"""Аутентификация и управление пользователями админ-панели (фаза 15).

* ``POST /auth/login``   — пара JWT-токенов по email+паролю
* ``POST /auth/refresh`` — новая пара по refresh-токену (с проверкой
  ``is_active`` в БД: отключённый пользователь теряет доступ максимум
  через TTL access-токена)
* ``GET  /auth/me``      — профиль текущего пользователя
* ``GET/POST/PATCH /auth/users`` — управление пользователями (только ADMIN;
  DELETE отсутствует сознательно — деактивация через ``is_active=false``
  сохраняет аудит-след)
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import AdminUser
from app.schemas import (
    AdminUserCreate,
    AdminUserResponse,
    AdminUserUpdate,
    LoginRequest,
    RefreshRequest,
    TokenPairResponse,
)
from app.security import (
    AuthUser,
    decode_token,
    get_current_user,
    hash_password,
    issue_token_pair,
    require_role,
    verify_password,
)

log = structlog.get_logger("api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid email or password",
)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@router.post("/login", response_model=TokenPairResponse)
async def login(
    payload: LoginRequest,
    session: AsyncSession = Depends(get_session_dep),
) -> TokenPairResponse:
    user = (
        await session.execute(
            select(AdminUser).where(AdminUser.email == payload.email.lower())
        )
    ).scalar_one_or_none()
    # verify_password выполняется и для несуществующего пользователя
    # (фиктивный хэш), чтобы не выдавать наличие email по времени ответа.
    hashed = user.password_hash if user else hash_password("timing-equalizer")
    ok = verify_password(payload.password, hashed)
    if user is None or not ok or not user.is_active:
        log.warning("login_failed", email=payload.email)
        raise _CREDENTIALS_ERROR

    user.last_login_at = datetime.now(UTC)
    await session.commit()
    log.info("login_ok", email=user.email, role=str(user.role))
    return TokenPairResponse(**issue_token_pair(
        user_id=str(user.user_id), email=user.email, role=str(user.role),
    ))


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_session_dep),
) -> TokenPairResponse:
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    user = await session.get(AdminUser, uuid.UUID(claims["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="user disabled or deleted")
    return TokenPairResponse(**issue_token_pair(
        user_id=str(user.user_id), email=user.email, role=str(user.role),
    ))


@router.get("/me", response_model=AdminUserResponse)
async def me(
    current: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUserResponse:
    user = await session.get(AdminUser, uuid.UUID(current.user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="user deleted")
    return AdminUserResponse.model_validate(user)


# ---------------------------------------------------------------------------
# User management (ADMIN only)
# ---------------------------------------------------------------------------
@router.get("/users", response_model=list[AdminUserResponse],
            dependencies=[Depends(require_role("ADMIN"))])
async def list_users(
    session: AsyncSession = Depends(get_session_dep),
) -> list[AdminUserResponse]:
    rows = (
        await session.execute(select(AdminUser).order_by(AdminUser.created_at))
    ).scalars().all()
    return [AdminUserResponse.model_validate(u) for u in rows]


@router.post("/users", response_model=AdminUserResponse, status_code=201,
             dependencies=[Depends(require_role("ADMIN"))])
async def create_user(
    payload: AdminUserCreate,
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUserResponse:
    exists = (
        await session.execute(
            select(AdminUser.user_id).where(AdminUser.email == payload.email.lower())
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    user = AdminUser(
        user_id=uuid.uuid4(),
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
        role=payload.role,
        is_active=True,
        created_at=datetime.now(UTC),
    )
    session.add(user)
    await session.commit()
    log.info("user_created", email=user.email, role=payload.role)
    return AdminUserResponse.model_validate(user)


@router.patch("/users/{user_id}", response_model=AdminUserResponse,
              dependencies=[Depends(require_role("ADMIN"))])
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    current: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUserResponse:
    user = await session.get(AdminUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    data = payload.model_dump(exclude_unset=True)
    # Админ не может понизить роль или деактивировать сам себя —
    # иначе стенд можно окирпичить одним запросом.
    if str(user_id) == current.user_id:
        if data.get("role") not in (None, "ADMIN") or data.get("is_active") is False:
            raise HTTPException(
                status_code=409,
                detail="cannot demote or deactivate your own account",
            )

    password = data.pop("password", None)
    if password:
        user.password_hash = hash_password(password)
    for field, value in data.items():
        setattr(user, field, value)
    await session.commit()
    log.info("user_updated", email=user.email, fields=sorted(data))
    return AdminUserResponse.model_validate(user)
