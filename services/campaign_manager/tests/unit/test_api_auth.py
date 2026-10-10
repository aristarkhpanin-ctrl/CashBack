"""HTTP-тесты /auth/* (фаза 15) через настоящее приложение (см. conftest)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models import AdminUser, RolePermission
from app.rbac import PERMISSION_KEYS
from app.security import decode_token, hash_password, issue_token_pair, verify_password

ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_T0 = datetime(2026, 1, 1, tzinfo=UTC)

# bcrypt с rounds=12 дорогой — хэшируем пароли один раз на модуль.
_HASH_ADMIN = hash_password("admin-pass")
_HASH_OTHER = hash_password("other-pass")


def _user(email="admin@bank.ru", *, user_id=ADMIN_ID, role="ADMIN",
          password_hash=_HASH_ADMIN, is_active=True, created_at=_T0):
    return AdminUser(
        user_id=user_id, email=email, password_hash=password_hash,
        full_name=email.split("@")[0], role=role, is_active=is_active,
        created_at=created_at, last_login_at=None,
    )


def _refresh_token(user: AdminUser) -> str:
    return issue_token_pair(
        user_id=str(user.user_id), email=user.email, role=user.role,
    )["refresh_token"]


# ── POST /auth/login ──────────────────────────────────────────────────────────
def test_login_success_returns_token_pair_and_stamps_last_login(client, db):
    user = _user()
    db.put(user)
    r = client.post("/auth/login",
                    json={"email": "admin@bank.ru", "password": "admin-pass"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    access = decode_token(body["access_token"], expected_type="access")
    refresh = decode_token(body["refresh_token"], expected_type="refresh")
    assert access["sub"] == refresh["sub"] == str(ADMIN_ID)
    assert access["role"] == "ADMIN" and access["email"] == "admin@bank.ru"
    assert user.last_login_at is not None
    assert db.commits == 1


def test_login_email_is_case_insensitive(client, db):
    db.put(_user())
    r = client.post("/auth/login",
                    json={"email": "Admin@Bank.RU", "password": "admin-pass"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("email,password,is_active", [
    ("admin@bank.ru", "wrong-pass", True),     # неверный пароль
    ("ghost@bank.ru", "admin-pass", True),     # нет такого пользователя
    ("admin@bank.ru", "admin-pass", False),    # деактивирован
])
def test_login_failures_are_uniform_401(client, db, email, password, is_active):
    db.put(_user(is_active=is_active))
    r = client.post("/auth/login", json={"email": email, "password": password})
    # одинаковый ответ — не раскрываем, существует ли email
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}
    assert db.commits == 0


def test_login_rejects_malformed_body(client):
    r = client.post("/auth/login", json={"email": "admin@bank.ru", "password": ""})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "password"]


# ── POST /auth/refresh ────────────────────────────────────────────────────────
def test_refresh_issues_new_pair_for_active_user(client, db):
    user = _user(role="MARKETER", email="m@bank.ru")
    db.put(user)
    r = client.post("/auth/refresh", json={"refresh_token": _refresh_token(user)})
    assert r.status_code == 200, r.text
    claims = decode_token(r.json()["access_token"], expected_type="access")
    assert claims["sub"] == str(user.user_id) and claims["role"] == "MARKETER"


def test_refresh_rejects_disabled_or_deleted_user(client, db):
    disabled = _user(is_active=False)
    db.put(disabled)
    r = client.post("/auth/refresh", json={"refresh_token": _refresh_token(disabled)})
    assert r.status_code == 401
    assert r.json()["detail"] == "user disabled or deleted"

    ghost = _user(user_id=uuid.uuid4())                  # в БД его нет
    r = client.post("/auth/refresh", json={"refresh_token": _refresh_token(ghost)})
    assert r.status_code == 401


def test_refresh_rejects_access_token_and_garbage(client, db, auth):
    db.put(_user())
    access = auth("ADMIN")["Authorization"].removeprefix("Bearer ")
    r = client.post("/auth/refresh", json={"refresh_token": access})
    assert r.status_code == 401
    assert r.json()["detail"] == "expected refresh token"

    r = client.post("/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid or expired token"


# ── GET /auth/me ──────────────────────────────────────────────────────────────
def test_me_requires_token(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_me_rejects_refresh_token_as_access(client, db):
    user = _user()
    db.put(user)
    r = client.get("/auth/me",
                   headers={"Authorization": f"Bearer {_refresh_token(user)}"})
    assert r.status_code == 401


def test_me_admin_gets_profile_with_full_permissions(client, db, auth):
    db.put(_user())
    r = client.get("/auth/me", headers=auth("ADMIN", user_id=str(ADMIN_ID)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "admin@bank.ru" and body["role"] == "ADMIN"
    assert "password_hash" not in body
    assert body["permissions"] == {k: True for k in PERMISSION_KEYS}


def test_me_marketer_gets_permissions_from_role_table(client, db, auth):
    uid = uuid.uuid4()
    db.put(_user("m@bank.ru", user_id=uid, role="MARKETER"))
    db.put(RolePermission(role="MARKETER",
                          permissions={**{k: True for k in PERMISSION_KEYS},
                                       "users": False, "analytics": False}))
    r = client.get("/auth/me", headers=auth("MARKETER", user_id=str(uid)))
    assert r.status_code == 200, r.text
    perms = r.json()["permissions"]
    assert perms["campaigns_create"] is True
    assert perms["analytics"] is False and perms["users"] is False


def test_me_for_deleted_user_is_401(client, auth):
    r = client.get("/auth/me", headers=auth("ADMIN", user_id=str(uuid.uuid4())))
    assert r.status_code == 401
    assert r.json()["detail"] == "user deleted"


# ── GET /auth/users ───────────────────────────────────────────────────────────
def test_list_users_admin_only(client, db, auth):
    db.put(_user(), _user("a@bank.ru", user_id=uuid.uuid4(), role="ANALYST",
                          created_at=_T0 + timedelta(days=1)))
    r = client.get("/auth/users", headers=auth("ADMIN"))
    assert r.status_code == 200, r.text
    assert [u["email"] for u in r.json()] == ["admin@bank.ru", "a@bank.ru"]

    assert client.get("/auth/users").status_code == 401
    for role in ("MARKETER", "ANALYST"):
        assert client.get("/auth/users", headers=auth(role)).status_code == 403


# ── POST /auth/users ──────────────────────────────────────────────────────────
def test_create_user_hashes_password_and_normalises_email(client, db, auth):
    r = client.post("/auth/users", headers=auth("ADMIN"), json={
        "email": "New@Bank.ru", "password": "s3cret!", "full_name": "Новый",
        "role": "MARKETER",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "new@bank.ru" and body["role"] == "MARKETER"
    assert body["is_active"] is True
    (stored,) = db.rows(AdminUser)
    assert stored.email == "new@bank.ru"
    assert stored.password_hash != "s3cret!"
    assert verify_password("s3cret!", stored.password_hash)
    assert db.commits == 1


def test_create_user_duplicate_email_is_409(client, db, auth):
    db.put(_user())
    r = client.post("/auth/users", headers=auth("ADMIN"), json={
        "email": "ADMIN@bank.ru", "password": "s3cret!", "full_name": "Дубль",
    })
    assert r.status_code == 409
    assert len(db.rows(AdminUser)) == 1 and db.commits == 0


def test_create_user_validates_role_and_requires_admin(client, db, auth):
    payload = {"email": "x@bank.ru", "password": "s3cret!", "full_name": "X"}
    r = client.post("/auth/users", headers=auth("ADMIN"),
                    json={**payload, "role": "ROOT"})
    assert r.status_code == 422
    r = client.post("/auth/users", headers=auth("MARKETER"), json=payload)
    assert r.status_code == 403
    assert db.rows(AdminUser) == []


# ── PATCH /auth/users/{id} ────────────────────────────────────────────────────
def test_update_user_changes_role_status_and_password(client, db, auth):
    other_id = uuid.uuid4()
    other = _user("m@bank.ru", user_id=other_id, role="MARKETER",
                  password_hash=_HASH_OTHER)
    db.put(_user(), other)
    r = client.patch(f"/auth/users/{other_id}", headers=auth("ADMIN"), json={
        "role": "ANALYST", "is_active": False, "password": "brand-new",
    })
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "ANALYST" and r.json()["is_active"] is False
    assert verify_password("brand-new", other.password_hash)
    assert db.commits == 1


def test_update_missing_user_is_404(client, auth):
    r = client.patch(f"/auth/users/{uuid.uuid4()}", headers=auth("ADMIN"),
                     json={"full_name": "X"})
    assert r.status_code == 404


@pytest.mark.parametrize("patch", [{"role": "MARKETER"}, {"is_active": False}])
def test_admin_cannot_demote_or_deactivate_self(client, db, auth, patch):
    me = _user()
    db.put(me)
    r = client.patch(f"/auth/users/{ADMIN_ID}",
                     headers=auth("ADMIN", user_id=str(ADMIN_ID)), json=patch)
    assert r.status_code == 409
    assert me.role == "ADMIN" and me.is_active is True and db.commits == 0


def test_admin_can_edit_own_name(client, db, auth):
    me = _user()
    db.put(me)
    r = client.patch(f"/auth/users/{ADMIN_ID}",
                     headers=auth("ADMIN", user_id=str(ADMIN_ID)),
                     json={"full_name": "Главный", "role": "ADMIN"})
    assert r.status_code == 200, r.text
    assert me.full_name == "Главный"


def test_update_user_requires_admin(client, db, auth):
    db.put(_user())
    r = client.patch(f"/auth/users/{ADMIN_ID}", headers=auth("MARKETER"),
                     json={"full_name": "hack"})
    assert r.status_code == 403
