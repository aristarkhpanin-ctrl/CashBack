"""TransactionListener — Kafka delivery semantics (stage C §2, B45).

A fake consumer with real seek/position semantics stands in for Kafka: what
it re-delivers after a seek is exactly what aiokafka would. The live
counterpart is tests/integration/test_listener_kafka.py.
"""
from __future__ import annotations

import json
import struct
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Optional

import fastavro
import pytest
import redis.exceptions as redis_exc
from aiokafka import TopicPartition
from aiokafka.errors import CommitFailedError, IllegalStateError, KafkaConnectionError
from app import listener as mod
from app.accrual.engine import AccrualResult
from app.config import Settings
from app.listener import (
    PermanentMessageError,
    TransactionListener,
    is_retryable,
    parse_transaction,
    parse_transaction_date,
)
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import SerializationError
from sqlalchemy.exc import DBAPIError, OperationalError

USER = str(uuid.uuid4())
CAMPAIGN = str(uuid.uuid4())
REC = str(uuid.uuid4())
TOPIC = "transactions.raw"
TP0 = TopicPartition(TOPIC, 0)
TP1 = TopicPartition(TOPIC, 1)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
@dataclass
class Msg:
    partition: int
    offset: int
    value: Any
    key: Optional[bytes] = b"k"
    topic: str = TOPIC
    headers: tuple = ()


def event(**over: Any) -> dict:
    evt = {"transaction_id": f"tx-{uuid.uuid4().hex[:8]}", "user_id": USER,
           "mcc_code": "5411", "amount": "1500.00", "currency": "RUB",
           "transaction_date": "2026-10-17T15:00:00+03:00", "channel": "POS",
           "merchant_id": None, "metadata": None}
    evt.update(over)
    return evt


def msg(partition: int, offset: int, value: Any = None, **kw: Any) -> Msg:
    if value is None:
        value = event()
    if isinstance(value, dict):
        value = json.dumps(value).encode()   # FakeDeserializer: JSON stands for Avro
    return Msg(partition, offset, value, **kw)


class FakeDeserializer:
    def __init__(self) -> None:
        self.raise_for: dict[bytes, Exception] = {}

    def __call__(self, value: bytes, ctx: Any) -> Any:
        if value in self.raise_for:
            raise self.raise_for[value]
        try:
            return json.loads(value)
        except ValueError:
            raise SerializationError("Unexpected magic byte") from None


class FakeConsumer:
    """Holds the records of each partition and a position, like Kafka."""

    def __init__(self, records: list[Msg], on_drained: Any = None) -> None:
        self.records: dict[TopicPartition, list[Msg]] = {}
        for m in records:
            self.records.setdefault(TopicPartition(m.topic, m.partition), []).append(m)
        self.position = {tp: msgs[0].offset for tp, msgs in self.records.items()}
        self.on_drained = on_drained
        self.commits: list[dict] = []
        self.seeks: list[tuple] = []
        self.commit_error: Optional[Exception] = None
        self.seek_error: Optional[Exception] = None
        self.started = self.stopped = False
        self.polls = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def getmany(self, timeout_ms: int = 0, max_records: Optional[int] = None):
        self.polls += 1
        batch = {}
        for tp, msgs in self.records.items():
            pending = [m for m in msgs if m.offset >= self.position[tp]]
            if pending:
                batch[tp] = pending
                self.position[tp] = pending[-1].offset + 1
        if not batch and self.on_drained is not None:
            self.on_drained()
        return batch

    async def commit(self, offsets: dict) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits.append(dict(offsets))

    def seek(self, tp: TopicPartition, offset: int) -> None:
        if self.seek_error is not None:
            raise self.seek_error
        self.seeks.append((tp, offset))
        self.position[tp] = offset


class FakeRedis:
    def __init__(self, offers: Optional[dict[str, Any]] = None) -> None:
        self.offers = offers if offers is not None else {
            f"accepted_offers:{USER}:5411": json.dumps(
                {"campaign_id": CAMPAIGN, "recommendation_id": REC,
                 "accepted_at": "2026-10-17T11:00:00+00:00", "activation_id": "a"})}
        self.error: Optional[Exception] = None

    async def get(self, key: str) -> Any:
        if self.error is not None:
            raise self.error
        return self.offers.get(key)


