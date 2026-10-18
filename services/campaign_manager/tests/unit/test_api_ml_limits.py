"""HTTP-тесты /ml-limits (фаза 18): чтение, upsert, снапшот для BRE R7."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from app.api.ml_limits import GLOBAL_ROW, SNAPSHOT_KEY, SNAPSHOT_TTL, publish_snapshot
from app.models import MlLimit, RolePermission
from app.rbac import PERMISSION_KEYS

_T1 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
_T2 = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)


def _limit(bucket, lo="1.00", hi="5.00", *, by="a@bank.ru", at=_T1, enabled=True):
    return MlLimit(segment_bucket=bucket, min_rate=Decimal(lo), max_rate=Decimal(hi),
                   daily_budget=Decimal("1000.00"), auto_approve=False,
                   risk_level="medium", enabled=enabled, updated_by=by, updated_at=at)


def _item(bucket, lo=2, hi=7, budget=5000, **extra):
    return {"segment_bucket": bucket, "min_rate": lo, "max_rate": hi,
            "daily_budget": budget, **extra}


# ── GET ───────────────────────────────────────────────────────────────────────
def test_get_requires_token(client):
    assert client.get("/ml-limits").status_code == 401


def test_get_needs_analytics_permission(client, db, auth):
    """B35: лимиты ML — аналитика; MARKETER (analytics=false) их не видит."""
    headers = auth("MARKETER")
    r = client.get("/ml-limits", headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "role MARKETER lacks permission 'analytics'"
    db.put(RolePermission(role="MARKETER", permissions={"analytics": True}))
    assert client.get("/ml-limits", headers=headers).status_code == 200


def test_get_empty_table_defaults_to_enabled(client, auth):
    r = client.get("/ml-limits", headers=auth("ANALYST"))
    assert r.status_code == 200, r.text
    assert r.json() == {"global_enabled": True, "limits": [],
                        "updated_by": None, "updated_at": None}


def test_get_splits_global_switch_sorts_buckets_and_reports_latest_editor(client, db, auth):
    db.put(_limit("young", by="old@bank.ru", at=_T1),
           _limit(GLOBAL_ROW, "0", "100", by="boss@bank.ru", at=_T2, enabled=False),
           _limit("mass", "0.50", "3.00"))
    r = client.get("/ml-limits", headers=auth("ANALYST"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["global_enabled"] is False
    # служебная строка __global__ не попадает в список корзин
    assert [x["segment_bucket"] for x in body["limits"]] == ["mass", "young"]
    assert Decimal(body["limits"][0]["max_rate"]) == Decimal("3.00")
    assert body["updated_by"] == "boss@bank.ru"
    assert body["updated_at"].startswith("2026-03-02")


# ── PUT ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role", ["MARKETER", "ANALYST"])
def test_put_is_admin_only(client, db, auth, role):
    # kill switch не делегируется: даже со всеми правами матрицы — 403
    db.put(RolePermission(role=role, permissions={k: True for k in PERMISSION_KEYS}))
    r = client.put("/ml-limits", headers=auth(role), json={"global_enabled": False})
    assert r.status_code == 403
    assert db.commits == 0 and db.rows(MlLimit) == []


def test_put_creates_buckets_and_publishes_snapshot(client, db, redis, auth):
    r = client.put("/ml-limits", headers=auth("ADMIN", email="boss@bank.ru"), json={
        "limits": [_item("premium", 3, 10, auto_approve=True, risk_level="low"),
                   _item("mass")],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["segment_bucket"] for x in body["limits"]] == ["mass", "premium"]
    assert body["updated_by"] == "boss@bank.ru"
    stored = {row.segment_bucket: row for row in db.rows(MlLimit)}
    assert set(stored) == {"premium", "mass"}
    assert stored["premium"].auto_approve is True
    assert stored["premium"].risk_level == "low"
    assert db.commits == 1

    ttl, raw = redis.data[SNAPSHOT_KEY]
    assert ttl == SNAPSHOT_TTL
    snap = json.loads(raw)
    assert snap["global_enabled"] is True
    assert snap["buckets"]["premium"] == {"min_rate": 3.0, "max_rate": 10.0,
                                          "enabled": True}
    assert set(snap["buckets"]) == {"premium", "mass"}


def test_put_updates_existing_bucket_in_place(client, db, auth):
    existing = _limit("young", "1.00", "2.00")
    db.put(existing)
    r = client.put("/ml-limits", headers=auth("ADMIN"),
                   json={"limits": [_item("young", 4, 9, 777)]})
    assert r.status_code == 200, r.text
    assert len(db.rows(MlLimit)) == 1               # обновили, а не задублировали
    assert existing.max_rate == Decimal("9") and existing.daily_budget == Decimal("777")


def test_put_global_switch_creates_global_row_and_snapshot_reflects_it(
        client, db, redis, auth):
    db.put(_limit("mass"))
    r = client.put("/ml-limits", headers=auth("ADMIN"), json={"global_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["global_enabled"] is False
    assert [x["segment_bucket"] for x in r.json()["limits"]] == ["mass"]
    g = db.tables[MlLimit][GLOBAL_ROW]
    assert g.enabled is False and g.updated_by == "admin@bank.ru"
    assert json.loads(redis.data[SNAPSHOT_KEY][1])["global_enabled"] is False

    # повторное включение переиспользует ту же строку
    r = client.put("/ml-limits", headers=auth("ADMIN"), json={"global_enabled": True})
    assert r.json()["global_enabled"] is True and g.enabled is True
    assert len(db.rows(MlLimit)) == 2


@pytest.mark.parametrize("payload", [{"limits": [_item("senior")]},
                                     {"global_enabled": False}])
def test_put_with_redis_down_is_503_but_keeps_the_change(client, db, redis, auth,
                                                         payload):
    """B39: снапшот не опубликован → R7 продолжает действовать по старому
    (выключенный kill switch молча не работал, а PUT отвечал 200)."""
    redis.fail = True
    r = client.put("/ml-limits", headers=auth("ADMIN"), json=payload)
    assert r.status_code == 503, r.text
    assert r.json() == {"detail": "saved but not published to the serving path; "
                                  "it will be retried"}
    # запись в БД закоммичена до публикации — её переиздаст планировщик
    assert db.commits == 1 and redis.data == {}
    if "limits" in payload:
        assert db.tables[MlLimit]["senior"].max_rate == Decimal("7")
    else:
        assert db.tables[MlLimit][GLOBAL_ROW].enabled is False

    redis.fail = False                      # Redis вернулся: задача republish
    asyncio.run(publish_snapshot(redis, db))
    snap = json.loads(redis.data[SNAPSHOT_KEY][1])
    if "limits" in payload:
        assert snap["buckets"]["senior"]["max_rate"] == 7.0
    else:
        assert snap["global_enabled"] is False


@pytest.mark.parametrize("bad", [
    {"limits": [_item("vip")]},                         # неизвестная корзина
    {"limits": [_item("mass", 2, 150)]},                # ставка > 100 %
    {"limits": [_item("mass", risk_level="extreme")]},  # неизвестный риск
    {"limits": []},                                     # пустой список
])
def test_put_validates_payload(client, db, auth, bad):
    r = client.put("/ml-limits", headers=auth("ADMIN"), json=bad)
    assert r.status_code == 422
    assert db.commits == 0


# ── publish_snapshot (его же зовёт планировщик) ──────────────────────────────
@pytest.mark.asyncio
async def test_publish_snapshot_from_db_state(db, redis):
    db.put(_limit("business", "0.10", "1.50"),
           _limit(GLOBAL_ROW, "0", "100", enabled=True))
    await publish_snapshot(redis, db)
    snap = json.loads(redis.data[SNAPSHOT_KEY][1])
    assert snap == {"global_enabled": True,
                    "buckets": {"business": {"min_rate": 0.1, "max_rate": 1.5,
                                             "enabled": True}}}


@pytest.mark.asyncio
async def test_snapshot_honours_each_bucket_enabled(db, redis):
    """B39/B52: выключенная корзина уходит в снапшот выключенной — R7
    отклоняет её офферы (раньше в снапшот всегда писалось enabled=true)."""
    db.put(_limit("young", enabled=False), _limit("mass"),
           _limit(GLOBAL_ROW, "0", "100", enabled=False))
    await publish_snapshot(redis, db)
    snap = json.loads(redis.data[SNAPSHOT_KEY][1])
    assert snap["global_enabled"] is False
    assert snap["buckets"]["young"]["enabled"] is False
    assert snap["buckets"]["mass"]["enabled"] is True


def test_put_keeps_a_disabled_bucket_disabled(client, db, redis, auth):
    """PUT меняет ставки, но не включает выключенную корзину: enabled в
    теле запроса нет, и снапшот после PUT по-прежнему её отключает."""
    db.put(_limit("young", enabled=False))
    r = client.put("/ml-limits", headers=auth("ADMIN"),
                   json={"limits": [_item("young", 2, 9)]})
    assert r.status_code == 200, r.text
    snap = json.loads(redis.data[SNAPSHOT_KEY][1])
    assert snap["buckets"]["young"] == {"min_rate": 2.0, "max_rate": 9.0,
                                        "enabled": False}
