"""Проверка входящего JWT (service-to-service auth, beyond-plan).

recommendation_api больше не открыт: ``/recommendations/*`` требует
валидный HS256-токен, подписанный общим ``JWT_SECRET`` (тот же секрет,
что выпускает campaign_manager). Принимаются два типа токенов:

* ``service`` — машинный вызов (mobile_api → rec_api);
* ``access``  — админ/фронтенд (тот же токен, что выдаёт /auth/login),
  чтобы live-SHAP на странице «ML-объяснения» продолжал работать.

Health-эндпоинты остаются открытыми (k8s-пробы, health-пинг фронта).

Сознательное дублирование логики с campaign_manager/app/security.py:
сервисы не имеют общего пакета (зафиксировано в ADR-0002).
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings

ALGORITHM = "HS256"
_ACCEPTED_TYPES = {"service", "access"}

_bearer = HTTPBearer(auto_error=False)


async def require_caller(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """401 без токена/при плохой подписи; принимает service|access."""
    cfg = get_settings()
    if not cfg.auth_enabled:
        return {"type": "disabled"}
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(creds.credentials, cfg.jwt_secret,
                             algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if payload.get("type") not in _ACCEPTED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"token type {payload.get('type')!r} not accepted",
        )
    return payload
