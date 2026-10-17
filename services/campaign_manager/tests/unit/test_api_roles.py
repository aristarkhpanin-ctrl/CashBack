"""HTTP-тесты матрицы прав /roles (фаза 26, этап B — B35) через настоящее приложение.

PATCH — один атомарный upsert со слиянием JSONB в Postgres
(``roles._MERGE_PERMISSIONS``). Фейковая БД ниже воспроизводит его
семантику (``permissions || patch``) словарём; что сам оператор атомарен и
не теряет параллельные переключения, проверено на живом Postgres. Здесь —
что обработчик шлёт ровно этот оператор и не делает read-modify-write.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.api import roles
from app.main import create_app
from app.models import AdminUser, MlLimit, RolePermission
from app.rbac import DEFAULT_PERMISSIONS, PERMISSION_KEYS
from fastapi.testclient import TestClient

from tests.unit.fakes import FakeSession, fake_sessionmaker


class _Returning:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class RolesDB(FakeSession):
    """FakeSession + upsert матрицы прав; ``merges`` — параметры каждого вызова."""

    def __init__(self) -> None:
        super().__init__()
        self.merges: list[dict] = []

    async def execute(self, stmt, params=None, *a, **k):
        if stmt is not roles._MERGE_PERMISSIONS:
            return await super().execute(stmt, params, *a, **k)
        self.merges.append(params)
        row = self.tables.get(RolePermission, {}).get(params["role"])
        if row is None:                                   # INSERT
            row = RolePermission(role=params["role"], permissions={})
            self.put(row)
        row.permissions = {**row.permissions, **params["patch"]}   # rp || EXCLUDED
        row.updated_at, row.updated_by = params["updated_at"], params["updated_by"]
        return _Returning(dict(row.permissions))


@pytest.fixture
def db() -> RolesDB:
    return RolesDB()


def _grant(db, role, perms):
    db.put(RolePermission(role=role, permissions=perms))


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
    _grant(db, "ANALYST", {"campaigns_create": True, "legacy": True})
    body = client.get("/roles/permissions", headers=auth("ADMIN")).json()
    assert body["ANALYST"]["campaigns_create"] is True        # из таблицы
    assert body["ANALYST"]["analytics"] is True               # дефолт
    assert "legacy" not in body["ANALYST"]                    # только известные ключи
    assert body["MARKETER"] == {k: DEFAULT_PERMISSIONS["MARKETER"][k]
                                for k in PERMISSION_KEYS}


def test_get_permissions_needs_users_permission(client, db, auth):
    """§4: чтение матрицы — право users (делегируемо), а не роль ADMIN."""
    headers = auth("MARKETER")
    r = client.get("/roles/permissions", headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "role MARKETER lacks permission 'users'"
    _grant(db, "MARKETER", {"users": True})
    assert client.get("/roles/permissions", headers=headers).status_code == 200


@pytest.mark.parametrize("role", ["MARKETER", "ANALYST"])
def test_patch_is_admin_role_only_even_with_all_permissions(client, db, auth, role):
    # держатель users не может выдать себе права (самоэскалация)
    _grant(db, role, {k: True for k in PERMISSION_KEYS})
    r = client.patch("/roles/ANALYST/permissions", headers=auth(role),
                     json={"permissions": {"analytics": False}})
    assert r.status_code == 403
    assert db.merges == []


def test_roles_api_requires_token(client):
    assert client.get("/roles/permissions").status_code == 401
    r = client.patch("/roles/ANALYST/permissions",
                     json={"permissions": {"analytics": False}})
    assert r.status_code == 401


# ── PATCH /roles/{role}/permissions ──────────────────────────────────────────
def test_patch_merges_in_one_statement_without_reading_the_row(client, db, auth):
    _grant(db, "MARKETER", {"campaigns_create": True, "analytics": False,
                            "legacy": True})
    r = client.patch("/roles/MARKETER/permissions",
                     headers=auth("ADMIN", email="boss@bank.ru"),
                     json={"permissions": {"analytics": True}})
    assert r.status_code == 200, r.text
    assert r.json() == {**{k: DEFAULT_PERMISSIONS["MARKETER"][k] for k in PERMISSION_KEYS},
                        "campaigns_create": True, "analytics": True}
    # один upsert ровно с присланными ключами, строку обработчик не читал
    (merge,) = db.merges
    assert merge["role"] == "MARKETER" and merge["patch"] == {"analytics": True}
    assert merge["updated_by"] == "boss@bank.ru"
    assert (RolePermission, "MARKETER") not in db.gets
    assert db.commits == 1
    # слияние в БД сохранило соседние ключи (в т.ч. неизвестные)
    assert db.tables[RolePermission]["MARKETER"].permissions == {
        "campaigns_create": True, "analytics": True, "legacy": True}


def test_patch_creates_missing_row_with_the_patch(client, db, auth):
    r = client.patch("/roles/ANALYST/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"campaigns_edit": True}})
    assert r.status_code == 200, r.text
    assert r.json() == {**{k: DEFAULT_PERMISSIONS["ANALYST"][k] for k in PERMISSION_KEYS},
                        "campaigns_edit": True}
    row = db.tables[RolePermission]["ANALYST"]
    assert row.permissions == {"campaigns_edit": True}  # остальное — дефолты при чтении


def test_merge_statement_is_an_atomic_jsonb_upsert():
    sql = " ".join(str(roles._MERGE_PERMISSIONS).split())
    assert sql.startswith("INSERT INTO role_permissions AS rp")
    assert "ON CONFLICT (role) DO UPDATE SET permissions = rp.permissions || EXCLUDED.permissions" in sql
    assert sql.endswith("RETURNING permissions")


def test_patch_admin_role_is_forbidden(client, db, auth):
    r = client.patch("/roles/ADMIN/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"users": False}})
    assert r.status_code == 403
    assert db.merges == [] and db.rows(RolePermission) == []


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
    assert db.merges == [] and db.commits == 0


def test_patch_takes_effect_immediately_for_role_users(client, db, auth):
    """/auth/me маркетолога сразу видит новое право."""
    uid = uuid.uuid4()
    db.put(AdminUser(user_id=uid, email="m@bank.ru", password_hash="x",
                     full_name="M", role="MARKETER", is_active=True,
                     created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    marketer = auth("MARKETER", user_id=str(uid), email="m@bank.ru")
    before = client.get("/auth/me", headers=marketer).json()["permissions"]
    assert before["analytics"] is False

    r = client.patch("/roles/MARKETER/permissions", headers=auth("ADMIN"),
                     json={"permissions": {"analytics": True}})
    assert r.status_code == 200, r.text

    after = client.get("/auth/me", headers=marketer).json()["permissions"]
    assert after["analytics"] is True


def test_change_through_one_replica_is_enforced_by_another_at_once(db, redis, auth):
    """Две реплики над одной БД (B35): у каждой был свой кэш прав на 30 с,
    и снятое через одну реплику право продолжало работать на другой."""
    replicas = []
    for _ in range(2):
        app = create_app()
        app.state.sessionmaker = fake_sessionmaker(db)
        app.state.redis = redis
        replicas.append(TestClient(app))
    one, two = replicas
    db.put(MlLimit(segment_bucket="mass", min_rate=1, max_rate=5,
                   daily_budget=1000, auto_approve=False, risk_level="low",
                   enabled=True, updated_by="a", updated_at=datetime.now(UTC)))
    _grant(db, "MARKETER", dict(DEFAULT_PERMISSIONS["MARKETER"]))
    marketer, admin = auth("MARKETER"), auth("ADMIN")

    assert two.get("/ml-limits", headers=marketer).status_code == 403
    r = one.patch("/roles/MARKETER/permissions", headers=admin,
                  json={"permissions": {"analytics": True}})
    assert r.status_code == 200
    assert two.get("/ml-limits", headers=marketer).status_code == 200

    # право сняла третья реплика (строка изменена в БД в обход этого процесса)
    db.tables[RolePermission]["MARKETER"].permissions["analytics"] = False
    assert two.get("/ml-limits", headers=marketer).status_code == 403
    assert one.get("/ml-limits", headers=marketer).status_code == 403