@dataclass
class FakeEngine:
    outcomes: list[Any] = field(default_factory=list)   # exceptions or results
    calls: list[dict] = field(default_factory=list)

    async def accrue(self, **kwargs: Any) -> AccrualResult:
        self.calls.append(kwargs)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return AccrualResult(accrual_id=uuid.uuid4(), cashback_amount=Decimal("75"))


class FakeProducer:
    def __init__(self, fail: Optional[Exception] = None) -> None:
        self.fail = fail
        self.sent: list[dict] = []
        self.started = self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(self, topic: str, value: Any = None, key: Any = None,
                            headers: Any = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append({"topic": topic, "value": value, "key": key,
                          "headers": dict(headers)})


def settings(**over: Any) -> Settings:
    return Settings(transactions_topic=TOPIC, retry_backoff_initial_s=1.0,
                    retry_backoff_max_s=30.0, **over)


def make(records: list[Msg], *, engine: Optional[FakeEngine] = None,
         redis: Optional[FakeRedis] = None, dlq: Optional[FakeProducer] = None):
    lst = TransactionListener(settings(), engine or FakeEngine(), redis or FakeRedis(),
                              dlq_producer=dlq or FakeProducer())
    consumer = FakeConsumer(records, on_drained=lst._stop.set)
    lst._consumer = consumer
    lst._deserializer = FakeDeserializer()
    lst._registry = FakeRegistry()
    lst._dlq_topic_ready = True
    return lst, consumer


async def one_batch(lst: TransactionListener, consumer: FakeConsumer) -> bool:
    return await lst._process_batch(await consumer.getmany())


def retryable_db_error(sqlstate: str = "40P01") -> DBAPIError:
    return OperationalError("SELECT 1", {}, _PgError(sqlstate))


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"pg error {sqlstate}")
        self.sqlstate = sqlstate


# ---------------------------------------------------------------------------
# consumer configuration
# ---------------------------------------------------------------------------
async def test_accrual_group_starts_from_the_earliest_offset(monkeypatch):
    created: dict[str, Any] = {}

    class Consumer(FakeConsumer):
        def __init__(self, *topics: str, **kwargs: Any) -> None:
            super().__init__([])
            created.update(kwargs, topics=topics)

    monkeypatch.setattr(mod, "AIOKafkaConsumer", Consumer)
    monkeypatch.setattr(mod, "SchemaRegistryClient", lambda conf: conf)
    monkeypatch.setattr(mod, "AvroDeserializer", lambda registry: FakeDeserializer())
    lst = TransactionListener(settings(), FakeEngine(), FakeRedis())

    await lst.start()

    assert created["auto_offset_reset"] == "earliest"
    assert created["enable_auto_commit"] is False
    assert created["group_id"] == "cashback-accrual-group"
    assert created["topics"] == (TOPIC,)
    assert lst._consumer.started


# ---------------------------------------------------------------------------
# happy path and business skips — committed
# ---------------------------------------------------------------------------
async def test_purchase_with_an_accepted_offer_is_accrued_at_the_transaction_time():
    engine = FakeEngine()
    m = msg(0, 7, event(transaction_id="tx-a", amount=" 1500.00 ", mcc_code="5411 "))
    lst, consumer = make([m], engine=engine)

    assert await one_batch(lst, consumer)

    [call] = engine.calls
    assert call == {
        "user_id": USER, "campaign_id": CAMPAIGN, "recommendation_id": REC,
        "transaction_id": "tx-a", "mcc": "5411", "amount": Decimal("1500.00"),
        "transaction_at": datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
    }
    assert consumer.commits == [{TP0: 8}]


@pytest.mark.parametrize("offers,outcome", [
    ({}, "no_offer"),
    ({f"accepted_offers:{USER}:5411": json.dumps({"campaign_id": CAMPAIGN})},
     "legacy_offer"),
    ({f"accepted_offers:{USER}:5411": "{not json"}, "bad_offer"),
    ({f"accepted_offers:{USER}:5411": "[1, 2]"}, "bad_offer"),
    ({f"accepted_offers:{USER}:5411": json.dumps(
        {"campaign_id": CAMPAIGN, "recommendation_id": "nope"})}, "bad_offer"),
    ({f"accepted_offers:{USER}:5411": json.dumps(
        {"campaign_id": "nope", "recommendation_id": REC})}, "bad_offer"),
], ids=["no-key", "legacy", "not-json", "not-object", "bad-rid", "bad-campaign"])
async def test_purchase_without_a_usable_offer_is_not_accrued(offers, outcome):
    engine = FakeEngine()
    lst, consumer = make([msg(0, 0)], engine=engine, redis=FakeRedis(offers))

    assert await lst._process(consumer.records[TP0][0]) == outcome
    assert await one_batch(lst, consumer)
    assert engine.calls == []
    assert consumer.commits == [{TP0: 1}]


