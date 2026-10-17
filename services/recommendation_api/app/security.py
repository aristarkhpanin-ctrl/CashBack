"""Проверка входящих service-токенов (stage B: B19, B27).

recommendation_api принимает ТОЛЬКО машинные токены (контракт stage B, §1–2):

* HS256, подпись ``SERVICE_JWT_SECRET`` — отдельным от ``JWT_SECRET``
  ключом: rec_api не хранит секрет админских токенов, а админский
  access-токен (aud ``cashback-admin``, type ``access``) не принимается
  ни на одном маршруте;
* ``aud = recommendation-api``, ``type = service``; ``exp``, ``iat``,
  ``iss`` и ``aud`` обязательны (python-jose без ``require_aud`` молча
  пропускает токен вообще без ``aud``);
* издатель (``iss``) сверяется со списком, разрешённым маршруту:
  боевая выдача ``GET /recommendations/{user_id}`` (пишет в Postgres и
  Kafka, тратит anti-fatigue и дневной лимит) — только ``mobile-api``;
  read-only ``GET /recommendations/{user_id}/explain`` (страница
  «ML-объяснения» через campaign_manager) — только ``campaign-manager``.

Коды ответа: 401 — токена нет или он недействителен (подпись, срок, aud,
type, отсутствующий/битый клейм); 403 — токен действителен, но его
издателю этот маршрут не разрешён (вызывающий опознан, но не авторизован).

Health и /metrics открыты (k8s-пробы, Prometheus). Тесты подменяют
зависимости ``require_mobile_api`` / ``require_campaign_manager`` через
``app.dependency_overrides`` — флага отключения проверки нет.

Сознательное дублирование констант с минтерами в mobile_api и
campaign_manager: общего пакета у сервисов нет (ADR-0002).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings

ALGORITHM = "HS256"
AUDIENCE = "recommendation-api"
TOKEN_TYPE = "service"
ISSUER_MOBILE_API = "mobile-api"
ISSUER_CAMPAIGN_MANAGER = "campaign-manager"

_DECODE_OPTIONS = {
    "require_exp": True,
    "require_iat": True,
    "require_iss": True,
    "require_aud": True,
}

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_service_token(token: str) -> dict:
    """Подпись, exp/iat/iss/aud и type; любое нарушение → 401."""
    secret = get_settings().service_jwt_secret.get_secret_value()
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM],
                             audience=AUDIENCE, options=_DECODE_OPTIONS)
    except JWTError as exc:
        raise _unauthorized("invalid or expired token") from exc
    if payload.get("type") != TOKEN_TYPE:
        raise _unauthorized("service token required")
    if not isinstance(payload.get("iss"), str):
        raise _unauthorized("invalid token issuer")
    return payload


def _authorize(creds: HTTPAuthorizationCredentials | None,
               allowed_issuers: tuple[str, ...]) -> dict:
    if creds is None:
        raise _unauthorized("authentication required")
    payload = decode_service_token(creds.credentials)
    if payload["iss"] not in allowed_issuers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="caller is not allowed to use this endpoint",
        )
    return payload


async def require_mobile_api(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Боевая выдача с побочными эффектами — только mobile_api."""
    return _authorize(creds, (ISSUER_MOBILE_API,))


async def require_campaign_manager(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Read-only объяснение для админки — только campaign_manager."""
    return _authorize(creds, (ISSUER_CAMPAIGN_MANAGER,))
