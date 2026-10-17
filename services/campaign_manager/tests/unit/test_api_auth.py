"""HTTP-тесты /auth/* (фаза 15, этап B) через настоящее приложение (см. conftest)."""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.models import AdminUser, RolePermission
from app.rbac import PERMISSION_KEYS
from app.security import decode_token, hash_password, make_access_token, verify_password

ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_T0 = datetime(2026, 1, 1, tzinfo=UTC)

# bcrypt с rounds=12 дорогой — хэшируем пароли один раз на модуль.
_HASH_ADMIN = hash_password("admin-pass")
_HASH_OTHER = hash_password("other-pass")


def _user(email="admin@bank.ru", *, user_id=ADMIN_ID, role="ADMIN",
          password_hash=_HASH_ADMIN, is_active=True, created_at=_T0,
          must_change_password=False, token_version=0):
    return AdminUser(
        user_id=user_id, email=email, password_hash=password_hash,
        full_name=email.split("@")[0], role=role, is_active=is_active,
        created_at=created_at, last_login_at=None,
        must_change_password=must_change_password, token_version=token_version,
    )


def _login(client, email="admin@bank.ru", password="admin-pass"):
    return client.post("/auth/login", json={"email": email, "password": password})


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── POST /auth/login ──────────────────────────────────────────────────────────
def test_login_success_returns_token_pair_and_stamps_last_login(client, db, redis):
    user = _user()
    db.put(user)
    r = _login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["must_change_password"] is False
    access = decode_token(body["access_token"], expected_type="access")
    refresh = decode_token(body["refresh_token"], expected_type="refresh")
    assert access["sub"] == refresh["sub"] == str(ADMIN_ID)
    assert access["role"] == "ADMIN" and access["email"] == "admin@bank.ru"
    assert access["ver"] == refresh["ver"] == 0 and access["pwd_change"] is False
    assert access["iss"] == "cashback-campaign-manager"
    assert access["aud"] == "cashback-admin"
    # refresh-jti записан в Redis на срок жизни токена — без записи он одноразово
    # не обменивается (ротация).
    ttl, record = redis.data[f"auth:refresh:{refresh['jti']}"]
    assert json.loads(record) == {"uid": str(ADMIN_ID), "fam": refresh["fam"]}
    assert ttl == 7 * 24 * 3600
    assert user.last_login_at is not None
    assert db.commits == 1


def test_login_email_is_case_insensitive(client, db):
    db.put(_user())
    r = _login(client, email=" Admin@Bank.RU ")
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("email,password,is_active", [
    ("admin@bank.ru", "wrong-pass", True),     # неверный пароль
    ("ghost@bank.ru", "admin-pass", True),     # нет такого пользователя
    ("admin@bank.ru", "admin-pass", False),    # деактивирован
])
def test_login_failures_are_uniform_401(client, db, email, password, is_active):
    db.put(_user(is_active=is_active))
    r = _login(client, email=email, password=password)
    # одинаковый ответ — не раскрываем, существует ли email
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid email or password"}
    assert db.commits == 0


def test_login_rejects_malformed_body(client):
    r = client.post("/auth/login", json={"email": "admin@bank.ru", "password": ""})
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "password"]


def test_login_with_must_change_password_returns_pwd_change_token(client, db):
    db.put(_user(must_change_password=True))
    r = _login(client)
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is True
    claims = decode_token(r.json()["access_token"], expected_type="access")
    assert claims["pwd_change"] is True


# ── троттлинг (B20) ───────────────────────────────────────────────────────────
def test_login_brute_force_is_throttled_per_email_and_ip(client, db, redis):
    db.put(_user())
    for _ in range(5):
        assert _login(client, password="wrong-pass").status_code == 401
    r = _login(client, password="wrong-pass")
    assert r.status_code == 429
    assert r.json() == {"detail": "too many login attempts"}
    assert 0 < int(r.headers["retry-after"]) <= 900
    # верный пароль внутри окна тоже 429: иначе троттлинг — оракул пароля
    assert _login(client).status_code == 429
    # другой email с того же IP — свой счётчик
    db.put(_user("m@bank.ru", user_id=uuid.uuid4(), password_hash=_HASH_OTHER))
    assert _login(client, "m@bank.ru", "other-pass").status_code == 200