async def test_skip_reason_of_the_engine_is_the_outcome():
    engine = FakeEngine([AccrualResult(skipped_reason="daily_limit_reached")])
    lst, consumer = make([msg(0, 0)], engine=engine)
    assert await lst._process(consumer.records[TP0][0]) == "daily_limit_reached"


# ---------------------------------------------------------------------------
# retryable failures — not committed, sought back, backoff
# ---------------------------------------------------------------------------
async def test_retryable_failure_commits_the_prefix_and_seeks_back():
    engine = FakeEngine([AccrualResult(accrual_id=uuid.uuid4()), retryable_db_error()])
    records = [msg(0, 10), msg(0, 11), msg(0, 12), msg(1, 5), msg(1, 6)]
    lst, consumer = make(records, engine=engine)

    assert await one_batch(lst, consumer) is False

    assert consumer.commits == [{TP0: 11}]                # offset 10 done, 11 failed
    assert consumer.seeks == [(TP0, 11), (TP1, 5)]        # nothing skipped
    assert len(engine.calls) == 2                         # 12, 5, 6 not touched
    # the next poll re-delivers exactly the unprocessed records
    again = await consumer.getmany()
    assert {tp: [m.offset for m in ms] for tp, ms in again.items()} == {
        TP0: [11, 12], TP1: [5, 6]}


async def test_failure_in_a_later_partition_keeps_the_earlier_ones_committed():
    engine = FakeEngine([AccrualResult(accrual_id=uuid.uuid4()),
                         AccrualResult(accrual_id=uuid.uuid4()),
                         ConnectionRefusedError("pg down")])
    lst, consumer = make([msg(0, 3), msg(0, 4), msg(1, 9)], engine=engine)

    assert await one_batch(lst, consumer) is False

    assert consumer.commits == [{TP0: 5}]
    assert consumer.seeks == [(TP1, 9)]


async def test_redis_outage_is_retried():
    redis = FakeRedis()
    redis.error = redis_exc.ConnectionError("redis down")
    lst, consumer = make([msg(0, 0)], redis=redis)

    assert await one_batch(lst, consumer) is False
    assert consumer.commits == []
    assert consumer.seeks == [(TP0, 0)]


async def test_schema_registry_outage_is_retried():
    lst, consumer = make([msg(0, 0)])
    value = consumer.records[TP0][0].value
    lst._deserializer.raise_for[value] = ConnectionError("registry down")

    assert await one_batch(lst, consumer) is False
    assert consumer.seeks == [(TP0, 0)]


async def test_run_backs_off_exponentially_and_recovers_without_losing_a_message():
    failures = [retryable_db_error("08006") for _ in range(7)]
    engine = FakeEngine(failures)
    lst, consumer = make([msg(0, 0), msg(0, 1)], engine=engine)
    pauses: list[float] = []

    async def pause(seconds: float) -> None:
        pauses.append(seconds)

    lst._pause = pause
    lst.start = _noop                                      # consumer injected

    await lst.run()

    assert pauses == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert [c["transaction_id"] for c in engine.calls[-2:]] == [
        json.loads(consumer.records[TP0][0].value)["transaction_id"],
        json.loads(consumer.records[TP0][1].value)["transaction_id"]]
    assert consumer.commits[-1] == {TP0: 2}
    assert lst._retry_delay == 1.0                         # reset after success
    assert consumer.stopped


async def _noop() -> None:
    return None


# ---------------------------------------------------------------------------
# permanent failures — DLQ, then committed
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,error", [
    (b"garbage", "wire format"),
    (event(transaction_date="yesterday"), "transaction_date"),
    (event(transaction_date=""), "transaction_date"),
    ({k: v for k, v in event().items() if k != "transaction_date"}, "transaction_date"),
    (event(user_id=""), "user_id"),
    (event(amount="12,50"), "amount"),
    (event(amount="NaN"), "amount"),
    (event(user_id="not-a-uuid"), "user_id"),
    ([1, 2, 3], "TransactionEvent record"),
], ids=["undecodable", "bad-date", "empty-date", "no-date", "no-user", "bad-amount",
        "nan-amount", "user-not-uuid", "not-a-record"])
