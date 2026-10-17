"""Аутентификация и управление пользователями админ-панели (фаза 15, этап B).

* ``POST /auth/login``   — пара JWT-токенов по email+паролю; троттлинг в
  Redis (B20), ровно один bcrypt на запрос в пуле потоков (B35)
* ``POST /auth/refresh`` — ротация: refresh-токен одноразовый; повторное
  предъявление отзывает всё семейство (B35)
* ``POST /auth/logout``  — отзыв семейства refresh-токенов
* ``POST /auth/change-password`` — смена пароля (в т.ч. обязательная после
  bootstrap, ``must_change_password``) → новая пара, старые токены гаснут
* ``GET  /auth/me``      — профиль текущего пользователя
* ``GET/POST/PATCH /auth/users`` — управление пользователями (право
  ``users``; роль ADMIN выдаёт и учётки ADMIN меняет только ADMIN —
  ``rbac.forbid_admin_escalation``; DELETE отсутствует сознательно —
  деактивация через ``is_active=false`` сохраняет аудит-след)

Ключи Redis:
* ``auth:refresh:{jti}`` → ``{"uid", "fam"}``, TTL = срок refresh-токена —
  пишется при выдаче, забирается атомарным GETDEL при обмене;
* ``auth:refresh_fam_revoked:{fam}`` — отозванное семейство (TTL 7 дней);
* ``auth:login_fail:{sha256(email)}:{ip}`` / ``auth:login_ip:{ip}`` —
  счётчики попыток входа в окне ``LOGIN_THROTTLE_WINDOW_SECONDS``.

Отказ Redis: логин работает без троттлинга (fail-open, error-лог —
доступность админки важнее, перебор и так ограничен стоимостью bcrypt),
а refresh/logout отвечают 503 (fail-closed — отзыв нельзя проверить).
Все refresh-семейства пользователя гасит ``token_version`` в БД (смена
пароля/роли/статуса): ``/auth/refresh`` сверяет ``ver`` с текущим.

IP клиента — ``request.client.host``. За nginx/ingress uvicorn подставляет
адрес из X-Forwarded-For только для доверенных прокси (``--proxy-headers``
+ ``FORWARDED_ALLOW_IPS``); без этого все попытки выглядят как из одного IP.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session_dep
from app.models import AdminUser
from app.rbac import forbid_admin_escalation, permissions_for, require_permission
from app.schemas import (
    AdminUserCreate,
    AdminUserResponse,
    AdminUserUpdate,
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenPairResponse,
)
from app.security import (
    DUMMY_PASSWORD_HASH,
    AuthUser,
    decode_token,
    get_current_user,
    get_current_user_allow_pwd_change,
    hash_password_async,
    load_token_user,
    make_access_token,
    make_refresh_token,
    password_policy_violation,
    refresh_token_ttl,
    verify_password_async,
)

log = structlog.get_logger("api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid email or password",
)
_REDIS_ERRORS = (RedisError, OSError)


def _refresh_key(jti: str) -> str:
    return f"auth:refresh:{jti}"


def _family_revoked_key(fam: str) -> str:
    return f"auth:refresh_fam_revoked:{fam}"


def _redis(request: Request) -> Any:
    # Клиент кладёт lifespan; без него (приложение без lifespan) ведём себя
    # как при недоступном Redis.
    return getattr(request.app.state, "redis", None)


def _require_redis(redis: Any) -> Any:
    if redis is None:
        raise ConnectionError("redis client is not initialised")
    return redis


def _session_store_down(exc: BaseException) -> HTTPException:
    log.error("session_store_unavailable", error=str(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                         detail="session store unavailable")


async def _lock_user(session: AsyncSession, user_id: uuid.UUID) -> AdminUser | None:
    """Строка ``admin_users`` под ``SELECT … FOR UPDATE`` — свежая, а не из
    identity map: смена пароля и правка учётки админом читают и пишут
    ``token_version``/``password_hash`` одной строки. Без блокировки обе
    читали k и писали k+1 — сброс пароля админом молча терялся (B35)."""
    stmt = (select(AdminUser).where(AdminUser.user_id == user_id)
            .with_for_update().execution_options(populate_existing=True))
    return (await session.execute(stmt)).scalar_one_or_none()


def _refresh_revoked() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="refresh token reused or revoked",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# Троттлинг входа
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttle_keys(email: str, ip: str) -> tuple[str, str]:
    digest = hashlib.sha256(email.encode()).hexdigest()[:32]
    return f"auth:login_fail:{digest}:{ip}", f"auth:login_ip:{ip}"


async def _hit(redis: Any, key: str, window: int) -> int:
    n = int(await redis.incr(key))
    # Окно отсчитывается от первой попытки и не продлевается; ключ без TTL
    # (процесс упал между INCR и EXPIRE) чинится следующей попыткой. Не
    # EXPIRE … NX: его нет в Redis < 7.0, и там каждая попытка молча
    # уходила бы в fail-open.
    if n == 1 or int(await redis.ttl(key)) == -1:
        await redis.expire(key, window)
    return n


async def _throttle_attempt(redis: Any, email: str, ip: str) -> int | None:
    """Засчитывает попытку; ``Retry-After`` в секундах, если лимит исчерпан.

    Попытка сразу считается неудачной (INCR до bcrypt), а успешная сбрасывает
    счётчик (email, IP) — параллельные запросы не проскочат между проверкой
    и записью. Лимиты: ``login_max_failures_per_email_ip`` на пару и
    ``login_max_attempts_per_ip`` на IP за окно.
    """
    cfg = get_settings()
    window = cfg.login_throttle_window_seconds
    pair_key, ip_key = _throttle_keys(email, ip)
    try:
        redis = _require_redis(redis)
        pair_n = await _hit(redis, pair_key, window)
        ip_n = await _hit(redis, ip_key, window)
        blocked = (pair_key if pair_n > cfg.login_max_failures_per_email_ip
                   else ip_key if ip_n > cfg.login_max_attempts_per_ip
                   else None)
        if blocked is None:
            return None
        ttl = int(await redis.ttl(blocked))
        return ttl if ttl > 0 else window
    except _REDIS_ERRORS as exc:
        log.error("login_throttle_unavailable", error=str(exc))
        return None


async def _throttle_reset(redis: Any, email: str, ip: str) -> None:
    pair_key, _ = _throttle_keys(email, ip)
    try:
        await _require_redis(redis).delete(pair_key)
    except _REDIS_ERRORS as exc:
        log.error("login_throttle_unavailable", error=str(exc))


async def _check_throttle(redis: Any, email: str, ip: str) -> None:
    retry_after = await _throttle_attempt(redis, email, ip)
    if retry_after is not None:
        log.warning("login_throttled", email=email, ip=ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
async def _issue_pair(redis: Any, user: AdminUser, *, fam: str | None = None,
                      store_required: bool = False) -> TokenPairResponse:
    """Новая пара; ``jti`` refresh-токена записывается в Redis.

    Без записи refresh-токен не пройдёт ``/auth/refresh`` (fail-closed),
    поэтому логин и смена пароля при сбое Redis всё равно отдают пару — через
    TTL access-токена придётся войти заново. ``/auth/refresh`` же отвечает 503.
    """
    fam = fam or uuid.uuid4().hex
    refresh_token, jti = make_refresh_token(user, fam=fam)
    record = json.dumps({"uid": str(user.user_id), "fam": fam})
    try:
        await _require_redis(redis).set(
            _refresh_key(jti), record,
            ex=int(refresh_token_ttl().total_seconds()),
        )
    except _REDIS_ERRORS as exc:
        if store_required:
            raise _session_store_down(exc) from exc
        log.error("refresh_store_unavailable", error=str(exc))
    return TokenPairResponse(
        access_token=make_access_token(user),
        refresh_token=refresh_token,
        token_type="bearer",
        must_change_password=bool(user.must_change_password),
    )


@router.post("/login", response_model=TokenPairResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session_dep),
) -> TokenPairResponse:
    email = payload.email.strip().lower()
    ip = _client_ip(request)
    redis = _redis(request)
    await _check_throttle(redis, email, ip)

    user = (
        await session.execute(select(AdminUser).where(AdminUser.email == email))
    ).scalar_one_or_none()
    # Ровно одна проверка bcrypt на запрос — для неизвестного email против
    # фиктивного хэша той же стоимости: время ответа не выдаёт наличие email.
    hashed = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    ok = await verify_password_async(payload.password, hashed)
    if user is None or not ok or not user.is_active:
        log.warning("login_failed", email=email, ip=ip)
        raise _CREDENTIALS_ERROR

    await _throttle_reset(redis, email, ip)
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    log.info("login_ok", email=user.email, role=str(user.role),
             must_change_password=bool(user.must_change_password))
    return await _issue_pair(redis, user)


@router.post("/refresh", response_model=TokenPairResponse)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session_dep),
) -> TokenPairResponse:
    claims = decode_token(payload.refresh_token, expected_type="refresh")
    jti, fam = claims.get("jti"), claims.get("fam")
    if not (isinstance(jti, str) and jti and isinstance(fam, str) and fam):
        raise _refresh_revoked()

    redis = _redis(request)
    try:
        record = await _require_redis(redis).getdel(_refresh_key(jti))
        if record is None:
            # jti уже обменян или отозван: копия токена у кого-то ещё —
            # гасим всё семейство, в т.ч. токен, выданный «честному» клиенту.
            await redis.set(_family_revoked_key(fam), "1",
                            ex=int(refresh_token_ttl().total_seconds()))
            log.warning("refresh_reuse_detected", fam=fam, sub=claims.get("sub"))
            raise _refresh_revoked()
        family_revoked = await redis.exists(_family_revoked_key(fam))
    except _REDIS_ERRORS as exc:
        raise _session_store_down(exc) from exc

    stored = json.loads(record)
    if (family_revoked or stored.get("fam") != fam
            or stored.get("uid") != claims.get("sub")):
        raise _refresh_revoked()
    user = await load_token_user(session, claims)
    return await _issue_pair(redis, user, fam=fam, store_required=True)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, request: Request) -> Response:
    """Отзывает семейство refresh-токена. Идемпотентно: битый, просроченный
    или уже отозванный токен — тоже 204."""
    try:
        claims = decode_token(payload.refresh_token, expected_type="refresh")
    except HTTPException:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    fam, jti = claims.get("fam"), claims.get("jti")
    if isinstance(fam, str) and fam:
        try:
            redis = _require_redis(_redis(request))
            await redis.set(_family_revoked_key(fam), "1",
                            ex=int(refresh_token_ttl().total_seconds()))
            if isinstance(jti, str) and jti:
                await redis.delete(_refresh_key(jti))
        except _REDIS_ERRORS as exc:
            raise _session_store_down(exc) from exc
        log.info("logout", fam=fam, sub=claims.get("sub"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", response_model=TokenPairResponse)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current: AuthUser = Depends(get_current_user_allow_pwd_change),
    session: AsyncSession = Depends(get_session_dep),
) -> TokenPairResponse:
    redis, ip = _redis(request), _client_ip(request)
    # Подбор текущего пароля с украденным access-токеном — тот же перебор.
    await _check_throttle(redis, current.email, ip)
    # Пароль проверяется уже под блокировкой: если админ успел сбросить
    # пароль или отключить учётку, версия токена устарела — 401, а не
    # перезапись его сброса паролем держателя токена.
    user = await _lock_user(session, uuid.UUID(current.user_id))
    if (user is None or not user.is_active
            or user.token_version != current.token_version):
        raise HTTPException(status_code=401, detail="token revoked")
    if not await verify_password_async(payload.current_password, user.password_hash):
        log.warning("change_password_failed", email=user.email, ip=ip)
        raise HTTPException(status_code=401, detail="invalid current password")
    await _throttle_reset(redis, current.email, ip)

    problem = password_policy_violation(payload.new_password, email=user.email,
                                        current=payload.current_password)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)

    user.password_hash = await hash_password_async(payload.new_password)
    user.must_change_password = False
    user.password_changed_at = datetime.now(UTC)
    user.token_version += 1          # старые access/refresh/sse-токены гаснут
    await session.commit()
    log.info("password_changed", email=user.email)
    return await _issue_pair(redis, user)


@router.get("/me", response_model=AdminUserResponse)
async def me(
    current: AuthUser = Depends(get_current_user_allow_pwd_change),
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUserResponse:
    user = await session.get(AdminUser, uuid.UUID(current.user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="token revoked")
    resp = AdminUserResponse.model_validate(user)
    # Фаза 26: живые права роли — фронт гейтит меню/страницы по ним.
    resp.permissions = await permissions_for(str(user.role), session)
    return resp


# ---------------------------------------------------------------------------
# User management (право users)
# ---------------------------------------------------------------------------
_can_manage_users = require_permission("users")


@router.get("/users", response_model=list[AdminUserResponse],
            dependencies=[Depends(_can_manage_users)])
async def list_users(
    session: AsyncSession = Depends(get_session_dep),
) -> list[AdminUserResponse]:
    rows = (
        await session.execute(select(AdminUser).order_by(AdminUser.created_at))
    ).scalars().all()
    return [AdminUserResponse.model_validate(u) for u in rows]


@router.post("/users", response_model=AdminUserResponse, status_code=201,
             dependencies=[Depends(_can_manage_users)])
async def create_user(
    payload: AdminUserCreate,
    current: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUserResponse:
    forbid_admin_escalation(current, payload.role)
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
        password_hash=await hash_password_async(payload.password),
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
              dependencies=[Depends(_can_manage_users)])
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdate,
    current: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> AdminUserResponse:
    user = await _lock_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    data = payload.model_dump(exclude_unset=True)
    # Право users делегируемо, роль ADMIN — нет: иначе MARKETER с users
    # повысит себя до ADMIN или отключит админа.
    forbid_admin_escalation(current, str(user.role), data.get("role"))
    # Админ не может понизить роль или деактивировать сам себя —
    # иначе стенд можно окирпичить одним запросом.
    if str(user_id) == current.user_id:
        if data.get("role") not in (None, "ADMIN") or data.get("is_active") is False:
            raise HTTPException(
                status_code=409,
                detail="cannot demote or deactivate your own account",
            )

    # Смена роли/статуса/пароля гасит уже выданные токены (B35): иначе
    # разжалованный или отключённый админ действует до истечения токена.
    revoke = any(
        field in data and data[field] != getattr(user, field)
        for field in ("role", "is_active")
    )
    password = data.pop("password", None)
    if password:
        user.password_hash = await hash_password_async(password)
        user.password_changed_at = datetime.now(UTC)
        revoke = True
    for field, value in data.items():
        setattr(user, field, value)
    if revoke:
        user.token_version += 1
    await session.commit()
    log.info("user_updated", email=user.email, fields=sorted(data),
             password_reset=bool(password), tokens_revoked=revoke)
    return AdminUserResponse.model_validate(user)