def test_successful_login_resets_email_ip_counter(client, db):
    db.put(_user())
    for _ in range(4):
        assert _login(client, password="wrong-pass").status_code == 401
    assert _login(client).status_code == 200
    for _ in range(5):
        assert _login(client, password="wrong-pass").status_code == 401
    assert _login(client, password="wrong-pass").status_code == 429


def test_login_throttled_per_ip_across_emails(client, db, redis):
    for i in range(30):
        r = _login(client, email=f"user{i}@bank.ru", password="x")
        assert r.status_code == 401, (i, r.text)
    r = _login(client, email="another@bank.ru", password="x")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) == redis.data["auth:login_ip:testclient"][0]


def test_retry_after_is_the_remaining_window(client, db, redis):
    db.put(_user())
    for _ in range(5):
        _login(client, password="wrong-pass")
    (key,) = (k for k in redis.data if k.startswith("auth:login_fail:"))
    assert "admin@bank.ru" not in key              # email в ключе — хэш
    redis.data[key] = (42, redis.data[key][1])     # окно частично истекло
    r = _login(client, password="wrong-pass")
    assert r.status_code == 429 and r.headers["retry-after"] == "42"


# За прокси (nginx фронта, ingress) uvicorn --proxy-headers ставит клиентом
# адрес из X-Forwarded-For — только если соединение пришло от адреса из
# FORWARDED_ALLOW_IPS. Здесь — тот же middleware uvicorn перед приложением.
PROXY_IP = "172.28.0.10"


def _behind_proxy(app, peer: str):
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    return TestClient(ProxyHeadersMiddleware(app, trusted_hosts=PROXY_IP),
                      client=(peer, 50000))


def test_throttle_counts_the_forwarded_client_behind_a_trusted_proxy(app, db, redis):
    db.put(_user())
    proxy = _behind_proxy(app, PROXY_IP)
    attacker = {"X-Forwarded-For": "198.51.100.7"}
    for _ in range(5):
        r = proxy.post("/auth/login", headers=attacker,
                       json={"email": "admin@bank.ru", "password": "wrong-pass"})
        assert r.status_code == 401
    r = proxy.post("/auth/login", headers=attacker,
                   json={"email": "admin@bank.ru", "password": "admin-pass"})
    assert r.status_code == 429
    # владелец за тем же прокси не заблокирован чужим перебором
    r = proxy.post("/auth/login", headers={"X-Forwarded-For": "203.0.113.20"},
                   json={"email": "admin@bank.ru", "password": "admin-pass"})
    assert r.status_code == 200, r.text
    assert "auth:login_ip:198.51.100.7" in redis.data
    assert not any(k.endswith(PROXY_IP) for k in redis.data)


def test_untrusted_peer_cannot_dodge_the_throttle_with_x_forwarded_for(app, db, redis):
    db.put(_user())
    direct = _behind_proxy(app, "203.0.113.9")          # мимо прокси, на :8002
    for i in range(5):
        r = direct.post("/auth/login", headers={"X-Forwarded-For": f"10.0.0.{i}"},
                        json={"email": "admin@bank.ru", "password": "wrong-pass"})
        assert r.status_code == 401
    r = direct.post("/auth/login", headers={"X-Forwarded-For": "10.0.0.99"},
                    json={"email": "admin@bank.ru", "password": "wrong-pass"})
    assert r.status_code == 429
    assert set(k.rsplit(":", 1)[1] for k in redis.data) == {"203.0.113.9"}


def test_throttle_works_on_redis_6(client, db, redis):
    """Redis 6.2 (внешний в helm) не знает ``EXPIRE … NX``: каждая попытка
    падала в fail-open, и перебор шёл без ограничения, молча."""
    redis.version = (6, 2)
    db.put(_user())
    for _ in range(5):
        assert _login(client, password="wrong-pass").status_code == 401
    assert _login(client).status_code == 429
    assert {ttl for ttl, _ in redis.data.values()} == {900}


