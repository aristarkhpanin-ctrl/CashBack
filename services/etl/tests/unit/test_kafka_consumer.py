"""Unit tests for the Kafka side of the daily ETL (B43 b, d).

* extract stages messages but never commits offsets;
* the offsets it consumed end up in an :class:`ExtractManifest`;
* :func:`commit_offsets` commits exactly those offsets, idempotently;
* messages that can never be decoded are counted, logged and dead-lettered;
* anything else (Schema Registry down, 5xx, a deserializer bug) fails the
  batch instead: dead-lettering it would commit past valid transactions.
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import time
import types
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from aiokafka import TopicPartition
from app import kafka_consumer
from app.kafka_consumer import (
    ExtractManifest,
    StagingWriter,
    TransactionConsumer,
    commit_offsets,
)
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import SerializationError
from structlog.testing import capture_logs

from tests.unit.fakes import (
    STRING_SCHEMA_ID,
    FakeAvroDeserializer,
    FakeKafka,
    FakeSchemaRegistry,
    Msg,
    avro_value,
    msg,
)

TOPIC = "transactions.raw"
GROUP = "cashback-etl-group"


def _record(i: int) -> dict:
    return {
        "transaction_id": f"tx-{i}",
        "user_id": "00000000-0000-0000-0000-000000000001",
        "mcc_code": "5411",
        "amount": "10.00",
        "currency": "RUB",
        "transaction_date": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
        "channel": "POS",
        "merchant_id": None,
        "metadata": None,
    }


@pytest.fixture
def registry(monkeypatch):
    """The Schema Registry every TransactionConsumer in the test talks to."""
    sr = FakeSchemaRegistry()

    def client(conf):
        assert conf == {"url": "http://schema-registry.invalid:8081"}
        return sr

    monkeypatch.setattr(kafka_consumer, "SchemaRegistryClient", client)
    monkeypatch.setattr(kafka_consumer, "AvroDeserializer", FakeAvroDeserializer)
    return sr


@pytest.fixture
def make_consumer(tmp_path, monkeypatch, registry):
    """TransactionConsumer wired to an in-memory Kafka and Schema Registry."""

    async def _make(batches):
        kafka = FakeKafka(batches)
        monkeypatch.setattr(kafka_consumer, "AIOKafkaConsumer", kafka.factory)
        consumer = TransactionConsumer(
            bootstrap_servers="kafka:9092",
            schema_registry_url="http://schema-registry.invalid:8081",
            topic=TOPIC,
            staging=StagingWriter(tmp_path),
            group_id=GROUP,
        )
        await consumer.start()
        return consumer, kafka

    return _make


# ---------------------------------------------------------------------------
# (b) extract never commits; it tracks offsets per partition
# ---------------------------------------------------------------------------
async def test_consume_batch_stages_without_committing(make_consumer):
    batch = [msg(_record(i), partition=i % 2, offset=100 + i) for i in range(6)]
    consumer, kafka = await make_consumer([batch])

    count, path = await consumer.consume_batch()

    assert count == 6
    assert pq.read_table(path).num_rows == 6
    assert kafka.commits == []  # committing here loses data if load fails
    assert consumer.pending_offsets() == {
        TopicPartition(TOPIC, 0): 105,  # offsets 100, 102, 104 -> next 105
        TopicPartition(TOPIC, 1): 106,  # offsets 101, 103, 105 -> next 106
    }
    consumer_kwargs = kafka.consumers[0].kwargs
    assert consumer_kwargs["enable_auto_commit"] is False
    assert consumer_kwargs["group_id"] == GROUP


async def test_drain_returns_manifest_of_staged_files_and_offsets(make_consumer):
    batches = [
        [msg(_record(i), offset=i) for i in range(3)],
        [msg(_record(10 + i), partition=1, offset=40 + i) for i in range(2)],
    ]
    consumer, kafka = await make_consumer(batches)
    consumer.BATCH_SIZE = 3  # a full batch keeps polling within consume_batch

    manifest = await consumer.drain(idle_sleep=0)

    assert manifest.topic == TOPIC and manifest.group_id == GROUP
    assert manifest.offsets == {0: 3, 1: 42}
    assert manifest.records == 5
    assert sum(pq.read_table(f).num_rows for f in manifest.files) == 5
    assert manifest.dead_letter_count == 0 and manifest.dead_letter_files == []
    assert kafka.commits == []


async def test_drain_stops_at_max_batches(make_consumer):
    batches = [[msg(_record(i), offset=i)] for i in range(5)]
    consumer, _kafka = await make_consumer(batches)
    manifest = await consumer.drain(max_batches=2, idle_sleep=0)
    assert len(manifest.files) == 2
    assert manifest.offsets == {0: 2}  # only what was actually staged


async def test_manifest_round_trips_through_staging(tmp_path):
    manifest = ExtractManifest(
        topic=TOPIC, group_id=GROUP, offsets={0: 7, 3: 12},
        files=["/data/a.parquet"], records=9,
        dead_letter_files=["/data/dl.jsonl"], dead_letter_count=1,
    )
    path = StagingWriter(tmp_path).write_manifest(manifest)
    assert ExtractManifest.read(path) == manifest
    assert json.loads(path.read_text())["offsets"] == {"0": 7, "3": 12}
    assert list(path.parent.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# (b) commit after load: exactly the manifest offsets, idempotent
# ---------------------------------------------------------------------------
def _manifest(offsets: dict[int, int]) -> ExtractManifest:
    return ExtractManifest(topic=TOPIC, group_id=GROUP, offsets=offsets)


async def test_commit_offsets_commits_exactly_the_manifest():
    kafka = FakeKafka()
    committed = await commit_offsets(_manifest({0: 105, 1: 106}), "kafka:9092",
                                     consumer_factory=kafka.factory)

    expected = {TopicPartition(TOPIC, 0): 105, TopicPartition(TOPIC, 1): 106}
    assert kafka.commits == [expected]
    assert kafka.group_committed == expected
    assert committed == {0: 105, 1: 106}
    [consumer] = kafka.consumers
    assert consumer.kwargs == {"bootstrap_servers": "kafka:9092", "group_id": GROUP,
                               "enable_auto_commit": False}
    assert consumer.started and consumer.stopped


async def test_commit_offsets_defaults_to_aiokafka(monkeypatch):
    kafka = FakeKafka()
    monkeypatch.setattr(kafka_consumer, "AIOKafkaConsumer", kafka.factory)
    await commit_offsets(_manifest({2: 9}), "kafka:9092")
    assert kafka.group_committed == {TopicPartition(TOPIC, 2): 9}


async def test_commit_offsets_is_idempotent():
    kafka = FakeKafka()
    manifest = _manifest({0: 105, 1: 106})
    await commit_offsets(manifest, "kafka:9092", consumer_factory=kafka.factory)
    again = await commit_offsets(manifest, "kafka:9092", consumer_factory=kafka.factory)
    assert again == {}
    assert len(kafka.commits) == 1


async def test_commit_offsets_never_rewinds_the_group():
    kafka = FakeKafka(committed={TopicPartition(TOPIC, 0): 500, TopicPartition(TOPIC, 1): 90})
    committed = await commit_offsets(_manifest({0: 105, 1: 106}), "kafka:9092",
                                     consumer_factory=kafka.factory)
    assert committed == {1: 106}
    assert kafka.group_committed[TopicPartition(TOPIC, 0)] == 500


async def test_commit_offsets_without_offsets_does_not_connect():
    def factory(**_kw):
        raise AssertionError("must not connect")

    assert await commit_offsets(_manifest({}), "kafka:9092", consumer_factory=factory) == {}


async def test_commit_offsets_stops_consumer_on_failure():
    kafka = FakeKafka(fail_commit=True)
    with pytest.raises(RuntimeError, match="CommitFailedError"):
        await commit_offsets(_manifest({0: 1}), "kafka:9092", consumer_factory=kafka.factory)
    assert kafka.consumers[0].stopped
    assert kafka.group_committed == {}


# ---------------------------------------------------------------------------
# (d) messages that can never be decoded: counted, logged, dead-lettered
# ---------------------------------------------------------------------------
async def test_undecodable_messages_are_dead_lettered(make_consumer):
    good = avro_value(_record(0))
    bad_utf8 = good.replace(b"tx-0", b"tx-\xff")
    batch = [
        msg(_record(0), offset=10),
        Msg(TOPIC, 0, 11, b"junk, not avro", key=b"k1"),          # bad magic byte
        Msg(TOPIC, 0, 12, b"\x00\x00\x01"),                       # truncated framing
        Msg(TOPIC, 0, 13, b"\x00\x00\x00\x00\x07not-avro"),        # schema id 7: 404 / 40403
        Msg(TOPIC, 0, 14, good[:20]),                              # Avro payload cut short
        Msg(TOPIC, 0, 15, bad_utf8),                               # invalid UTF-8 in a string
        Msg(TOPIC, 0, 16, avro_value("x", STRING_SCHEMA_ID)),      # decodes, but not a record
        Msg(TOPIC, 0, 17, None),                                   # tombstone
        msg(_record(3), offset=18),
    ]
    consumer, kafka = await make_consumer([batch])

    with capture_logs() as logs:
        count, path = await consumer.consume_batch()

    assert count == 2
    assert pq.read_table(path).column("transaction_id").to_pylist() == ["tx-0", "tx-3"]
    assert consumer.dead_letter_count == 7
    assert consumer.pending_offsets() == {TopicPartition(TOPIC, 0): 19}  # past the bad ones
    assert kafka.commits == []

    [dl_path] = consumer.dead_letter_paths
    assert dl_path.parent == path.parent  # staging dir of the same day
    entries = [json.loads(line) for line in dl_path.read_text().splitlines()]
    assert [(e["partition"], e["offset"]) for e in entries] == [(0, o) for o in range(11, 18)]
    assert base64.b64decode(entries[0]["value"]) == b"junk, not avro"
    assert base64.b64decode(entries[0]["key"]) == b"k1"
    assert all(e["error"].startswith("UndecodableMessageError: ") for e in entries)
    reasons = [e["error"] for e in entries]
    assert "wire format" in reasons[0] and "wire format" in reasons[1]
    assert "unknown schema id" in reasons[2] and "40403" in reasons[2]
    assert "does not decode with schema id 1: EOFError" in reasons[3]
    assert "does not decode with schema id 1: UnicodeDecodeError" in reasons[4]
    assert "got str" in reasons[5]
    assert entries[6]["value"] is None and "got NoneType" in reasons[6]
    assert entries[3]["cause"].startswith("EOFError")  # what the deserializer raised
    failed = [e for e in logs if e["event"] == "deserialise_failed"]
    assert [e["offset"] for e in failed] == list(range(11, 18))


def _transient_errors() -> list:
    """What the Schema Registry client raises while the registry is unavailable."""
    errors = [
        ConnectionRefusedError(111, "Connection refused"),
        TimeoutError("timed out"),
        SchemaRegistryError(503, 50003, "Error while forwarding the request to the leader"),
        SchemaRegistryError(500, -1, "Unknown Schema Registry Error: b'<html>'"),
        SchemaRegistryError(401, 40101, "Unauthorized"),
        # 404 from something that is not a Schema Registry (wrong URL / proxy):
        SchemaRegistryError(404, -1, "Unknown Schema Registry Error: b'There is no handle'"),
    ]
    # The real client's own network errors: requests (confluent-kafka 2.3,
    # the Airflow image) or httpx / httpx2 (newer releases).
    for module, name in (("requests.exceptions", "ConnectionError"),
                         ("requests.exceptions", "ReadTimeout"),
                         ("httpx", "ConnectError"), ("httpx2", "ConnectError")):
        try:
            errors.append(getattr(importlib.import_module(module), name)("Connection refused"))
        except ImportError:
            pass
    return errors


def _error_id(error: BaseException) -> str:
    name = f"{type(error).__module__.partition('.')[0]}.{type(error).__name__}"
    return f"{name}-{error.http_status_code}" if isinstance(error, SchemaRegistryError) else name


@pytest.mark.parametrize("error", _transient_errors(), ids=_error_id)
async def test_registry_unavailable_fails_the_batch_instead_of_dead_lettering(
        make_consumer, registry, tmp_path, error):
    consumer, kafka = await make_consumer([[msg(_record(i), offset=i) for i in range(3)]])
    registry.error = error

    with pytest.raises(type(error)):
        await consumer.consume_batch()

    # Nothing set aside, nothing staged: the extract task fails and retries.
    assert consumer.dead_letter_count == 0 and consumer.dead_letter_paths == []
    assert list(tmp_path.rglob("*.*")) == []
    assert kafka.commits == []


async def test_corrupt_message_is_not_dead_lettered_while_registry_is_down(
        make_consumer, registry):
    """Cached schema decodes the first message; the corrupt one cannot be
    confirmed undecodable while the registry is down, so the batch fails."""
    good = avro_value(_record(1))
    consumer, _kafka = await make_consumer([[msg(_record(0), offset=0),
                                             Msg(TOPIC, 0, 1, good[:20])]])
    original = consumer._deserializer.__call__

    def go_down_after_first(data, ctx):
        result = original(data, ctx)
        registry.error = ConnectionRefusedError(111, "Connection refused")
        return result

    consumer._deserializer = go_down_after_first
    with pytest.raises(ConnectionRefusedError):
        await consumer.consume_batch()
    assert consumer.dead_letter_count == 0


async def test_deserializer_failure_on_a_decodable_message_propagates(make_consumer):
    """An error that is not about the bytes (here a bug) must not dead-letter them."""
    consumer, _kafka = await make_consumer([[msg(_record(0), offset=0)]])
    consumer._deserializer.bug = AttributeError("'NoneType' object has no attribute 'schema_str'")

    with pytest.raises(AttributeError):
        await consumer.consume_batch()
    assert consumer.dead_letter_count == 0


async def test_unexplained_error_on_an_unknown_schema_id_is_dead_lettered(make_consumer):
    consumer, _kafka = await make_consumer([[Msg(TOPIC, 0, 0, b"\x00\x00\x00\x00\x09payload")]])
    consumer._deserializer.bug = RuntimeError("boom")
    count, _path = await consumer.consume_batch()
    assert count == 0 and consumer.dead_letter_count == 1


async def test_unexplained_error_while_registry_answers_5xx_propagates(make_consumer, registry):
    consumer, _kafka = await make_consumer([[msg(_record(0), offset=0)]])
    consumer._deserializer.bug = RuntimeError("boom")
    registry.error = SchemaRegistryError(503, 50003, "unavailable")
    with pytest.raises(SchemaRegistryError):
        await consumer.consume_batch()
    assert consumer.dead_letter_count == 0


async def test_unexplained_error_without_a_registry_propagates(make_consumer):
    consumer, _kafka = await make_consumer([[msg(_record(0), offset=0)]])
    consumer._registry = None
    consumer._deserializer.bug = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        await consumer.consume_batch()


async def test_schema_with_references_is_not_judged_undecodable(make_consumer, registry):
    consumer, _kafka = await make_consumer([[Msg(TOPIC, 0, 0, avro_value(_record(0))[:20])]])
    registry.schemas[1] = types.SimpleNamespace(schema_str=registry.schemas[1].schema_str,
                                                references=[{"name": "Other"}])
    with pytest.raises(EOFError):
        await consumer.consume_batch()


async def test_non_bytes_value_errors_propagate(make_consumer):
    consumer, _kafka = await make_consumer([[Msg(TOPIC, 0, 0, {"not": "bytes"})]])
    with pytest.raises(SerializationError):
        await consumer.consume_batch()
    assert consumer.dead_letter_count == 0


async def test_slow_decoding_does_not_starve_the_event_loop(make_consumer):
    """aiokafka heartbeats run on the event loop; decoding that blocks it for
    longer than session.timeout.ms (a registry that retries for seconds) got
    the consumer evicted and the same messages re-delivered, forever."""
    consumer, _kafka = await make_consumer([[msg(_record(i), offset=i) for i in range(3)]])
    decode = consumer._deserializer

    def slow(data, ctx):
        time.sleep(0.1)  # a blocking HTTP call to the Schema Registry
        return decode(data, ctx)

    consumer._deserializer = slow
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        count, _path = await consumer.consume_batch()
    finally:
        beat.cancel()
    assert count == 3
    assert ticks >= 10  # the loop kept running during the 0.3 s of decoding


async def test_batch_of_only_dead_letters_is_not_an_empty_poll(make_consumer):
    batches = [[Msg(TOPIC, 0, 0, b"junk")], [msg(_record(1), offset=1)]]
    consumer, _kafka = await make_consumer(batches)

    manifest = await consumer.drain(max_empty_polls=1, idle_sleep=0)

    assert manifest.records == 1  # polling went on after the all-bad batch
    assert manifest.dead_letter_count == 1
    assert len(manifest.dead_letter_files) == 1
    assert manifest.offsets == {0: 2}


async def test_mixed_iso8601_shapes_are_staged_as_sent(make_consumer):
    """Parsing dates at extract failed the whole batch on mixed shapes (pandas
    infers one format per column); staging keeps the producer's strings."""
    shapes = ["2026-10-09T07:00:00+00:00", "2026-10-09T07:00:00.123456+00:00",
              "2026-10-09T07:00:00Z", "2026-10-09T10:00:00+03:00", "not a date"]
    batch = [msg({**_record(i), "transaction_date": d}, offset=i) for i, d in enumerate(shapes)]
    consumer, _kafka = await make_consumer([batch])

    count, path = await consumer.consume_batch()

    assert count == 5
    assert pq.read_table(path).column("transaction_date").to_pylist() == shapes


def test_staging_writer_rejects_empty_input(tmp_path):
    staging = StagingWriter(tmp_path)
    with pytest.raises(ValueError):
        staging.write_batch([])
    with pytest.raises(ValueError):
        staging.write_dead_letters([])


async def test_stream_batches_yields_staged_paths(make_consumer):
    consumer, _kafka = await make_consumer([[msg(_record(0))]])
    seen = []
    paths = [p async for p in consumer.stream_batches(
        on_batch=lambda c, p: seen.append(c), max_batches=1)]
    assert len(paths) == 1 and seen == [1]
    await consumer.stop()
    assert consumer._consumer is None
