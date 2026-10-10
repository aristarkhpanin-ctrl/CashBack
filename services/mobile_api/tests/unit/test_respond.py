"""POST /v1/mobile/recommendations/{id}/respond — переходы статуса (B13).

Отклик меняет статус одним условным UPDATE (только PENDING и не истёкший).
Тесты гоняют эндпоинт на фейках: in-memory таблица ``recommendations`` с
транзакциями, Redis, Kafka-продюсер и планировщик SNOOZE. Фейк БД исполняет
UPDATE так, как его написал код: условие ``response_status = '…'`` и
``expires_at > now()`` применяются, только если они есть в тексте запроса, —
поэтому снятие guard'а из SQL роняет тесты, а не проходит незамеченным.
"""
from __future__ import annotations

import copy
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from app.api import mobile
from app.config import Settings
from app.scheduling import SnoozeScheduler
from fastapi import FastAPI


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class FakeRecommendationsDB:
    """Таблица ``recommendations`` + ``begin()`` с откатом при исключении."""

    def __init__(self, now: datetime) -> None:
        self.now = now  # now() «базы» — общий для всей транзакции
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail_commit = False

    def add(
        self,
        *,
        status: str = "PENDING",
        expires_in: timedelta | None = timedelta(hours=2),
        mcc_code: str = "5411",
    ) -> uuid.UUID:
        """``expires_in=None`` — строка с NULL в expires_at."""
        rid = uuid.uuid4()
        self.rows[str(rid)] = {
            "user_id": str(uuid.uuid4()),
            "campaign_id": str(uuid.uuid4()),
            "mcc_code": mcc_code,
            "expires_at": None if expires_in is None else self.now + expires_in,
            "response_status": status,
            "responded_at": None,
            "channel": None,
        }
        return rid

    def view(self, rid: str) -> SimpleNamespace:
        row = self.rows[rid]
        exp = row["expires_at"]
        return SimpleNamespace(
            rid=rid, **row, expired=exp is None or exp <= self.now,
        )

    def begin(self) -> _FakeTx:
        return _FakeTx(self)


class _FakeTx:
    def __init__(self, db: FakeRecommendationsDB) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeConn:
        self._snapshot = copy.deepcopy(self._db.rows)
        return _FakeConn(self._db)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            self._db.rows = self._snapshot
            return False
        if self._db.fail_commit:
            self._db.rows = self._snapshot
            raise ConnectionError("connection lost during COMMIT")
        return False


class _FakeConn:
    def __init__(self, db: FakeRecommendationsDB) -> None:
        self._db = db

    async def execute(self, clause: Any, params: dict[str, Any]) -> _Result:
        sql = " ".join(str(clause).split())
        rid = str(params["rid"])
        row = self._db.rows.get(rid)
        if sql.startswith("SELECT"):
            return _Result([self._db.view(rid)] if row else [])
        if not sql.startswith("UPDATE recommendations"):
            raise AssertionError(f"unexpected SQL: {sql}")

        set_part, where_part = sql.split(" WHERE ", 1)
        if row is None:
            return _Result([])
        guard = re.search(r"response_status = '(\w+)'", where_part)
        if guard and row["response_status"] != guard.group(1):
            return _Result([])
        if "expires_at > now()" in where_part and not (
            row["expires_at"] is not None and row["expires_at"] > self._db.now
        ):
            return _Result([])

        literal = re.search(r"SET response_status = '(\w+)'", set_part)
        row["response_status"] = literal.group(1) if literal else params["status"]
        if "responded_at = now()" in set_part:
            row["responded_at"] = self._db.now
        if "channel = :channel" in set_part:
            row["channel"] = params["channel"]
        return _Result([self._db.view(rid)] if "RETURNING" in sql else [])


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.fail_setex = False

    async def setex(self, key: str, ttl: int, value: str) -> None:
        if self.fail_setex:
            raise ConnectionError("redis unavailable")
        if ttl <= 0:  # как настоящий Redis: ERR invalid expire time
            raise ValueError("invalid expire time in 'setex' command")
        self.store[key] = value
        self.ttls[key] = ttl

    async def delete(self, *keys: str) -> int:
        return sum(self.store.pop(k, None) is not None for k in keys)


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, topic: str, key: bytes, value: bytes) -> None:
        self.sent.append({"topic": topic, "key": key, "value": json.loads(value)})


