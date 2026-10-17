"""Unit tests for dynamic RBAC (phase 26, stage B — B35)."""
from __future__ import annotations

import pytest
from app import rbac
from app.models import RolePermission
from app.rbac import (
    DEFAULT_PERMISSIONS,
    PERMISSION_KEYS,
    effective_permissions,
    forbid_admin_escalation,
    permissions_for,
    require_permission,
)
from app.security import AuthUser
from fastapi import HTTPException

from tests.unit.fakes import FakeSession


def _user(role):
    return AuthUser(user_id="u", email="a@b.ru", role=role)


def _session(**rows: dict) -> FakeSession:
    s = FakeSession()
    for role, perms in rows.items():
        s.put(RolePermission(role=role, permissions=perms))
    return s


# ── effective_permissions ─────────────────────────────────────────────────────
def test_effective_permissions_overlays_row_on_role_defaults():
    perms = effective_permissions("MARKETER", {"analytics": True,
                                               "campaigns_edit": False,
                                               "legacy": True})
    assert set(perms) == set(PERMISSION_KEYS)          # только известные ключи
    assert perms["analytics"] is True                  # из строки
    assert perms["campaigns_edit"] is False            # из строки
    assert perms["dashboard"] is True                  # ключа нет → дефолт роли
    assert perms["users"] is False


def test_effective_permissions_grants_only_on_json_true():
    perms = effective_permissions("ANALYST", {"users": "true", "campaigns_edit": 1})
    assert perms["users"] is False and perms["campaigns_edit"] is False


def test_effective_permissions_unknown_role_has_nothing():
    assert effective_permissions("ROOT", None) == {k: False for k in PERMISSION_KEYS}


# ── permissions_for ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_admin_always_full_access_without_db():
    s = _session(ADMIN={k: False for k in PERMISSION_KEYS})
    perms = await permissions_for("ADMIN", s)
    assert perms == {k: True for k in PERMISSION_KEYS}
    assert s.gets == []                                # БД не трогается


@pytest.mark.asyncio
async def test_permissions_for_reads_role_row():
    s = _session(MARKETER={"campaigns_create": True, "campaigns_delete": False})
    perms = await permissions_for("MARKETER", s)
    assert perms["campaigns_create"] is True
    assert perms["campaigns_delete"] is False
    assert s.gets == [(RolePermission, "MARKETER")]    # один запрос по PK


@pytest.mark.asyncio
async def test_permissions_for_without_row_uses_defaults():
    assert await permissions_for("ANALYST", FakeSession()) == DEFAULT_PERMISSIONS["ANALYST"]


@pytest.mark.asyncio
async def test_permissions_for_has_no_process_cache():
    """B35: кэш на процесс (30 с) — другая реплика продолжала пускать по
    снятому праву. Права читаются из БД на каждом вызове."""
    s = _session(ANALYST={"analytics": True})
    assert (await permissions_for("ANALYST", s))["analytics"] is True
    # строку поменяла другая реплика (или другой процесс) — без PATCH здесь
    s.tables[RolePermission]["ANALYST"].permissions = {"analytics": False}
    assert (await permissions_for("ANALYST", s))["analytics"] is False
    assert len(s.gets) == 2
    assert not hasattr(rbac, "invalidate_cache")       # нечего инвалидировать


# ── require_permission ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_require_permission_admin_bypasses():
    dep = require_permission("campaigns_delete")
    s = _session(ADMIN={"campaigns_delete": False})
    user = await dep(user=_user("ADMIN"), session=s)
    assert user.role == "ADMIN" and s.gets == []


@pytest.mark.asyncio
async def test_require_permission_allows_when_granted():
    dep = require_permission("campaigns_create")
    user = await dep(user=_user("MARKETER"),
                     session=_session(MARKETER={"campaigns_create": True}))
    assert user.role == "MARKETER"


@pytest.mark.asyncio
async def test_require_permission_403_when_revoked():
    dep = require_permission("campaigns_create")
    with pytest.raises(HTTPException) as exc:
        await dep(user=_user("MARKETER"),
                  session=_session(MARKETER={"campaigns_create": False}))
    assert exc.value.status_code == 403
    assert exc.value.detail == "role MARKETER lacks permission 'campaigns_create'"


def test_require_permission_rejects_unknown_key():
    # опечатка в ключе давала бы вечный 403 — падает при импорте роутера
    with pytest.raises(ValueError, match="analitycs"):
        require_permission("analitycs")


# ── forbid_admin_escalation ───────────────────────────────────────────────────
@pytest.mark.parametrize("roles", [("ADMIN",), ("MARKETER", "ADMIN"), ("ADMIN", None)])
def test_non_admin_cannot_touch_admin_role(roles):
    with pytest.raises(HTTPException) as exc:
        forbid_admin_escalation(_user("MARKETER"), *roles)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("roles", [("ANALYST",), ("MARKETER", None), ()])
def test_non_admin_may_manage_non_admin_roles(roles):
    forbid_admin_escalation(_user("MARKETER"), *roles)


def test_admin_may_grant_admin():
    forbid_admin_escalation(_user("ADMIN"), "ANALYST", "ADMIN")