def test_throttle_window_is_set_once_and_repaired_if_lost(client, db, redis):
    db.put(_user())
    _login(client, password="wrong-pass")
    pair_key, ip_key = sorted(redis.data)
    redis.data[pair_key] = (42, redis.data[pair_key][1])   # окно идёт
    redis.data[ip_key] = (None, redis.data[ip_key][1])     # упал между INCR и EXPIRE
    _login(client, password="wrong-pass")
    assert redis.data[pair_key] == (42, "2")               # не продлевается
    assert redis.data[ip_key] == (900, "2")                # TTL восстановлен


def test_login_fails_open_when_redis_is_down(client, db, redis):
    """Redis недоступен → вход работает без троттлинга (доступность админки),
    refresh-токен не записан и потому не обменивается (fail-closed)."""
    db.put(_user())
    redis.fail = True
    for _ in range(7):
        assert _login(client, password="wrong-pass").status_code == 401
    r = _login(client)
    assert r.status_code == 200, r.text
    redis.fail = False
    r = client.post("/auth/refresh", json={"refresh_token": r.json()["refresh_token"]})
    assert r.status_code == 401


def test_login_without_redis_client_is_401_not_500(app, client, db):
    del app.state.redis
    r = _login(client, password="wrong-pass")
    assert r.status_code == 401


# ── bcrypt: ровно один раз, в пуле потоков (B35) ──────────────────────────────
@pytest.mark.parametrize("email,password,is_active", [
    ("admin@bank.ru", "admin-pass", True),
    ("admin@bank.ru", "wrong-pass", True),
    ("ghost@bank.ru", "admin-pass", True),
    ("admin@bank.ru", "admin-pass", False),
])
def test_login_runs_bcrypt_exactly_once_off_the_event_loop(
        client, db, monkeypatch, email, password, is_active):
    import threading

    import bcrypt
    calls: list[tuple[bytes, str]] = []
    hashes: list[bytes] = []
    real_check, real_hash = bcrypt.checkpw, bcrypt.hashpw

    def _spy(pw, hashed):
        calls.append((hashed, threading.current_thread().name))
        return real_check(pw, hashed)

    def _spy_hash(pw, salt):
        hashes.append(pw)
        return real_hash(pw, salt)

    monkeypatch.setattr(bcrypt, "checkpw", _spy)
    monkeypatch.setattr(bcrypt, "hashpw", _spy_hash)
    db.put(_user(is_active=is_active))
    loop_thread = []

    @client.app.middleware("http")
    async def _remember_loop_thread(request, call_next):
        loop_thread.append(threading.current_thread().name)
        return await call_next(request)

    _login(client, email=email, password=password)
    assert len(calls) == 1 and hashes == []       # ни второго хэша, ни соли
    hashed, thread = calls[0]
    assert thread != loop_thread[0]
    if email == "ghost@bank.ru":
        from app.security import DUMMY_PASSWORD_HASH
        assert hashed == DUMMY_PASSWORD_HASH.encode()


# ── POST /auth/refresh (ротация) ──────────────────────────────────────────────
def test_refresh_rotates_and_keeps_family(client, db, redis):
    user = _user(role="MARKETER", email="m@bank.ru")
    db.put(user)
    first = _login(client, "m@bank.ru").json()
    r = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200, r.text
    second = r.json()
    old = decode_token(first["refresh_token"], expected_type="refresh")
    new = decode_token(second["refresh_token"], expected_type="refresh")
    assert new["fam"] == old["fam"] and new["jti"] != old["jti"]
    assert f"auth:refresh:{old['jti']}" not in redis.data       # использован
    assert f"auth:refresh:{new['jti']}" in redis.data
    claims = decode_token(second["access_token"], expected_type="access")
    assert claims["sub"] == str(user.user_id) and claims["role"] == "MARKETER"


