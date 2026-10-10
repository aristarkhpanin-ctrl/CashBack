"""B43 against real infrastructure: ClickHouse, Kafka (KRaft) and Schema Registry.

Opt-in (excluded from the unit gate by ``-m "not integration"``); point the
ETL_IT_* variables at throw-away containers, e.g.::

    ETL_IT_CLICKHOUSE_PORT=18450 \\
    ETL_IT_KAFKA_BOOTSTRAP=localhost:18452 \\
    ETL_IT_SCHEMA_REGISTRY_URL=http://localhost:18451 \\
    python -m pytest tests/integration -m integration

Every test gets its own ClickHouse database (with
``infrastructure/clickhouse/migrations`` applied) and its own Kafka topic /
consumer group, so runs do not interfere with each other.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
from aiokafka import AIOKafkaConsumer, TopicPartition
from app import feature_eng
from app import settings as settings_mod
from app.ch_loader import ClickHouseLoader, SchemaMismatchError
from app.kafka_consumer import ExtractManifest, StagingWriter

from tests.dag_runner import load_dag, run

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[4]
MIGRATIONS = REPO / "infrastructure" / "clickhouse" / "migrations"
AVRO_SCHEMA = REPO / "infrastructure" / "kafka" / "schemas" / "transaction_event.avsc"

CH_HOST = os.getenv("ETL_IT_CLICKHOUSE_HOST", "127.0.0.1")
CH_PORT = os.getenv("ETL_IT_CLICKHOUSE_PORT")
CH_USER = os.getenv("ETL_IT_CLICKHOUSE_USER", "cashback")
CH_PASSWORD = os.getenv("ETL_IT_CLICKHOUSE_PASSWORD", "cashback")
KAFKA = os.getenv("ETL_IT_KAFKA_BOOTSTRAP")
SCHEMA_REGISTRY = os.getenv("ETL_IT_SCHEMA_REGISTRY_URL")

needs_clickhouse = pytest.mark.skipif(not CH_PORT, reason="ETL_IT_CLICKHOUSE_PORT is not set")
needs_kafka = pytest.mark.skipif(
    not (KAFKA and SCHEMA_REGISTRY),
    reason="ETL_IT_KAFKA_BOOTSTRAP / ETL_IT_SCHEMA_REGISTRY_URL are not set",
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def _apply_migration(client, path: Path) -> None:
    for stmt in _statements(path.read_text(encoding="utf-8")):
        client.command(stmt)


@pytest.fixture
def ch():
    import clickhouse_connect

    admin = clickhouse_connect.get_client(
        host=CH_HOST, port=int(CH_PORT), username=CH_USER, password=CH_PASSWORD)
    db = f"etl_it_{uuid.uuid4().hex[:10]}"
    admin.command(f"CREATE DATABASE {db}")
    client = clickhouse_connect.get_client(
        host=CH_HOST, port=int(CH_PORT), username=CH_USER, password=CH_PASSWORD, database=db)
    try:
        for path in sorted(MIGRATIONS.glob("*.sql")):
            _apply_migration(client, path)
        yield client
    finally:
        admin.command(f"DROP DATABASE IF EXISTS {db}")


def _record(i: int, **overrides) -> dict:
    base = {
        "transaction_id": f"tx-{uuid.uuid4()}",
        "user_id": str(uuid.uuid4()),
        "mcc_code": "5411",
        "amount": f"{100 + i}.25",
        "currency": "RUB",
        "transaction_date": (datetime.now(UTC) - timedelta(hours=1, minutes=i)).isoformat(),
        "channel": "POS",
        "merchant_id": None if i % 2 else f"MID-{i}",
        "metadata": '{"device": "android"}',
    }
    base.update(overrides)
    return base


def _stage_like_dag(tmp_path: Path, records: list[dict]) -> Path:
    staged = StagingWriter(tmp_path).write_batch(records)
    clean = Path(str(staged).replace(".parquet", ".clean.parquet"))
    pd.DataFrame.from_records(pq.read_table(staged).to_pylist()).to_parquet(clean, index=False)
    return clean


def _count(client, table: str = "transactions_raw") -> tuple[int, int]:
    rows, ids = client.query(f"SELECT count(), uniqExact(transaction_id) FROM {table}").result_rows[0]
    return rows, ids


# ---------------------------------------------------------------------------
# (a) + (c): projection, flush and idempotent reload on real ClickHouse
# ---------------------------------------------------------------------------
@needs_clickhouse
def test_loader_lands_avro_records_and_reload_does_not_duplicate(ch, tmp_path):
    records = [_record(i) for i in range(50)]
    path = _stage_like_dag(tmp_path / "a", records)
    assert "metadata" in pq.read_table(path).column_names

    assert ClickHouseLoader(ch).load_paths([path]) == 50
    # No waiting for the Buffer engine's 10-60 s timer: the loader flushed.
    assert _count(ch) == (50, 50)

    first = records[0]
    row = ch.query(
        "SELECT toString(user_id), amount, merchant_id, merchant_name, transaction_date, "
        "is_weekend, inserted_at FROM transactions_raw WHERE transaction_id = %(tid)s",
        parameters={"tid": first["transaction_id"]},
    ).result_rows[0]
    assert row[0] == first["user_id"]
    assert row[1] == Decimal(first["amount"])
    assert row[2] == first["merchant_id"] and row[3] == ""  # DEFAULT for absent columns
    expected_ts = datetime.fromisoformat(first["transaction_date"]).replace(microsecond=0)
    assert row[4].replace(tzinfo=UTC) == expected_ts
    assert row[6] is not None

    # Retried load (new Airflow try) of the same file: nothing new.
    assert ClickHouseLoader(ch).load_paths([path]) == 0
    assert _count(ch) == (50, 50)
    assert _count(ch, "transactions_buffer") == (50, 50)  # buffer + destination

    # Kafka redelivery: 10 already-loaded records + 5 new ones in one file.
    new = [_record(100 + i) for i in range(5)]
    overlap = _stage_like_dag(tmp_path / "b", records[:10] + new)
    assert ClickHouseLoader(ch).load_paths([overlap]) == 5
    assert _count(ch) == (55, 55)


@needs_clickhouse
def test_loader_rejects_file_without_required_column(ch, tmp_path):
    path = _stage_like_dag(tmp_path, [_record(0)])
    table = pq.read_table(path).drop_columns(["amount"])
    pq.write_table(table, path)
    with pytest.raises(SchemaMismatchError, match="amount"):
        ClickHouseLoader(ch).load_paths([path])
    assert _count(ch) == (0, 0)


# ---------------------------------------------------------------------------
# (b) + (d): the DAG's task code against real Kafka / Schema Registry / ClickHouse
# ---------------------------------------------------------------------------
async def _create_topic(topic: str, partitions: int) -> None:
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    admin = AIOKafkaAdminClient(bootstrap_servers=KAFKA)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(topic, num_partitions=partitions,
                                            replication_factor=1)])
    finally:
        await admin.close()


def _produce(topic: str, records: list[dict], poison: list[tuple[int, bytes]]) -> None:
    from confluent_kafka import Producer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import MessageField, SerializationContext

    serializer = AvroSerializer(SchemaRegistryClient({"url": SCHEMA_REGISTRY}),
                                AVRO_SCHEMA.read_text(encoding="utf-8"))
    producer = Producer({"bootstrap.servers": KAFKA})
    ctx = SerializationContext(topic, MessageField.VALUE)
    for i, rec in enumerate(records):
        producer.produce(topic, value=serializer(rec, ctx), partition=i % 2)
    for partition, raw in poison:
        producer.produce(topic, value=raw, partition=partition)
    assert producer.flush(30) == 0


async def _group_offsets(group: str, topic: str) -> tuple[dict, dict]:
    """(committed, end) offsets per partition, read through the broker."""
    consumer = AIOKafkaConsumer(bootstrap_servers=KAFKA, group_id=group,
                                enable_auto_commit=False)
    await consumer.start()
    try:
        tps = [TopicPartition(topic, p) for p in (0, 1)]
        consumer.assign(tps)
        committed = {tp.partition: await consumer.committed(tp) for tp in tps}
        end = {tp.partition: o for tp, o in (await consumer.end_offsets(tps)).items()}
        return committed, end
    finally:
        await consumer.stop()


@needs_clickhouse
@needs_kafka
def test_daily_dag_commits_offsets_only_after_successful_load(ch, tmp_path, monkeypatch):
    topic = f"it.transactions.{uuid.uuid4().hex[:8]}"
    group = f"it-etl-{uuid.uuid4().hex[:8]}"
    asyncio.run(_create_topic(topic, partitions=2))
    records = [_record(i) for i in range(30)]
    _produce(topic, records, poison=[(0, b"\x00\x00\x00\x00\x2anot avro at all")])

    cfg = settings_mod.Settings(
        kafka_bootstrap_servers=KAFKA, schema_registry_url=SCHEMA_REGISTRY,
        kafka_topic_transactions=topic, kafka_consumer_group=group,
        clickhouse_host=CH_HOST, clickhouse_http_port=int(CH_PORT),
        clickhouse_user=CH_USER, clickhouse_password=CH_PASSWORD,
        clickhouse_db=ch.database, staging_dir=str(tmp_path),
    )
    monkeypatch.setattr(settings_mod, "get_settings", lambda: cfg)
    # RFM recomputation is not part of B43 and needs Redis; it runs on a
    # separate branch of the DAG and does not gate the offset commit.
    monkeypatch.setattr(feature_eng.RFMComputer, "run", lambda self, window_days: 0)
    nodes = load_dag(monkeypatch)
    stubs = {"check_kafka_lag": {"lag_per_partition": {}, "topic": topic}}

    # Run 1 — the load fails for real (the buffer table is gone).
    ch.command("DROP TABLE transactions_buffer")
    results, failed = run(nodes, stubs)
    assert isinstance(failed.get("load_to_clickhouse"), SchemaMismatchError)
    assert "commit_kafka_offsets" in failed and failed["commit_kafka_offsets"] is None
    first = ExtractManifest.read(results["extract_transactions"])
    committed, end = asyncio.run(_group_offsets(group, topic))
    assert end == {0: 16, 1: 15}  # 15 + poison on p0, 15 on p1
    assert first.records == 30 and first.offsets == end
    assert first.dead_letter_count == 1 and Path(first.dead_letter_files[0]).exists()
    assert committed == {0: None, 1: None}  # extract staged but did not commit

    # Run 2 — ClickHouse is back: the same messages are re-read and loaded once.
    _apply_migration(ch, MIGRATIONS / "003_create_buffer_table.sql")
    results, failed = run(nodes, stubs)
    assert failed == {}
    second = ExtractManifest.read(results["extract_transactions"])
    assert second.records == 30  # nothing was lost by run 1
    assert results["load_to_clickhouse"] == 30
    assert _count(ch) == (30, 30)
    committed, end = asyncio.run(_group_offsets(group, topic))
    assert committed == end == second.offsets
    assert results["commit_kafka_offsets"] == {"0": 16, "1": 15}

    # Re-running the commit task (Airflow retry) is a no-op and never rewinds.
    commit = nodes["commit_kafka_offsets"].fn
    assert commit(results["extract_transactions"], 30) == {}
    assert commit(_stale(first, tmp_path), 0) == {}
    assert asyncio.run(_group_offsets(group, topic))[0] == end

    # Run 3 — topic drained: nothing staged, nothing loaded, offsets unchanged.
    results, failed = run(nodes, stubs)
    assert failed == {}
    third = ExtractManifest.read(results["extract_transactions"])
    assert third.records == 0 and third.files == [] and third.offsets == {}
    assert _count(ch) == (30, 30)
    assert asyncio.run(_group_offsets(group, topic))[0] == end


def _stale(manifest: ExtractManifest, tmp_path: Path) -> str:
    """A manifest from an older run (lower offsets) — committing it must not rewind."""
    stale = ExtractManifest(topic=manifest.topic, group_id=manifest.group_id,
                            offsets={p: 1 for p in manifest.offsets})
    return str(StagingWriter(tmp_path / "stale").write_manifest(stale))
