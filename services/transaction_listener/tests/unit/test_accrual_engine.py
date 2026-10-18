"""AccrualEngine — order of checks and side effects (stage C §2, B45).

The SQL itself (eligible_campaigns, calculate_cashback, locks, UNIQUE) is
proven on real Postgres in tests/integration/test_accrual_engine_pg.py;
here a scripted connection checks what the engine does with each answer:
which reason it skips with, that nothing is written or published on a skip,
that the budget is untouched on a duplicate, and when the Redis key is
compare-and-deleted.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

import pytest
from app.accrual import engine as mod
from app.accrual.engine import AccrualEngine

USER = str(uuid.uuid4())
CAMPAIGN = str(uuid.uuid4())
REC = str(uuid.uuid4())
AT = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
KEY = f"accepted_offers:{USER}:5411"


class Row(tuple):
    """A result row: attribute access + ``row[0]``."""

    def __new__(cls, **fields: Any) -> Row:
        row = super().__new__(cls, tuple(fields.values()))
        row.__dict__.update(fields)
        return row


def offer_row(**over: Any) -> Row:
    fields = {"user_id": USER, "campaign_id": CAMPAIGN, "mcc_code": "5411",
              "response_status": "ACCEPTED", "responded_at": AT - timedelta(hours=1),
              "expires_at": AT + timedelta(days=1)}
    fields.update(over)
    return Row(**fields)


def eligible_row(**over: Any) -> Row:
    fields = {"budget_total": Decimal("1000"), "budget_spent": Decimal("0"),
              "daily_limit": None, "spent_today": Decimal("0")}
    fields.update(over)
    return Row(**fields)


@dataclass
class World:
    """What Postgres answers to each statement of the engine."""

    offer: Optional[Row] = None
    paid: tuple[Row, ...] = ()             # accruals of this offer / this purchase
    eligible: Optional[Row] = None
    cashback: Any = Decimal("75.00")
    insert_conflict: bool = False          # INSERT … ON CONFLICT DO NOTHING → no row
    paid_after_conflict: tuple[Row, ...] = ()

    def __post_init__(self) -> None:
        self.offer = self.offer if self.offer is not None else offer_row()
        self.eligible = self.eligible if self.eligible is not None else eligible_row()


class FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value

    def all(self) -> list[Any]:
        return list(self._value)

    def scalar(self) -> Any:
        return self._value


class FakeConn:
    def __init__(self, world: World, *, missing_eligible: bool = False,
                 fail_on: Any = None) -> None:
        self.world = world
        self.missing_eligible = missing_eligible
        self.fail_on = fail_on
        self.calls: list[tuple[Any, dict]] = []
        self._paid_queries = 0

    async def execute(self, stmt: Any, params: Optional[dict] = None) -> FakeResult:
        self.calls.append((stmt, params or {}))
        if stmt is self.fail_on:
            raise ConnectionResetError("connection lost")
        w = self.world
        if stmt is mod._LOCK_OFFER_SQL:
            return FakeResult(w.offer)
        if stmt is mod._PAID_SQL:
            self._paid_queries += 1
            return FakeResult(w.paid if self._paid_queries == 1 else w.paid_after_conflict)
        if stmt is mod._LOCK_CAMPAIGN_SQL:
            return FakeResult(Row(campaign_id=CAMPAIGN))
        if stmt is mod._ELIGIBLE_SQL:
            return FakeResult(None if self.missing_eligible else w.eligible)
        if stmt is mod._CASHBACK_SQL:
            return FakeResult(w.cashback)
        if stmt is mod._INSERT_ACCRUAL_SQL:
            return FakeResult(None if w.insert_conflict
                              else Row(accrual_id=str(params["accrual_id"])))
        if stmt is mod._UPDATE_BUDGET_SQL:
            return FakeResult(None)
        raise AssertionError(f"unexpected statement {stmt}")

    def statements(self) -> list[Any]:
        return [stmt for stmt, _ in self.calls]

    def params(self, stmt: Any) -> dict:
        return next(p for s, p in self.calls if s is stmt)


class FakeDB:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn
        self.commits = 0
        self.rollbacks = 0

    def begin(self) -> FakeDB:
        return self

    async def __aenter__(self) -> FakeConn:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commits += 1
        else:
            self.rollbacks += 1
        return False


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.evals: list[tuple] = []

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.evals.append((script, numkeys, *args))
        return 1


class FakeProducer:
    def __init__(self, fail: bool = False, log: Optional[list] = None) -> None:
        self.fail = fail
        self.sent: list[dict] = []
        self.log = log

    async def send(self, topic: str, *, key: bytes, value: bytes) -> None:
        if self.fail:
            raise RuntimeError("kafka down")
        self.sent.append({"topic": topic, "key": key.decode(),
                          "value": json.loads(value.decode())})


def make(world: Optional[World] = None, *, redis: Optional[FakeRedis] = None,
         producer: Optional[FakeProducer] = None, **conn_kw: Any):
    conn = FakeConn(world or World(), **conn_kw)
    db = FakeDB(conn)
    redis = redis or FakeRedis()
    producer = producer or FakeProducer()
    return AccrualEngine(db, producer, redis), conn, db, redis, producer


async def accrue(engine: AccrualEngine, **over: Any):
    kwargs = {"user_id": USER, "campaign_id": CAMPAIGN, "recommendation_id": REC,
              "transaction_id": "tx-1", "mcc": "5411", "amount": Decimal("1500.00"),
              "transaction_at": AT}
    kwargs.update(over)
    return await engine.accrue(**kwargs)


# ---------------------------------------------------------------------------
async def test_accrual_runs_every_check_in_one_transaction_then_releases_the_offer():
    engine, conn, db, redis, producer = make()

    result = await accrue(engine)

    assert result.accrued and result.cashback_amount == Decimal("75.00")
    assert conn.statements() == [
        mod._LOCK_OFFER_SQL, mod._PAID_SQL, mod._LOCK_CAMPAIGN_SQL,
        mod._ELIGIBLE_SQL, mod._CASHBACK_SQL, mod._INSERT_ACCRUAL_SQL,
        mod._UPDATE_BUDGET_SQL,
    ]
    assert db.commits == 1
    assert "FOR UPDATE" in str(mod._LOCK_OFFER_SQL)
    # serialises accruals of the campaign, not the FK checks of its offers
    assert "FOR NO KEY UPDATE" in str(mod._LOCK_CAMPAIGN_SQL)
    # "already paid" — by this offer OR for this purchase
    assert conn.params(mod._PAID_SQL) == {"rid": REC, "tid": "tx-1"}
    assert "recommendation_id = :rid OR transaction_id = :tid" in str(mod._PAID_SQL)
    # eligibility and cashback at the TRANSACTION time, with the MCC
    assert conn.params(mod._ELIGIBLE_SQL) == {
        "uid": USER, "mcc": "5411", "amount": Decimal("1500.00"), "at": AT,
        "cid": CAMPAIGN}
    assert conn.params(mod._CASHBACK_SQL) == {
        "cid": CAMPAIGN, "mcc": "5411", "amount": Decimal("1500.00")}
    assert "calculate_cashback(:cid, :mcc, :amount)" in str(mod._CASHBACK_SQL)
    insert = conn.params(mod._INSERT_ACCRUAL_SQL)
    assert insert["recommendation_id"] == REC
    assert insert["cashback_amount"] == Decimal("75.00")
    assert conn.params(mod._UPDATE_BUDGET_SQL) == {"amount": Decimal("75.00"),
                                                   "cid": CAMPAIGN}
    # compare-and-delete with this offer's id
    [(script, numkeys, key, rid)] = redis.evals
    assert (numkeys, key, rid) == (1, KEY, REC)
    assert "recommendation_id" in script and "DEL" in script
    [event] = producer.sent
    assert event["topic"] == "cashback.accrued" and event["key"] == USER
    assert event["value"]["recommendation_id"] == REC
    assert event["value"]["transaction_at"] == AT.isoformat()
    assert event["value"]["cashback_amount"] == "75.00"
    assert event["value"]["accrual_id"] == str(result.accrual_id)


@pytest.mark.parametrize("offer", [
    None,                                              # no such recommendation
    offer_row(user_id=str(uuid.uuid4())),              # someone else's
    offer_row(campaign_id=str(uuid.uuid4())),          # another campaign
    offer_row(mcc_code="5812"),                        # another MCC
], ids=["missing", "user", "campaign", "mcc"])
async def test_offer_that_does_not_match_the_purchase_is_not_paid(offer):
    world = World()
    world.offer = offer
    engine, conn, _, redis, producer = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "offer_mismatch"
    assert conn.statements() == [mod._LOCK_OFFER_SQL]
    assert redis.evals == [] and producer.sent == []


async def test_campaign_id_that_is_not_a_uuid_is_a_mismatch():
    engine, conn, *_ = make()
    assert (await accrue(engine, campaign_id="nope")).skipped_reason == "offer_mismatch"


async def test_offer_with_an_accrual_is_a_duplicate_and_its_key_is_released():
    world = World(paid=(Row(accrual_id=str(uuid.uuid4()), recommendation_id=REC),))
    engine, conn, db, redis, producer = make(world)

    result = await accrue(engine, transaction_id="tx-2")

    assert result.skipped_reason == "duplicate"
    assert conn.statements() == [mod._LOCK_OFFER_SQL, mod._PAID_SQL]
    assert [e[2:] for e in redis.evals] == [(KEY, REC)]
    assert producer.sent == []


@pytest.mark.parametrize("paid_by", [str(uuid.uuid4()), None],
                         ids=["another-offer", "legacy-row"])
async def test_purchase_paid_under_another_offer_is_a_duplicate_that_keeps_this_offer(
        paid_by):
    """A replay of a paid purchase meets a newer offer of another campaign
    on the user×MCC key: nothing is paid twice, and the newer offer — still
    unpaid — keeps its key for a real purchase."""
    world = World(paid=(Row(accrual_id=str(uuid.uuid4()), recommendation_id=paid_by),))
    engine, conn, _, redis, producer = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "duplicate"
    assert conn.statements() == [mod._LOCK_OFFER_SQL, mod._PAID_SQL]
    assert redis.evals == [] and producer.sent == []


async def test_this_offer_among_the_accruals_of_the_purchase_is_consumed():
    world = World(paid=(Row(accrual_id="a", recommendation_id=None),
                        Row(accrual_id="b", recommendation_id=REC)))
    engine, _, _, redis, _ = make(world)

    assert (await accrue(engine)).skipped_reason == "duplicate"
    assert [e[2:] for e in redis.evals] == [(KEY, REC)]


@pytest.mark.parametrize("status", ["PENDING", "DECLINED", "SNOOZE", "EXPIRED"])
async def test_offer_that_is_not_accepted_is_not_paid(status):
    world = World(offer=offer_row(response_status=status))
    engine, conn, _, redis, producer = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "offer_not_accepted"
    assert mod._INSERT_ACCRUAL_SQL not in conn.statements()
    assert redis.evals == []


@pytest.mark.parametrize("responded_at", [
    AT + timedelta(seconds=1),                 # accepted a second after the purchase
    AT + timedelta(days=2),                    # long after (a replay, consumer lag)
    None,                                      # acceptance time unknown
], ids=["second-after", "days-after", "unknown"])
async def test_purchase_before_the_accept_is_not_paid_and_keeps_the_key(responded_at):
    world = World(offer=offer_row(responded_at=responded_at))
    engine, conn, _, redis, producer = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "accepted_after_purchase"
    assert mod._LOCK_CAMPAIGN_SQL not in conn.statements()
    assert redis.evals == [] and producer.sent == []


async def test_purchase_in_the_second_of_the_accept_is_paid():
    """transaction_date has whole seconds: a purchase stamped 12:00:00 right
    after an accept at 12:00:00.7 counts as after it."""
    world = World(offer=offer_row(responded_at=AT + timedelta(microseconds=700_000)))
    engine, *_ = make(world)
    assert (await accrue(engine)).accrued


async def test_expiry_is_judged_at_the_transaction_time():
    expired = World(offer=offer_row(expires_at=AT))
    engine, conn, _, redis, _ = make(expired)
    assert (await accrue(engine)).skipped_reason == "offer_expired"
    assert mod._LOCK_CAMPAIGN_SQL not in conn.statements()
    assert redis.evals == []

    # bought a second before expiry, processed long after — still paid
    on_time = World(offer=offer_row(expires_at=AT + timedelta(seconds=1)))
    engine, *_ = make(on_time)
    assert (await accrue(engine)).accrued


async def test_campaign_that_is_not_eligible_is_not_paid():
    engine, conn, _, redis, producer = make(missing_eligible=True)

    result = await accrue(engine)

    assert result.skipped_reason == "not_eligible"
    assert conn.statements()[-1] is mod._ELIGIBLE_SQL
    assert redis.evals == [] and producer.sent == []


@pytest.mark.parametrize("cashback", [Decimal("0"), None])
async def test_zero_cashback_is_not_paid(cashback):
    engine, conn, *_ = make(World(cashback=cashback))
    assert (await accrue(engine)).skipped_reason == "zero_cashback"
    assert mod._INSERT_ACCRUAL_SQL not in conn.statements()


async def test_daily_limit_pays_nothing_partial():
    world = World(eligible=eligible_row(daily_limit=Decimal("300"),
                                        spent_today=Decimal("280")))
    engine, conn, _, redis, _ = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "daily_limit_reached"
    assert mod._INSERT_ACCRUAL_SQL not in conn.statements()
    assert redis.evals == []


async def test_daily_limit_can_be_reached_exactly():
    world = World(eligible=eligible_row(daily_limit=Decimal("300"),
                                        spent_today=Decimal("225")))
    engine, *_ = make(world)
    assert (await accrue(engine)).accrued


async def test_budget_is_not_overspent():
    world = World(eligible=eligible_row(budget_total=Decimal("1000"),
                                        budget_spent=Decimal("950")))
    engine, conn, *_ = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "budget_exhausted"
    assert result.metadata == {"remaining": "50", "needed": "75.00"}
    assert mod._INSERT_ACCRUAL_SQL not in conn.statements()


async def test_conflict_on_this_offer_is_a_duplicate_without_a_budget_change():
    world = World(insert_conflict=True,
                  paid_after_conflict=(Row(accrual_id="a", recommendation_id=REC),))
    engine, conn, _, redis, producer = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "duplicate"
    assert mod._UPDATE_BUDGET_SQL not in conn.statements()
    assert [e[2:] for e in redis.evals] == [(KEY, REC)]
    assert producer.sent == []


async def test_conflict_on_the_transaction_leaves_another_offer_unconsumed():
    """The purchase was paid meanwhile by another offer (a concurrent replay
    under another key): this offer is not consumed, so its key must survive."""
    world = World(insert_conflict=True, paid_after_conflict=(
        Row(accrual_id="a", recommendation_id=str(uuid.uuid4())),))
    engine, conn, _, redis, _ = make(world)

    result = await accrue(engine)

    assert result.skipped_reason == "duplicate"
    assert mod._UPDATE_BUDGET_SQL not in conn.statements()
    assert redis.evals == []


async def test_kafka_and_redis_failures_after_the_commit_do_not_undo_the_accrual():
    engine, _, db, *_ = make(redis=FakeRedis(fail=True),
                             producer=FakeProducer(fail=True))

    result = await accrue(engine)

    assert result.accrued
    assert db.commits == 1


async def test_database_errors_propagate_and_roll_back():
    """The listener must see the failure to retry — never a silent skip."""
    engine, _, db, redis, producer = make(fail_on=mod._INSERT_ACCRUAL_SQL)

    with pytest.raises(ConnectionResetError):
        await accrue(engine)

    assert db.rollbacks == 1 and db.commits == 0
    assert redis.evals == [] and producer.sent == []


async def test_naive_transaction_time_is_rejected():
    engine, conn, *_ = make()
    with pytest.raises(ValueError, match="timezone-aware"):
        await accrue(engine, transaction_at=datetime(2026, 10, 17, 12, 0))
    assert conn.calls == []