async def test_permanent_failure_goes_to_the_dlq_and_is_committed(value, error):
    dlq = FakeProducer()
    if isinstance(value, dict) and value.get("user_id") == "not-a-uuid":
        redis = FakeRedis({"accepted_offers:not-a-uuid:5411": json.dumps(
            {"campaign_id": CAMPAIGN, "recommendation_id": REC})})
    else:
        redis = FakeRedis()
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    m = Msg(0, 41, raw, key=b"user-key", headers=(("trace", b"t1"),))
    lst, consumer = make([m], dlq=dlq, redis=redis)

    assert await one_batch(lst, consumer)

    [sent] = dlq.sent
    assert sent["topic"] == "transactions.accrual.dlq"
    assert sent["value"] == raw and sent["key"] == b"user-key"
    assert error.encode() in sent["headers"]["dlq.error"]
    assert sent["headers"]["trace"] == b"t1"
    assert sent["headers"]["dlq.source.topic"] == TOPIC.encode()
    assert sent["headers"]["dlq.source.partition"] == b"0"
    assert sent["headers"]["dlq.source.offset"] == b"41"
    assert sent["headers"]["dlq.consumer.group"] == b"cashback-accrual-group"
    assert consumer.commits == [{TP0: 42}]


async def test_data_error_from_postgres_is_dead_lettered():
    engine = FakeEngine([DBAPIError("INSERT", {}, _PgError("22001"))])  # too long
    dlq = FakeProducer()
    lst, consumer = make([msg(0, 0)], engine=engine, dlq=dlq)

    assert await one_batch(lst, consumer)
    assert len(dlq.sent) == 1
    assert consumer.commits == [{TP0: 1}]


async def test_failed_dlq_publish_is_retried_not_committed():
    lst, consumer = make([msg(0, 0, b"junk"), msg(0, 1)],
                         dlq=FakeProducer(fail=KafkaConnectionError()))

    assert await one_batch(lst, consumer) is False
    assert consumer.commits == []
    assert consumer.seeks == [(TP0, 0)]


# ---------------------------------------------------------------------------
# commit / seek failures (rebalance) are not fatal
# ---------------------------------------------------------------------------
async def test_commit_failure_is_logged_and_the_loop_goes_on():
    lst, consumer = make([msg(0, 0), msg(0, 1)])
    consumer.commit_error = CommitFailedError("rebalanced")
    before = mod.COMMIT_FAILURES._value.get()

    assert await one_batch(lst, consumer) is True
    assert mod.COMMIT_FAILURES._value.get() == before + 1

    lst.start = _noop
    consumer.position[TP0] = 0                              # more to read
    await lst.run()                                         # does not raise
    assert consumer.stopped


async def test_seek_on_a_revoked_partition_is_not_fatal():
    engine = FakeEngine([ConnectionRefusedError("pg down")])
    lst, consumer = make([msg(0, 0)], engine=engine)
    consumer.seek_error = IllegalStateError("No current assignment")

    assert await one_batch(lst, consumer) is False


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------
async def test_stop_closes_the_consumer_and_an_owned_dlq_producer(monkeypatch):
    producer = FakeProducer()
    monkeypatch.setattr(mod, "AIOKafkaProducer", lambda **kw: producer)
    lst = TransactionListener(settings(), FakeEngine(), FakeRedis())
    lst._consumer = consumer = FakeConsumer([])
    lst._dlq_topic_ready = True
    assert await lst._dlq() is producer and producer.started

    await lst.stop()

    assert lst._stop.is_set()
    assert consumer.stopped and producer.stopped
    assert lst._consumer is None and lst._dlq_producer is None


async def test_crashed_run_closes_the_consumer_but_can_be_restarted(monkeypatch):
    lst, consumer = make([msg(0, 0)])

    async def boom(**_: Any):
        raise KafkaConnectionError()

    consumer.getmany = boom
    lst.start = _noop
    with pytest.raises(KafkaConnectionError):
        await lst.run()
    assert consumer.stopped
    assert not lst._stop.is_set()          # the supervisor may run it again


async def test_injected_dlq_producer_is_not_closed_by_the_listener():
    dlq = FakeProducer()
    lst, consumer = make([], dlq=dlq)
    await lst.stop()
    assert not dlq.stopped