def test_refresh_reuse_revokes_whole_family(client, db, redis):
    db.put(_user())
    first = _login(client).json()
    second = client.post("/auth/refresh",
                         json={"refresh_token": first["refresh_token"]}).json()
    # злоумышленник предъявляет украденный (уже обменянный) токен
    r = client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401
    assert r.json()["detail"] == "refresh token reused or revoked"
    fam = decode_token(first["refresh_token"], expected_type="refresh")["fam"]
    assert redis.data[f"auth:refresh_fam_revoked:{fam}"][0] == 7 * 24 * 3600
    # …и «честный» токен того же семейства тоже больше не работает
    r = client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 401
    # другие сессии (новый логин — новое семейство) не затронуты
    third = _login(client).json()
    r = client.post("/auth/refresh", json={"refresh_token": third["refresh_token"]})
    assert r.status_code == 200


def test_refresh_rejects_disabled_deleted_or_stale_version(client, db):
    user = _user()
    db.put(user)
    pair = _login(client).json()
    user.is_active = False
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401 and r.json()["detail"] == "token revoked"

    user.is_active = True
    pair = _login(client).json()
    user.token_version += 1                      # смена роли/пароля в другом месте
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401

    pair = _login(client).json()
    db.tables[AdminUser].clear()                 # пользователь удалён
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401


