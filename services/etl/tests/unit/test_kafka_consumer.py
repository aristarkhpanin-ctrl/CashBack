"""Unit tests for the Kafka side of the daily ETL (B43 b, d).

* extract stages messages but never commits offsets;
* the offsets it consumed end up in an :class:`ExtractManifest`;
* :func:`commit_offsets` commits exactly those offsets, idempotently;
* undecodable messages are counted, logged and dead-lettered.
"""
from __future__ import annotations

import base64
import json
import re
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
from structlog.testing import capture_logs

from tests.unit.fakes import FakeKafka, Msg, json_deserializer, msg

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
def make_consumer(tmp_path, monkeypatch):
    """TransactionConsumer wired to an in-memory Kafka (JSON instead of Avro)."""

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
        consumer._deserializer = json_deserializer
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
# (d) deserialisation failures: counted, logged, dead-lettered
# ---------------------------------------------------------------------------
async def test_undecodable_messages_are_dead_lettered(make_consumer):
    batch = [
        msg(_record(0), offset=10),
        Msg(TOPIC, 0, 11, b"\x00\x00\x00\x00\x07not-avro", key=b"k1"),
        Msg(TOPIC, 0, 12, None),  # tombstone -> deserialiser returns None
        msg(_record(3), offset=13),
    ]
    consumer, kafka = await make_consumer([batch])

    with capture_logs() as logs:
        count, path = await consumer.consume_batch()

    assert count == 2
    assert pq.read_table(path).column("transaction_id").to_pylist() == ["tx-0", "tx-3"]
    assert consumer.dead_letter_count == 2
    assert consumer.pending_offsets() == {TopicPartition(TOPIC, 0): 14}  # past the bad ones
    assert kafka.commits == []

    [dl_path] = consumer.dead_letter_paths
    assert dl_path.parent == path.parent  # staging dir of the same day
    entries = [json.loads(line) for line in dl_path.read_text().splitlines()]
    assert [(e["partition"], e["offset"]) for e in entries] == [(0, 11), (0, 12)]
    assert base64.b64decode(entries[0]["value"]) == b"\x00\x00\x00\x00\x07not-avro"
    assert base64.b64decode(entries[0]["key"]) == b"k1"
    assert re.match(r"\w+Error: ", entries[0]["error"])  # exception type + message
    assert entries[1]["value"] is None and "got NoneType" in entries[1]["error"]
    failed = [e for e in logs if e["event"] == "deserialise_failed"]
    assert [e["offset"] for e in failed] == [11, 12]


async def test_batch_of_only_dead_letters_is_not_an_empty_poll(make_consumer):
    batches = [[Msg(TOPIC, 0, 0, b"junk")], [msg(_record(1), offset=1)]]
    consumer, _kafka = await make_consumer(batches)

    manifest = await consumer.drain(max_empty_polls=1, idle_sleep=0)

    assert manifest.records == 1  # polling went on after the all-bad batch
    assert manifest.dead_letter_count == 1
    assert len(manifest.dead_letter_files) == 1
    assert manifest.offsets == {0: 2}


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