# ---------------------------------------------------------------------------
# DLQ topic on demand
# ---------------------------------------------------------------------------
class FakeAdmin:
    def __init__(self, errors: list[tuple] | None = None,
                 fail: Optional[Exception] = None) -> None:
        self.errors = errors or []
        self.fail = fail
        self.created: list[Any] = []
        self.closed = False

    async def start(self) -> None:
        if self.fail is not None:
            raise self.fail

    async def create_topics(self, topics: list[Any]):
        self.created.extend(topics)
        return type("Resp", (), {"topic_errors": self.errors})()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("code,ready", [(0, True), (36, True), (41, False)])
async def test_dlq_topic_is_created_on_demand(monkeypatch, code, ready):
    admin = FakeAdmin([("transactions.accrual.dlq", code, None)])
    monkeypatch.setattr(mod, "AIOKafkaAdminClient", lambda **kw: admin)
    lst = TransactionListener(settings(dlq_replication_factor=3), FakeEngine(),
                              FakeRedis(), dlq_producer=FakeProducer())

    await lst._dlq()

    [topic] = admin.created
    assert (topic.name, topic.num_partitions, topic.replication_factor) == (
        "transactions.accrual.dlq", 1, 3)
    assert lst._dlq_topic_ready is ready and admin.closed


async def test_dlq_publish_is_attempted_when_the_topic_cannot_be_created(monkeypatch):
    monkeypatch.setattr(mod, "AIOKafkaAdminClient",
                        lambda **kw: FakeAdmin(fail=KafkaConnectionError()))
    dlq = FakeProducer()
    lst, consumer = make([msg(0, 0, b"junk")], dlq=dlq)
    lst._dlq_topic_ready = False

    assert await one_batch(lst, consumer)
    assert len(dlq.sent) == 1 and lst._dlq_topic_ready is False


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc,retry", [
    (PermanentMessageError("bad"), False),
    (OperationalError("q", {}, _PgError("40P01")), True),     # deadlock
    (OperationalError("q", {}, _PgError("40001")), True),     # serialization failure
    (OperationalError("q", {}, _PgError("08006")), True),     # connection failure
    (DBAPIError("q", {}, _PgError("55000")), True),           # db not accepting conns
    (DBAPIError("q", {}, _PgError("57P01")), True),           # admin shutdown
    (DBAPIError("q", {}, _PgError("42883")), True),           # function missing: deploy
    (DBAPIError("q", {}, _PgError("22P02")), False),          # invalid text
    (DBAPIError("q", {}, _PgError("23503")), False),          # FK violation
    (ConnectionRefusedError(), True),
    (TimeoutError(), True),
    (redis_exc.ConnectionError(), True),
    (redis_exc.TimeoutError(), True),
    (redis_exc.ReadOnlyError("READONLY replica"), True),
    (redis_exc.ResponseError("WRONGTYPE Operation against a key"), False),
    (KafkaConnectionError(), True),
    (RuntimeError("unexpected"), True),
], ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else str(v))
def test_only_failures_about_the_message_itself_are_permanent(exc, retry):
    assert is_retryable(exc) is retry


def test_sqlstate_is_found_through_the_cause_chain():
    try:
        try:
            raise _PgError("23505")
        except _PgError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_retryable(outer) is False


# ---------------------------------------------------------------------------
# decoding (Avro wire format) — permanent vs transient
# ---------------------------------------------------------------------------
_SCHEMA = {"type": "record", "name": "T", "fields": [{"name": "a", "type": "string"}]}


def _wire(schema_id: int, body: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", schema_id) + body


def _avro(record: dict) -> bytes:
    import io
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, fastavro.parse_schema(_SCHEMA), record)
    return buf.getvalue()


class FakeRegistry:
    def __init__(self, error: Optional[Exception] = None, references: Any = None):
        self.error = error
        self.references = references

    def get_schema(self, schema_id: int):
        if self.error is not None:
            raise self.error
        return type("S", (), {"schema_str": json.dumps(_SCHEMA),
                              "references": self.references})()


def _decoder(error: Exception, registry: Optional[FakeRegistry] = None):
    lst = TransactionListener(settings(), FakeEngine(), FakeRedis())

    def deserializer(value: Any, ctx: Any) -> Any:
        raise error

    lst._deserializer = deserializer
    lst._registry = registry or FakeRegistry()
    return lst


