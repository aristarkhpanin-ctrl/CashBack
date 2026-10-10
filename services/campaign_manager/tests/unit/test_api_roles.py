"""HTTP-тесты матрицы прав /roles (фаза 26) через настоящее приложение."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.models import AdminUser, RolePermission
from app.rbac import DEFAULT_PERMISSIONS, PERMISSION_KEYS


# ── GET /roles/permissions ────────────────────────────────────────────────────
def test_get_permissions_defaults_when_table_empty(client, auth):
    r = client.get("/roles/permissions", headers=auth("ADMIN"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"ADMIN", "MARKETER", "ANALYST"}
    for role, perms in body.items():
        assert set(perms) == set(PERMISSION_KEYS)
        assert perms == {k: DEFAULT_PERMISSIONS[role].get(k, False)
                         for k in PERMISSION_KEYS}


def test_get_permissions_overlays_table_on_defaults(client, db, auth):
    # частичная строка (как после старой миграции) + мусорный ключ
    db.put(RolePermission(role="ANALYST",
                          permissions={"campaigns_create": True, "legacy": True}))
    body = client.get("/roles/permissions", headers=auth("ADMIN")).json()
    assert body["ANALYST"]["campaigns_create"] is True        # из таблицы
    assert body["ANALYST"]["analytics"] is True               # дефолт
    assert "legacy" not in body["ANALYST"]                    # только известные ключи
    assert body["MARKETER"] == {k: DEFAULT_PERMISSIONS["MARKETER"][k]
                                for k in PERMISSION_KEYS}


@pytest.mark.parametrize("role", ["MARKETER", "ANALYST"])
def test_roles_api_is_admin_only(client, auth, role):
    assert client.get("/roles/permissions", headers=auth(role)).status_code == 403
    r = client.patch("/roles/ANALYST/permissions", headers=auth(role),
                     json={"permissions": {"analytics": False}})
    assert r.status_code == 403


def test_roles_api_requires_token(client):
    assert client.get("/roles/permissions").status_code == 401


# ── PATCH /roles/{role}/permissions ──────────────────────────────────────────
def test_patch_creates_row_from_defaults(client, db, auth):
    r = client.patch("/roles/ANALYST/permissions",
                     headers=auth("ADMIN", email="boss@bank.ru"),
                     json={"permissions": {"campaigns_edit": True}})
    assert r.status_code == 200, r.text
    expected = {**{k: DEFAULT_PERMISSIONS["ANALYST"][k] for k in PERMISSION_KEYS},
                "campaigns_edit": True}
    assert r.json() == expected
    row = db.tables[RolePermission]["ANALYST"]
    assert row.permissions == expected and row.updated_by == "boss@bank.ru"
    assert db.commits == 1


def test_patch_admin_role_is_forbidden(client, db, auth):
    r = client.patch("/roles/ADMIN/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"users": False}})
    assert r.status_code == 403
    assert db.rows(RolePermission) == []


def test_patch_rejects_unknown_role_and_unknown_keys(client, db, auth):
    r = client.patch("/roles/ROOT/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"users": True}})
    assert r.status_code == 422                      # pattern пути
    r = client.patch("/roles/MARKETER/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"userz": True}})
    assert r.status_code == 422
    assert "userz" in r.json()["detail"]
    r = client.patch("/roles/MARKETER/permissions", headers=auth("ADMIN"),
                     json={"permissions": {}})
    assert r.status_code == 422                      # пустой патч
    assert db.commits == 0


def test_patch_takes_effect_immediately_for_role_users(client, db, auth):
    """PATCH инвалидирует кэш rbac: /auth/me маркетолога сразу видит новое право."""
    uid = uuid.uuid4()
    db.put(AdminUser(user_id=uid, email="m@bank.ru", password_hash="x",
                     full_name="M", role="MARKETER", is_active=True,
                     created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    marketer = auth("MARKETER", user_id=str(uid), email="m@bank.ru")
    before = client.get("/auth/me", headers=marketer).json()["permissions"]
    assert before["analytics"] is False                       # кэш прогрет

    r = client.patch("/roles/MARKETER/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"analytics": True}})
    assert r.status_code == 200, r.text

    after = client.get("/auth/me", headers=marketer).json()["permissions"]
    assert after["analytics"] is True