class FakeSnooze:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.scheduled: list[str] = []

    def schedule(self, recommendation_id: str, days: int) -> datetime:
        self.scheduled.append(recommendation_id)
        return self.now + timedelta(days=days)


# ---------------------------------------------------------------------------
@pytest.fixture
async def env():
    now = datetime.now(UTC)
    db = FakeRecommendationsDB(now)
    redis = FakeRedis()
    producer = FakeProducer()
    snooze = FakeSnooze(now)

    app = FastAPI()
    app.include_router(mobile.router)
    app.state.settings = Settings(_env_file=None)
    app.state.db_engine = db
    app.state.redis = redis
    app.state.kafka_producer = producer
    app.state.snooze_scheduler = snooze

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client, db=db, redis=redis, producer=producer,
            snooze=snooze, settings=app.state.settings,
        )


async def _respond(env, rid: uuid.UUID, action: str) -> httpx.Response:
    return await env.client.post(
        f"/v1/mobile/recommendations/{rid}/respond", json={"action": action},
    )


def _assert_no_side_effects(env) -> None:
    assert env.redis.store == {}
    assert env.producer.sent == []
    assert env.snooze.scheduled == []


# ---------------------------------------------------------------------------
# 200
# ---------------------------------------------------------------------------
async def test_accept_pending_offer_sets_key_with_ttl_from_expires_at(env):
    rid = env.db.add(expires_in=timedelta(hours=2))
    row = env.db.rows[str(rid)]

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 200, resp.text
    key = f"accepted_offers:{row['user_id']}:5411"
    assert resp.json()["accepted_offer_key"] == key
    assert json.loads(env.redis.store[key]) == {
        "campaign_id": row["campaign_id"],
        "recommendation_id": str(rid),
        "accepted_at": env.db.now.isoformat(),
    }
    # TTL — остаток жизни оффера, без искусственного минимума.
    assert 7190 <= env.redis.ttls[key] <= 7200
    assert row["response_status"] == "ACCEPTED"
    assert row["channel"] == "IN_APP"
    assert row["responded_at"] == env.db.now

    [event] = env.producer.sent
    assert event["topic"] == env.settings.offer_accepted_topic
    assert event["key"] == row["user_id"].encode()
    assert event["value"]["recommendation_id"] == str(rid)
    assert event["value"]["ttl_seconds"] == env.redis.ttls[key]


async def test_decline_pending_offer_has_no_side_effects(env):
    rid = env.db.add()

    resp = await _respond(env, rid, "DECLINED")

    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted_offer_key"] is None
    assert env.db.rows[str(rid)]["response_status"] == "DECLINED"
    _assert_no_side_effects(env)


# ---------------------------------------------------------------------------
# 404
# ---------------------------------------------------------------------------
async def test_unknown_recommendation_returns_404(env):
    resp = await _respond(env, uuid.uuid4(), "ACCEPTED")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "recommendation not found"
    _assert_no_side_effects(env)


# ---------------------------------------------------------------------------
# 409 — решение уже принято
# ---------------------------------------------------------------------------
async def test_reaccepting_paid_out_offer_returns_409_and_no_new_key(env):
    """Главный денежный сценарий B13: принял → купил (listener начислил и
    удалил ключ) → «принял» ещё раз. Ключ не должен появиться снова."""
    rid = env.db.add()
    first = await _respond(env, rid, "ACCEPTED")
    assert first.status_code == 200, first.text
    env.redis.store.clear()  # listener: начисление прошло, DEL accepted_offers:*

    again = await _respond(env, rid, "ACCEPTED")

    assert again.status_code == 409, again.text
    assert again.json()["detail"] == "recommendation already responded (status=ACCEPTED)"
    assert env.redis.store == {}
    assert len(env.producer.sent) == 1  # только от первого принятия


async def test_accept_after_decline_returns_409(env):
    rid = env.db.add()
    assert (await _respond(env, rid, "DECLINED")).status_code == 200

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 409, resp.text
    assert "status=DECLINED" in resp.json()["detail"]
    assert env.db.rows[str(rid)]["response_status"] == "DECLINED"
    _assert_no_side_effects(env)


async def test_decline_after_accept_returns_409_and_keeps_accepted(env):
    rid = env.db.add()
    assert (await _respond(env, rid, "ACCEPTED")).status_code == 200

    resp = await _respond(env, rid, "DECLINED")

    assert resp.status_code == 409, resp.text
    assert "status=ACCEPTED" in resp.json()["detail"]
    assert env.db.rows[str(rid)]["response_status"] == "ACCEPTED"


