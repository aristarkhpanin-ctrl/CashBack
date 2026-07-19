"""Матрица прав ролей (фаза 26).

``GET  /roles/permissions``          — все роли → права (только ADMIN).
``PATCH /roles/{role}/permissions``  — обновить права роли (только ADMIN);
роль ADMIN неизменяема (403). После записи инвалидируется кэш rbac, чтобы
``require_permission`` увидел изменения сразу.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import RolePermission
from app.rbac import (
    DEFAULT_PERMISSIONS,
    PERMISSION_KEYS,
    invalidate_cache,
)
from app.schemas import RolePermissionsUpdate
from app.security import AuthUser, get_current_user, require_role

log = structlog.get_logger("api.roles")

router = APIRouter(
    prefix="/roles", tags=["roles"],
    dependencies=[Depends(require_role("ADMIN"))],
)

_ROLES = ("ADMIN", "MARKETER", "ANALYST")


@router.get("/permissions", response_model=dict[str, dict[str, bool]])
async def get_permissions(
    session: AsyncSession = Depends(get_session_dep),
) -> dict[str, dict[str, bool]]:
    rows = (await session.execute(select(RolePermission))).scalars().all()
    by_role = {r.role: dict(r.permissions or {}) for r in rows}
    # Дополняем дефолтами отсутствующие роли/ключи (устойчивость к миграции).
    out: dict[str, dict[str, bool]] = {}
    for role in _ROLES:
        base = dict(DEFAULT_PERMISSIONS.get(role, {}))
        base.update(by_role.get(role, {}))
        out[role] = {k: bool(base.get(k, False)) for k in PERMISSION_KEYS}
    return out


@router.patch("/{role}/permissions", response_model=dict[str, bool])
async def update_permissions(
    payload: RolePermissionsUpdate,
    role: str = Path(..., pattern="^(ADMIN|MARKETER|ANALYST)$"),
    current: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> dict[str, bool]:
    if role == "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ADMIN role always has full access and cannot be modified",
        )
    # Валидируем ключи — молча игнорировать опечатки нельзя.
    unknown = set(payload.permissions) - set(PERMISSION_KEYS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown permission keys: {sorted(unknown)}",
        )

    row = await session.get(RolePermission, role)
    now = datetime.now(UTC)
    merged = dict(DEFAULT_PERMISSIONS.get(role, {}))
    if row is not None:
        merged.update(row.permissions or {})
    merged.update({k: bool(v) for k, v in payload.permissions.items()})
    merged = {k: bool(merged.get(k, False)) for k in PERMISSION_KEYS}

    if row is None:
        row = RolePermission(role=role)
        session.add(row)
    row.permissions = merged
    row.updated_by = current.email
    row.updated_at = now
    await session.commit()
    invalidate_cache()  # require_permission увидит изменения сразу
    log.info("role_permissions_updated", role=role, by=current.email)
    return merged
