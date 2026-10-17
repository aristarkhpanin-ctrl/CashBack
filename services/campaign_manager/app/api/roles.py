"""Матрица прав ролей (фаза 26, этап B — B35).

``GET  /roles/permissions``          — все роли → права (право ``users``).
``PATCH /roles/{role}/permissions``  — обновить права роли (только роль
ADMIN — не делегируется, иначе держатель ``users`` выдал бы себе любые
права); роль ADMIN неизменяема (403).

PATCH — один атомарный upsert со слиянием JSONB в Postgres
(``permissions || patch``): прежний read-merge-write в Python терял
переключения при параллельных PATCH. Кэша прав нет (``rbac``) — изменение
видно следующему запросу в любой реплике.
"""
from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import RolePermission
from app.rbac import PERMISSION_KEYS, effective_permissions, require_permission
from app.schemas import RolePermissionsUpdate
from app.security import AuthUser, get_current_user, require_role

log = structlog.get_logger("api.roles")

router = APIRouter(
    prefix="/roles", tags=["roles"],
    dependencies=[Depends(get_current_user)],
)

_ROLES = ("ADMIN", "MARKETER", "ANALYST")

# Строки нет (007 сеет все три роли, но таблицу могли почистить) → вставка
# патча; есть → слияние под блокировкой строки. Ключи, которых нет в
# строке, при чтении берутся из дефолтов роли (rbac.effective_permissions).
_MERGE_PERMISSIONS = text(
    """
    INSERT INTO role_permissions AS rp (role, permissions, updated_at, updated_by)
    VALUES (:role, :patch, :updated_at, :updated_by)
    ON CONFLICT (role) DO UPDATE
       SET permissions = rp.permissions || EXCLUDED.permissions,
           updated_at  = EXCLUDED.updated_at,
           updated_by  = EXCLUDED.updated_by
    RETURNING permissions
    """
).bindparams(bindparam("patch", type_=JSONB)).columns(permissions=JSONB)


@router.get("/permissions", response_model=dict[str, dict[str, bool]],
            dependencies=[Depends(require_permission("users"))])
async def get_permissions(
    session: AsyncSession = Depends(get_session_dep),
) -> dict[str, dict[str, bool]]:
    rows = (await session.execute(select(RolePermission))).scalars().all()
    by_role = {r.role: r.permissions for r in rows}
    # Дополняем дефолтами отсутствующие роли/ключи (устойчивость к миграции).
    return {role: effective_permissions(role, by_role.get(role)) for role in _ROLES}


@router.patch("/{role}/permissions", response_model=dict[str, bool],
              dependencies=[Depends(require_role("ADMIN"))])
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

    stored = (await session.execute(_MERGE_PERMISSIONS, {
        "role": role,
        "patch": {k: bool(v) for k, v in payload.permissions.items()},
        "updated_at": datetime.now(UTC),
        "updated_by": current.email,
    })).scalar_one()
    await session.commit()
    log.info("role_permissions_updated", role=role, by=current.email,
             patch=payload.permissions)
    return effective_permissions(role, stored)