async def test_snooze_blocks_until_reactivation_then_accept_works(env):
    rid = env.db.add()
    snoozed = await _respond(env, rid, "SNOOZE")
    assert snoozed.status_code == 200, snoozed.text
    assert snoozed.json()["snooze_until"] is not None
    assert env.snooze.scheduled == [str(rid)]

    early = await _respond(env, rid, "ACCEPTED")
    assert early.status_code == 409, early.text
    assert "status=SNOOZE" in early.json()["detail"]
    assert env.redis.store == {}

    # Настоящая реактивация из scheduling.py: SNOOZE → PENDING.
    await SnoozeScheduler(env.db)._reactivate(str(rid))
    assert env.db.rows[str(rid)]["response_status"] == "PENDING"

    later = await _respond(env, rid, "ACCEPTED")
    assert later.status_code == 200, later.text
    assert later.json()["accepted_offer_key"] in env.redis.store


# ---------------------------------------------------------------------------
# 410 — оффер истёк
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["ACCEPTED", "DECLINED", "SNOOZE"])
async def test_expired_pending_offer_returns_410_without_side_effects(env, action):
    rid = env.db.add(expires_in=timedelta(seconds=-1))

    resp = await _respond(env, rid, action)

    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"] == "recommendation expired"
    row = env.db.rows[str(rid)]
    assert row["response_status"] == "PENDING"
    assert row["responded_at"] is None
    _assert_no_side_effects(env)


async def test_offer_expiring_exactly_now_is_gone(env):
    rid = env.db.add(expires_in=timedelta(0))

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 410, resp.text
    _assert_no_side_effects(env)


async def test_offer_marked_expired_returns_410(env):
    rid = env.db.add(status="EXPIRED", expires_in=timedelta(days=1))

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 410, resp.text
    _assert_no_side_effects(env)


async def test_offer_without_expires_at_is_not_activated(env):
    """expires_at NOT NULL в схеме; если NULL всё же попал — fail-closed."""
    rid = env.db.add(expires_in=None)

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 410, resp.text
    _assert_no_side_effects(env)


@pytest.mark.parametrize(("current", "code", "detail"), [
    (None, 404, "recommendation not found"),
    (SimpleNamespace(response_status="PENDING", expired=True), 410, "recommendation expired"),
    (SimpleNamespace(response_status="EXPIRED", expired=False), 410, "recommendation expired"),
    # UPDATE не прошёл, а строка снова PENDING и не истекла — значит, между
    # UPDATE и SELECT её вернула реактивация SNOOZE: конфликт, можно повторить.
    (SimpleNamespace(response_status="PENDING", expired=False), 409,
     "recommendation changed concurrently, retry"),
    (SimpleNamespace(response_status="ACCEPTED", expired=True), 409,
     "recommendation already responded (status=ACCEPTED)"),
])
def test_rejection_maps_current_state_to_status_code(current, code, detail):
    exc = mobile._respond_rejection(current)
    assert (exc.status_code, exc.detail) == (code, detail)


# ---------------------------------------------------------------------------
# Сбои Redis / Kafka / COMMIT
# ---------------------------------------------------------------------------
async def test_kafka_failure_after_commit_keeps_accept(env):
    async def broken_send(*args, **kwargs):
        raise ConnectionError("kafka unavailable")

    env.producer.send = broken_send
    rid = env.db.add()

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 200, resp.text
    assert resp.json()["accepted_offer_key"] in env.redis.store
    assert env.db.rows[str(rid)]["response_status"] == "ACCEPTED"


async def test_redis_failure_rolls_back_transition_so_retry_succeeds(env):
    rid = env.db.add()
    env.redis.fail_setex = True

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 503, resp.text
    assert env.db.rows[str(rid)]["response_status"] == "PENDING"
    assert env.producer.sent == []

    env.redis.fail_setex = False
    retry = await _respond(env, rid, "ACCEPTED")
    assert retry.status_code == 200, retry.text
    assert retry.json()["accepted_offer_key"] in env.redis.store


async def test_commit_failure_after_setex_removes_key(env):
    rid = env.db.add()
    env.db.fail_commit = True

    resp = await _respond(env, rid, "ACCEPTED")

    assert resp.status_code == 500
    assert env.db.rows[str(rid)]["response_status"] == "PENDING"
    _assert_no_side_effects(env)
