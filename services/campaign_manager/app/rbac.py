"""Динамический RBAC (фаза 26, этап B — B35): права per-роль из ``role_permissions``.

``require_permission(key)`` дополняет ``security.require_role``: проверка идёт
по праву, а не по фикс-роли, поэтому admin может менять матрицу на лету.

Строка роли читается на КАЖДОМ запросе (один запрос по PK, без кэша в
процессе): кэш на 30 с жил в каждой реплике свой, и снятое право продолжало
действовать на других подах (B35). Роль берётся из БД (``get_current_user``),
а не из клейма токена.

Действующие права роли = дефолты роли, поверх — строка таблицы; только
известные ключи, право есть лишь при JSON ``true``. Тем же расчётом
пользуются ``GET /roles/permissions`` и ``/auth/me`` — UI показывает ровно
ту матрицу, что проверяет сервер.

Инварианты:
* роль ADMIN всегда имеет полный доступ (короткое замыкание до БД) — её
  нельзя ограничить, иначе стенд можно окирпичить;
* право ``users`` делегируемо, роль ADMIN — нет: только ADMIN создаёт или
  повышает пользователя до ADMIN и меняет учётки ADMIN
  (``forbid_admin_escalation``), иначе MARKETER с ``users`` повысит себя.
"""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import RolePermission
from app.security import AuthUser, get_current_user

PERMISSION_KEYS = (
    "dashboard", "campaigns_view", "campaigns_create", "campaigns_edit",
    "campaigns_delete", "analytics", "users",
)

_ALL = {k: True for k in PERMISSION_KEYS}
DEFAULT_PERMISSIONS: dict[str, dict[str, bool]] = {
    "ADMIN": dict(_ALL),
    "MARKETER": {**_ALL, "campaigns_delete": False, "analytics": False, "users": False},
    "ANALYST": {
        "dashboard": True, "campaigns_view": True, "campaigns_create": False,
        "campaigns_edit": False, "campaigns_delete": False, "analytics": True,
        "users": False,
    },
}


def effective_permissions(role: str, stored: dict[str, Any] | None) -> dict[str, bool]:
    """Дефолты роли, поверх — сохранённая строка; только известные ключи."""
    merged: dict[str, Any] = {**DEFAULT_PERMISSIONS.get(role, {}), **(stored or {})}
    return {k: merged.get(k) is True for k in PERMISSION_KEYS}


async def permissions_for(role: str, session: AsyncSession) -> dict[str, bool]:
    """Права одной роли — из БД на каждый вызов (ADMIN — всегда полный доступ)."""
    if role == "ADMIN":
        return dict(_ALL)
    row = await session.get(RolePermission, role)
    return effective_permissions(role, row.permissions if row is not None else None)


def require_permission(key: str):
    """Зависимость: 403, если у текущей роли нет права ``key``. ADMIN — всегда ок."""
    if key not in PERMISSION_KEYS:
        # Опечатка в ключе дала бы вечный 403 — пусть лучше упадёт импорт.
        raise ValueError(f"unknown permission key: {key!r}")

    async def _dep(
        user: AuthUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> AuthUser:
        perms = await permissions_for(user.role, session)
        if not perms[key]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {user.role} lacks permission '{key}'",
            )
        return user

    return _dep


def forbid_admin_escalation(current: AuthUser, *roles: str | None) -> None:
    """403, если не-ADMIN трогает роль ADMIN: создаёт/повышает до ADMIN
    (новая роль) или меняет учётку ADMIN (текущая роль цели)."""
    if current.role != "ADMIN" and "ADMIN" in roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only ADMIN can grant the ADMIN role or change ADMIN accounts",
        )
