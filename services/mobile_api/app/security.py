"""Аутентификация клиента мобильного BFF (stage B: B12, B19).

Каждый маршрут ``/v1/mobile/*`` требует ``Authorization: Bearer
<customer-токен>`` (контракт stage B, §2 и §7):

* HS256, подпись ``MOBILE_JWT_SECRET`` — отдельным ключом: mobile_api не
  хранит ни ``JWT_SECRET`` (админские токены), ни права выпускать
  customer-токены сам — их выпускает IdP банка (в демо —
  ``scripts/mint_customer_token.py``);
* ``aud = cashback-mobile``, ``iss = MOBILE_JWT_ISSUER``, ``type = customer``;
  ``exp``, ``iat``, ``iss``, ``aud`` и ``sub`` обязательны (python-jose без
  ``require_*`` молча пропускает токен без ``aud``/``exp``);
* ``sub`` — UUID клиента: всё, что BFF отдаёт или меняет, принадлежит ему.

Коды ответа: 401 — токена нет или он недействителен (подпись, срок, aud,
iss, type, отсутствующий/битый клейм); 403 ``forbidden`` — токен
действителен, но ``{user_id}`` пути — чужой. Чужая рекомендация в
``/respond`` — 404, как несуществующая (проверка владельца — в самом SQL
отклика, api/mobile.py): существование чужого оффера не раскрывается.

Health, /metrics и / открыты (k8s-пробы, Prometheus).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from app.config import get_settings

ALGORITHM = "HS256"
AUDIENCE = "cashback-mobile"
TOKEN_TYPE = "customer"

_DECODE_OPTIONS = {
    "require_exp": True,
    "require_iat": True,
    "require_iss": True,
    "require_aud": True,
    "require_sub": True,
}

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Customer:
    """Клиент банка, подтверждённый customer-токеном."""

    user_id: uuid.UUID


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def decode_customer_token(token: str) -> Customer:
    """Подпись, exp/iat/iss/aud/sub, type и формат sub; любое нарушение → 401."""
    settings = get_settings()
    try:
        payload = jwt.decode(
            token, settings.mobile_jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM], audience=AUDIENCE,
            issuer=settings.mobile_jwt_issuer, options=_DECODE_OPTIONS,
        )
    except JWTError as exc:
        raise _unauthorized("invalid or expired token") from exc
    if payload.get("type") != TOKEN_TYPE:
        raise _unauthorized("customer token required")
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise _unauthorized("invalid token subject") from exc
    return Customer(user_id=user_id)


async def require_customer(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> Customer:
    """Зависимость всего роутера /v1/mobile: без токена клиента — 401."""
    if creds is None:
        raise _unauthorized("authentication required")
    return decode_customer_token(creds.credentials)


async def require_own_user_id(
    user_id: uuid.UUID = Path(...),
    customer: Customer = Depends(require_customer),
) -> uuid.UUID:
    """``{user_id}`` пути — только свой (``sub`` токена); чужой → 403."""
    if user_id != customer.user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="forbidden")
    return user_id
