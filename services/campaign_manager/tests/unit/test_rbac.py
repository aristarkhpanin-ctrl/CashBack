"""Unit tests for dynamic RBAC (phase 26)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from app import rbac
from app.api.roles import update_permissions
from app.rbac import invalidate_cache, permissions_for, require_permission
from app.schemas import RolePermissionsUpdate
from app.security import AuthUser
from fastapi import HTTPException


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """execute → строки role_permissions; get/commit — для PATCH."""
    def __init__(self, rows=None, existing=None):
        self._rows = rows or []
        self._existing = existing
        self.committed = False
        self.added = []

    async def execute(self, *_a, **_k):
        return _ScalarsResult(self._rows)

    async def get(self, _model, _pk):
        return self._existing

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _row(role, perms):
    return SimpleNamespace(role=role, permissions=perms)


def _user(role):
    return AuthUser(user_id="u", email="a@b.ru", role=role)


# ── permissions_for ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_admin_always_full_access_without_db():
    invalidate_cache()
    perms = await permissions_for("ADMIN", _FakeSession(rows=[]))
    assert all(perms.values()) and set(perms) == set(rbac.PERMISSION_KEYS)


@pytest.mark.asyncio
async def test_permissions_for_reads_table():
    invalidate_cache()
    rows = [_row("MARKETER", {"campaigns_create": True, "campaigns_delete": False})]
    perms = await permissions_for("MARKETER", _FakeSession(rows=rows))
    assert perms["campaigns_create"] is True
    assert perms["campaigns_delete"] is False


# ── require_permission ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_require_permission_admin_bypasses():
    dep = require_permission("campaigns_delete")
    # admin — короткое замыкание, БД не трогается
    user = await dep(user=_user("ADMIN"), session=_FakeSession(rows=[]))
    assert user.role == "ADMIN"


@pytest.mark.asyncio
async def test_require_permission_allows_when_granted():
    invalidate_cache()
    rows = [_row("MARKETER", {"campaigns_create": True})]
    dep = require_permission("campaigns_create")
    user = await dep(user=_user("MARKETER"), session=_FakeSession(rows=rows))
    assert user.role == "MARKETER"


@pytest.mark.asyncio
async def test_require_permission_403_when_revoked():
    invalidate_cache()
    rows = [_row("MARKETER", {"campaigns_create": False})]
    dep = require_permission("campaigns_create")
    with pytest.raises(HTTPException) as exc:
        await dep(user=_user("MARKETER"), session=_FakeSession(rows=rows))
    assert exc.value.status_code == 403


# ── cache invalidation ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cache_invalidation_reflects_new_table():
    invalidate_cache()
    s1 = _FakeSession(rows=[_row("ANALYST", {"analytics": True})])
    assert (await permissions_for("ANALYST", s1))["analytics"] is True
    # без инвалидации — старый кэш, даже если таблица «изменилась»
    s2 = _FakeSession(rows=[_row("ANALYST", {"analytics": False})])
    assert (await permissions_for("ANALYST", s2))["analytics"] is True
    invalidate_cache()
    assert (await permissions_for("ANALYST", s2))["analytics"] is False


# ── PATCH /roles/{role}/permissions ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_patch_admin_role_forbidden():
    with pytest.raises(HTTPException) as exc:
        await update_permissions(
            payload=RolePermissionsUpdate(permissions={"users": False}),
            role="ADMIN", current=_user("ADMIN"), session=_FakeSession(),
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_patch_rejects_unknown_keys():
    with pytest.raises(HTTPException) as exc:
        await update_permissions(
            payload=RolePermissionsUpdate(permissions={"bogus": True}),
            role="MARKETER", current=_user("ADMIN"), session=_FakeSession(),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_patch_merges_and_commits():
    invalidate_cache()
    existing = _row("MARKETER", {"campaigns_create": True, "analytics": False})
    session = _FakeSession(existing=existing)
    result = await update_permissions(
        payload=RolePermissionsUpdate(permissions={"analytics": True}),
        role="MARKETER", current=_user("ADMIN"), session=session,
    )
    assert result["analytics"] is True          # обновлено
    assert result["campaigns_create"] is True   # сохранено из существующего
    assert session.committed
