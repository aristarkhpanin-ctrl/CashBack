"""Динамический RBAC (фаза 26): права per-роль из таблицы role_permissions.

``require_permission(key)`` дополняет ``security.require_role``: проверка идёт
по праву, а не по фикс-роли, поэтому admin может менять матрицу на лету.
Права кэшируются в процессе (TTL + инвалидация на PATCH) — таблица маленькая
и меняется редко, но дёргается на каждой защищённой мутации.

Инвариант: роль ADMIN всегда имеет полный доступ (короткое замыкание до БД) —
её нельзя ограничить, иначе стенд можно окирпичить.
"""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
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

_TTL = 30.0
_cache: dict[str, dict[str, bool]] | None = None
_cache_ts = 0.0


def invalidate_cache() -> None:
    global _cache
    _cache = None


async def load_permissions(session: AsyncSession) -> dict[str, dict[str, bool]]:
    """Все роли → права (кэш TTL). Пустая таблица → дефолты."""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache is not None and now - _cache_ts < _TTL:
        return _cache
    rows = (await session.execute(select(RolePermission))).scalars().all()
    loaded = {r.role: dict(r.permissions or {}) for r in rows}
    _cache = loaded or {k: dict(v) for k, v in DEFAULT_PERMISSIONS.items()}
    _cache_ts = now
    return _cache


async def permissions_for(role: str, session: AsyncSession) -> dict[str, bool]:
    """Права одной роли (ADMIN — всегда полный доступ)."""
    if role == "ADMIN":
        return dict(_ALL)
    perms = await load_permissions(session)
    return perms.get(role, DEFAULT_PERMISSIONS.get(role, {}))


def require_permission(key: str):
    """Зависимость: 403, если у роли снято право ``key``. ADMIN — всегда ок."""
    async def _dep(
        user: AuthUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session_dep),
    ) -> AuthUser:
        if user.role == "ADMIN":
            return user
        perms = await permissions_for(user.role, session)
        if not perms.get(key, False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {user.role} lacks permission '{key}'",
            )
        return user

    return _dep