def test_refresh_rejects_access_token_and_garbage(client, db, auth):
    access = auth("ADMIN")["Authorization"].removeprefix("Bearer ")
    r = client.post("/auth/refresh", json={"refresh_token": access})
    assert r.status_code == 401
    assert r.json()["detail"] == "expected refresh token"

    r = client.post("/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid or expired token"


def test_refresh_fails_closed_when_redis_is_down(client, db, redis):
    db.put(_user())
    pair = _login(client).json()
    redis.fail = True
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 503
    assert r.json() == {"detail": "session store unavailable"}


def test_refresh_store_failure_after_rotation_is_503(client, db, redis):
    """jti уже забран GETDEL, а новый не записался → 503, а не пара токенов,
    refresh которой заведомо не сработает."""
    db.put(_user())
    pair = _login(client).json()

    async def _set_fails(*_a, **_k):
        raise ConnectionError("redis went away")

    redis.set = _set_fails
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 503


@pytest.mark.parametrize("missing", ["jti", "fam"])
def test_refresh_and_logout_need_jti_and_family(client, db, redis, missing):
    from app.config import get_settings
    from jose import jwt

    db.put(_user())
    pair = _login(client).json()
    claims = decode_token(pair["refresh_token"], expected_type="refresh")
    del claims[missing]
    token = jwt.encode(claims, get_settings().jwt_secret.get_secret_value(),
                       algorithm="HS256")
    before = dict(redis.data)
    r = client.post("/auth/refresh", json={"refresh_token": token})
    assert r.status_code == 401
    r = client.post("/auth/logout", json={"refresh_token": token})
    assert r.status_code == 204
    if missing == "fam":
        assert redis.data == before               # отзывать нечего


# ── POST /auth/logout ─────────────────────────────────────────────────────────
def test_logout_revokes_family(client, db, redis):
    db.put(_user())
    first = _login(client).json()
    second = client.post("/auth/refresh",
                         json={"refresh_token": first["refresh_token"]}).json()
    r = client.post("/auth/logout", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 204 and r.content == b""
    r = client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 401
    # идемпотентно
    r = client.post("/auth/logout", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 204


@pytest.mark.parametrize("token", ["garbage", ""])
def test_logout_unknown_token_is_204(client, redis, token):
    r = client.post("/auth/logout", json={"refresh_token": token})
    assert r.status_code == 204
    assert redis.data == {}


def test_logout_reports_unavailable_store(client, db, redis):
    db.put(_user())
    pair = _login(client).json()
    redis.fail = True
    r = client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 503


# ── GET /auth/me ──────────────────────────────────────────────────────────────
def test_me_requires_token(client):
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_me_rejects_refresh_token_as_access(client, db):
    db.put(_user())
    pair = _login(client).json()
    r = client.get("/auth/me", headers=_bearer(pair["refresh_token"]))
    assert r.status_code == 401


def test_me_admin_gets_profile_with_full_permissions(client, db, auth):
    db.put(_user())
    r = client.get("/auth/me", headers=auth("ADMIN", user_id=str(ADMIN_ID)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "admin@bank.ru" and body["role"] == "ADMIN"
    assert body["must_change_password"] is False
    assert "password_hash" not in body and "token_version" not in body
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


def test_me_for_deleted_user_is_401(client, db):
    ghost = _user(user_id=uuid.uuid4())              # в БД его нет
    r = client.get("/auth/me", headers=_bearer(make_access_token(ghost)))
    assert r.status_code == 401
    assert r.json()["detail"] == "token revoked"


# ── must_change_password: только /auth/me и /auth/change-password ────────────
def test_pwd_change_token_only_reaches_me_and_change_password(client, db):
    db.put(_user(must_change_password=True))
    token = _login(client).json()["access_token"]
    me = client.get("/auth/me", headers=_bearer(token))
    assert me.status_code == 200 and me.json()["must_change_password"] is True
    for method, path in [("GET", "/auth/users"), ("GET", "/roles/permissions"),
                         ("GET", "/ml-limits"), ("GET", "/reference/segments")]:
        r = client.request(method, path, headers=_bearer(token))
        assert r.status_code == 403, (path, r.text)
        assert r.json() == {"detail": "password change required"}


def test_change_password_clears_flag_bumps_version_and_returns_new_pair(
        client, db, redis):
    user = _user(must_change_password=True)
    db.put(user)
    old = _login(client).json()
    r = client.post("/auth/change-password", headers=_bearer(old["access_token"]),
                    json={"current_password": "admin-pass",
                          "new_password": "Brand-new-passw0rd"})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["must_change_password"] is False and new["token_type"] == "bearer"
    assert verify_password("Brand-new-passw0rd", user.password_hash)
    assert user.must_change_password is False and user.token_version == 1
    assert user.password_changed_at is not None
    # новая пара работает везде, старые access/refresh — нет
    assert client.get("/auth/users", headers=_bearer(new["access_token"])).status_code == 200
    assert client.get("/auth/me", headers=_bearer(old["access_token"])).status_code == 401
    r = client.post("/auth/refresh", json={"refresh_token": old["refresh_token"]})
    assert r.status_code == 401
    r = client.post("/auth/refresh", json={"refresh_token": new["refresh_token"]})
    assert r.status_code == 200
    # и войти можно только с новым паролем
    assert _login(client).status_code == 401
    assert _login(client, password="Brand-new-passw0rd").status_code == 200


def test_change_password_wrong_current_is_401_and_throttled(client, db):
    user = _user()
    db.put(user)
    token = _login(client).json()["access_token"]
    body = {"current_password": "nope", "new_password": "Brand-new-passw0rd"}
    for _ in range(5):
        r = client.post("/auth/change-password", headers=_bearer(token), json=body)
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid current password"
    r = client.post("/auth/change-password", headers=_bearer(token), json=body)
    assert r.status_code == 429
    assert verify_password("admin-pass", user.password_hash)
    assert user.token_version == 0


@pytest.mark.parametrize("new_password,detail", [
    ("admin@bank.ru", "password must differ from email"),
    ("ADMIN@BANK.RU", "password must differ from email"),
    ("please-change-me", "password is a known default"),
    ("admin-pass-123", None),                               # годится
])
def test_change_password_policy(client, db, new_password, detail):
    user = _user(password_hash=hash_password("admin-pass-xyz"))
    db.put(user)
    token = _login(client, password="admin-pass-xyz").json()["access_token"]
    r = client.post("/auth/change-password", headers=_bearer(token),
                    json={"current_password": "admin-pass-xyz",
                          "new_password": new_password})
    if detail is None:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 422 and r.json() == {"detail": detail}
        assert user.token_version == 0


def test_change_password_rejects_same_short_or_overlong(client, db):
    db.put(_user(password_hash=hash_password("same-password-1")))
    token = _login(client, password="same-password-1").json()["access_token"]

    def _change(new):
        return client.post("/auth/change-password", headers=_bearer(token),
                           json={"current_password": "same-password-1",
                                 "new_password": new})

    r = _change("same-password-1")
    assert r.status_code == 422
    assert r.json()["detail"] == "new password must differ from the current one"
    assert _change("short-11chr").status_code == 422             # < 12 символов
    r = _change("я" * 40)                                         # 80 байт > 72
    assert r.status_code == 422 and "72 bytes" in r.text


def test_change_password_requires_token(client):
    r = client.post("/auth/change-password",
                    json={"current_password": "a", "new_password": "b" * 12})
    assert r.status_code == 401


# ── отзыв по token_version: разжалованный / отключённый админ (B35) ───────────
def test_demoted_admin_old_token_loses_admin_rights_immediately(client, db, auth):
    victim_id = uuid.uuid4()
    victim = _user("boss@bank.ru", user_id=victim_id)
    db.put(victim)
    stolen = _bearer(_login(client, "boss@bank.ru").json()["access_token"])
    assert client.get("/auth/users", headers=stolen).status_code == 200

    r = client.patch(f"/auth/users/{victim_id}", headers=auth("ADMIN"),
                     json={"role": "ANALYST"})
    assert r.status_code == 200, r.text
    assert victim.token_version == 1
    # старый токен (клейм role=ADMIN) больше не принимается вовсе
    r = client.post("/auth/users", headers=stolen, json={
        "email": "evil@bank.ru", "password": "s3cret!", "full_name": "Evil",
        "role": "ADMIN",
    })
    assert r.status_code == 401
    assert all(u.email != "evil@bank.ru" for u in db.rows(AdminUser))


def test_role_used_for_authorization_is_the_current_db_role(client, db):
    """Даже без смены token_version (прямая правка БД) роль берётся из БД."""
    user = _user("boss@bank.ru", user_id=uuid.uuid4())
    db.put(user)
    token = _bearer(_login(client, "boss@bank.ru").json()["access_token"])
    user.role = "MARKETER"
    assert client.get("/auth/users", headers=token).status_code == 403


def test_deactivated_admin_old_token_is_401(client, db, auth):
    victim_id = uuid.uuid4()
    db.put(_user("boss@bank.ru", user_id=victim_id))
    stolen = _bearer(_login(client, "boss@bank.ru").json()["access_token"])
    r = client.patch(f"/auth/users/{victim_id}", headers=auth("ADMIN"),
                     json={"is_active": False})
    assert r.status_code == 200, r.text
    r = client.get("/auth/me", headers=stolen)
    assert r.status_code == 401 and r.json()["detail"] == "token revoked"


def test_password_reset_by_admin_revokes_sessions(client, db, auth):
    uid = uuid.uuid4()
    other = _user("m@bank.ru", user_id=uid, role="MARKETER", password_hash=_HASH_OTHER)
    db.put(other)
    pair = _login(client, "m@bank.ru", "other-pass").json()
    r = client.patch(f"/auth/users/{uid}", headers=auth("ADMIN"),
                     json={"password": "brand-new"})
    assert r.status_code == 200, r.text
    assert other.token_version == 1 and other.password_changed_at is not None
    assert client.get("/auth/me", headers=_bearer(pair["access_token"])).status_code == 401
    r = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert r.status_code == 401


# ── гонка сброса пароля и смены пароля: строка под FOR UPDATE ────────────────
def test_change_password_after_a_concurrent_reset_is_401(client, db):
    """Админ сбросил пароль, пока смена пароля ждала блокировку строки:
    смена не перезаписывает его сброс (иначе держатель учётки сохранял бы
    свой пароль и свежие токены)."""
    user = _user()
    db.put(user)
    token = _login(client).json()["access_token"]
    reset_hash = _HASH_OTHER

    def admin_reset_commits_first():
        user.password_hash, user.token_version = reset_hash, user.token_version + 1

    db.on_lock = admin_reset_commits_first
    r = client.post("/auth/change-password", headers=_bearer(token),
                    json={"current_password": "admin-pass",
                          "new_password": "Attacker-chosen-42"})
    assert r.status_code == 401 and r.json()["detail"] == "token revoked"
    assert user.password_hash == reset_hash and user.token_version == 1
    assert db.locked == [(AdminUser, ADMIN_ID)]


def test_admin_update_locks_the_target_row(client, db, auth):
    uid = uuid.uuid4()
    other = _user("m@bank.ru", user_id=uid, role="MARKETER")
    db.put(other)
    seen = []
    # Чужая смена пароля закоммичена раньше: правка видит её версию и
    # поднимает поверх неё (k+2), а не перезаписывает значением k+1.
    db.on_lock = lambda: (seen.append(other.token_version),
                          setattr(other, "token_version", 1))
    r = client.patch(f"/auth/users/{uid}", headers=auth("ADMIN"),
                     json={"password": "brand-new"})
    assert r.status_code == 200, r.text
    assert db.locked == [(AdminUser, uid)] and seen == [0]
    assert other.token_version == 2


def test_name_change_keeps_sessions(client, db, auth):
    uid = uuid.uuid4()
    other = _user("m@bank.ru", user_id=uid, role="MARKETER")
    db.put(other)
    r = client.patch(f"/auth/users/{uid}", headers=auth("ADMIN"),
                     json={"full_name": "Мария", "role": "MARKETER",
                           "is_active": True})
    assert r.status_code == 200, r.text
    assert other.token_version == 0


# ── GET /auth/users ───────────────────────────────────────────────────────────
def test_list_users_admin_only(client, db, auth):
    db.put(_user(), _user("a@bank.ru", user_id=uuid.uuid4(), role="ANALYST",
                          created_at=_T0 + timedelta(days=1)))
    r = client.get("/auth/users", headers=auth("ADMIN"))
    assert r.status_code == 200, r.text
    assert [u["email"] for u in r.json()] == ["admin@bank.ru", "a@bank.ru"]
    assert all(u["must_change_password"] is False for u in r.json())

    assert client.get("/auth/users").status_code == 401
    for role in ("MARKETER", "ANALYST"):
        assert client.get("/auth/users", headers=auth(role)).status_code == 403


# ── POST /auth/users ──────────────────────────────────────────────────────────
def test_create_user_hashes_password_and_normalises_email(client, db, auth):
    headers = auth("ADMIN")
    r = client.post("/auth/users", headers=headers, json={
        "email": "New@Bank.ru", "password": "s3cret!", "full_name": "Новый",
        "role": "MARKETER",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "new@bank.ru" and body["role"] == "MARKETER"
    assert body["is_active"] is True and body["must_change_password"] is False
    (stored,) = (u for u in db.rows(AdminUser) if u.email == "new@bank.ru")
    assert stored.password_hash != "s3cret!"
    assert verify_password("s3cret!", stored.password_hash)
    assert stored.token_version == 0
    assert db.commits == 1


def test_create_user_rejects_password_over_bcrypt_limit(client, db, auth):
    r = client.post("/auth/users", headers=auth("ADMIN"), json={
        "email": "x@bank.ru", "password": "ж" * 40, "full_name": "X",
    })
    assert r.status_code == 422                       # было 500 (bcrypt ValueError)
    assert db.commits == 0


def test_create_user_duplicate_email_is_409(client, db, auth):
    headers = auth("ADMIN")
    r = client.post("/auth/users", headers=headers, json={
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
    assert all(u.email != "x@bank.ru" for u in db.rows(AdminUser))


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
    assert other.token_version == 1                  # один bump на запрос
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
    assert me.token_version == 0


def test_admin_can_edit_own_name(client, db, auth):
    me = _user()
    db.put(me)
    r = client.patch(f"/auth/users/{ADMIN_ID}",
                     headers=auth("ADMIN", user_id=str(ADMIN_ID)),
                     json={"full_name": "Главный", "role": "ADMIN"})
    assert r.status_code == 200, r.text
    assert me.full_name == "Главный" and me.token_version == 0


def test_update_user_requires_admin(client, db, auth):
    db.put(_user())
    r = client.patch(f"/auth/users/{ADMIN_ID}", headers=auth("MARKETER"),
                     json={"full_name": "hack"})
    assert r.status_code == 403