class HttpxLikeError(Exception):
    pass


HttpxLikeError.__module__ = "httpx._exceptions"


@pytest.mark.parametrize("value,error,registry,permanent", [
    (b"{json}", SerializationError("bad magic"), None, "wire format"),
    (b"\x00\x00", SerializationError("short"), None, "wire format"),
    ("text", SerializationError("not bytes"), None, "not bytes"),
    (_wire(7, b"\xff\xff\xff"), SerializationError("eof"), None, "does not decode"),
    (_wire(7, _avro({"a": "x"})), SerializationError("??"), None, None),
    (_wire(7, b""), SchemaRegistryError(404, 40403, "Schema not found"), None,
     "unknown schema id"),
    (_wire(7, b""), SchemaRegistryError(500, 50001, "boom"), None, None),
    (_wire(7, b""), ConnectionError("down"), None, None),
    (_wire(7, b""), HttpxLikeError("connect timeout"), None, None),
    (_wire(7, b"\xff"), SerializationError("x"),
     FakeRegistry(SchemaRegistryError(404, 40403, "nf")), "unknown schema id"),
    (_wire(7, b"\xff"), SerializationError("x"), FakeRegistry(references=["r"]), None),
], ids=["json", "short", "not-bytes", "garbage-body", "decodable", "sr-404",
        "sr-500", "network", "httpx", "lookup-404", "references"])
def test_decode_failure_classification(value, error, registry, permanent):
    lst = _decoder(error, registry)
    if permanent is None:
        with pytest.raises(type(error)):
            lst._decode(value)
    else:
        with pytest.raises(PermanentMessageError, match=permanent):
            lst._decode(value)


def test_registry_outage_during_the_check_is_transient():
    lst = _decoder(SerializationError("x"),
                   FakeRegistry(SchemaRegistryError(503, 50003, "unavailable")))
    with pytest.raises(SchemaRegistryError):
        lst._decode(_wire(7, b"\xff"))


def test_decoded_value_must_be_a_record():
    lst = TransactionListener(settings(), FakeEngine(), FakeRedis())
    lst._deserializer = lambda value, ctx: ["not", "a", "record"]
    with pytest.raises(PermanentMessageError, match="TransactionEvent record"):
        lst._decode(b"\x00\x00\x00\x00\x01")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("2026-10-17T12:00:00Z", datetime(2026, 10, 17, 12, tzinfo=UTC)),
    ("2026-10-17T15:00:00+03:00", datetime(2026, 10, 17, 12, tzinfo=UTC)),
    ("2026-10-17T12:00:00", datetime(2026, 10, 17, 12, tzinfo=UTC)),   # naive = UTC
    (" 2026-10-17 12:00:00+00:00 ", datetime(2026, 10, 17, 12, tzinfo=UTC)),
    ("yesterday", None),
    ("0001-01-01T00:00:00+01:00", None),                              # overflow
    (1760702400, None),
    (None, None),
])
def test_transaction_date_parsing(raw, expected):
    parsed = parse_transaction_date(raw)
    assert parsed == expected
    if parsed is not None:
        assert parsed.tzinfo is UTC


def test_parse_transaction_strips_and_converts():
    tx = parse_transaction(event(transaction_id=" tx-1 ", user_id=f" {USER} ",
                                 mcc_code="5411 ", amount="99.90"))
    assert (tx.transaction_id, tx.user_id, tx.mcc, tx.amount) == (
        "tx-1", USER, "5411", Decimal("99.90"))


@pytest.mark.parametrize("field_name", mod.REQUIRED_FIELDS)
def test_every_required_field_is_checked(field_name):
    with pytest.raises(PermanentMessageError, match=field_name):
        parse_transaction(event(**{field_name: "  "}))
    with pytest.raises(PermanentMessageError, match=field_name):
        parse_transaction(event(**{field_name: None}))


def test_parse_transaction_requires_a_record():
    with pytest.raises(PermanentMessageError, match="TransactionEvent record"):
        parse_transaction(["not", "a", "record"])


async def test_pause_returns_early_when_stopping():
    import asyncio

    lst = TransactionListener(settings(), FakeEngine(), FakeRedis())
    await lst._pause(0.0)
    lst._stop.set()
    await asyncio.wait_for(lst._pause(30.0), timeout=1.0)
