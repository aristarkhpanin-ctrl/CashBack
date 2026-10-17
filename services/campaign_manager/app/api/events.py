"""Server-Sent Events — realtime-канал дашборда (фаза 19; этап B — B36).

* ``POST /events/token`` (право ``dashboard``) → короткий (60 с) sse-токен.
  EventSource в браузере не умеет слать заголовок Authorization, поэтому
  токен едет в ``?token=``: у него своя аудитория (``cashback-sse``) и
  тип — access-токен вместо него не примут и наоборот, а короткий срок
  ограничивает вред от URL, осевшего в логах прокси.
* ``GET /events/stream?token=…`` — долгое соединение с агрегатами начислений
  (дельты budget_spent по кампаниям). Без токена или с невалидным — 401
  (раньше канал был открыт всем, а CORS ``*`` с credentials отдавал его
  любому сайту). Подписчиков на процесс не больше ``SSE_MAX_SUBSCRIBERS``,
  сверх лимита и во время остановки процесса — 503; у одного пользователя
  не больше ``SSE_MAX_SUBSCRIBERS_PER_USER`` — сверх него 429.
* При подключении право ``dashboard`` проверяется заново (его могли снять
  после выдачи токена), а открытый поток перепроверяет доступ раз в
  ``REVALIDATE_INTERVAL`` и закрывается, когда доступа больше нет.
* sse-токен одноразовый: ``jti`` гасится в Redis (``auth:sse_used:{jti}``,
  TTL = срок токена) при подключении, повтор — 401. URL с токеном оседает
  в логах прокси/ingress — из лога им уже не подключиться. Redis
  недоступен → 503 (fail-closed, как у refresh): realtime опционален,
  дашборд опрашивает API сам.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.events import (
    EventBroker,
    SubscriptionRefused,
    TooManyUserSubscriptions,
    sse_response_stream,
)
from app.rbac import permissions_for, require_permission
from app.security import (
    SSE_TOKEN_TTL_SECONDS,
    AuthUser,
    authenticate_sse_claims,
    decode_sse_token,
    issue_sse_token,
)

log = structlog.get_logger("api.events")

router = APIRouter(prefix="/events", tags=["events"])


class SseTokenResponse(BaseModel):
    token: str
    expires_in: int


async def _stream_access(claims: dict[str, Any], session: AsyncSession) -> AuthUser:
    """Учётка активна, ``ver`` текущий, пароль менять не нужно — и у роли
    всё ещё есть право ``dashboard`` (снятие права ``token_version`` не
    трогает)."""
    user = await authenticate_sse_claims(claims, session)
    if not (await permissions_for(user.role, session))["dashboard"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"role {user.role} lacks permission 'dashboard'")
    return user


async def _use_once(request: Request, claims: dict[str, Any]) -> None:
    """Гасит ``jti`` sse-токена: второе подключение с ним — 401."""
    jti = claims.get("jti")
    if not (isinstance(jti, str) and jti):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    redis = getattr(request.app.state, "redis", None)
    try:
        if redis is None:
            raise ConnectionError("redis client is not initialised")
        fresh = await redis.set(f"auth:sse_used:{jti}", "1", nx=True,
                                ex=SSE_TOKEN_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        log.error("session_store_unavailable", error=str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="session store unavailable",
                            headers={"Retry-After": "5"}) from exc
    if not fresh:
        log.warning("sse_token_reused", sub=claims.get("sub"))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="sse token already used",
                            headers={"WWW-Authenticate": "Bearer"})


async def require_sse_token(
    request: Request,
    token: str | None = Query(default=None,
                              description="sse-токен из POST /events/token"),
) -> AuthUser:
    """sse-токен из ``?token=``: те же проверки по БД, что у access-токена,
    право ``dashboard`` и одноразовость.

    Своя короткая сессия, а не ``get_session_dep``: соединение из пула
    не должно жить столько же, сколько поток (часы), — сотня открытых
    дашбордов исчерпала бы пул."""
    claims = decode_sse_token(token)
    async with request.app.state.sessionmaker() as session:
        user = await _stream_access(claims, session)
    await _use_once(request, claims)
    return user


def _revalidator(request: Request, user: AuthUser) -> Callable[[], Awaitable[bool]]:
    """Перепроверка открытого потока — те же условия, что при подключении,
    каждый раз в новой короткой сессии."""
    sessionmaker = request.app.state.sessionmaker
    claims = {"sub": user.user_id, "ver": user.token_version}

    async def _still_allowed() -> bool:
        try:
            async with sessionmaker() as session:
                await _stream_access(claims, session)
        except HTTPException as exc:
            log.info("sse_stream_revoked", sub=user.user_id, reason=exc.detail)
            return False
        except Exception as exc:  # noqa: BLE001 — БД недоступна: поток закрывается, клиент переподключится
            log.warning("sse_revalidation_failed", sub=user.user_id, error=str(exc))
            return False
        return True

    return _still_allowed


@router.post("/token", response_model=SseTokenResponse)
async def issue_stream_token(
    user: AuthUser = Depends(require_permission("dashboard")),
) -> SseTokenResponse:
    return SseTokenResponse(token=issue_sse_token(user),
                            expires_in=SSE_TOKEN_TTL_SECONDS)


@router.get("/stream")
async def stream(
    request: Request,
    user: AuthUser = Depends(require_sse_token),
) -> StreamingResponse:
    broker: EventBroker = request.app.state.event_broker
    try:
        # Подписка — до ответа: отказ ещё можно отдать статусом 503/429.
        q = broker.subscribe(owner=user.user_id)
    except SubscriptionRefused as exc:
        log.warning("sse_subscription_refused", reason=str(exc), sub=user.user_id,
                    subscribers=broker.n_subscribers)
        code = (status.HTTP_429_TOO_MANY_REQUESTS
                if isinstance(exc, TooManyUserSubscriptions)
                else status.HTTP_503_SERVICE_UNAVAILABLE)
        raise HTTPException(status_code=code, detail=str(exc),
                            headers={"Retry-After": "5"}) from exc
    return StreamingResponse(
        sse_response_stream(broker, q, revalidate=_revalidator(request, user)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Явно отключаем буферизацию nginx для этого ответа.
            "X-Accel-Buffering": "no",
        },
        # Генератор отписывается сам, но если клиент ушёл до первой
        # итерации, генератор так и не стартует — отписка здесь.
        background=BackgroundTask(broker.unsubscribe, q),
    )
