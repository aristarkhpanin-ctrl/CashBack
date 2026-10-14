"""Integration: Kafka consumer → ClickHouse loading pipeline.

The ETL service consumes Avro records from ``transactions.raw``, stages
them as Parquet, validates, and bulk-inserts into ClickHouse via the
``transactions_buffer`` table.

Each step is exercised against:

  * a real Parquet round-trip on disk (StagingWriter)
  * a real ClickHouse (testcontainers) with ``infrastructure/clickhouse/
    migrations`` applied (ClickHouseLoader: projection onto the buffer
    table, buffer flush, idempotent re-load) — the loader reads the target
    schema from ``system.columns``, so a fake client would only test itself
  * the asynchronous consume_batch() path with a stubbed AIOKafkaConsumer
    (offsets are tracked, not committed — the DAG commits them after the
    load, B43)

The Avro decoding of ``transactions.raw`` against a real Kafka + Schema
Registry is covered by ``test_listener_avro_accrual.py``.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import UTC, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

# Use the etl service's `app` package (kafka_consumer, ch_loader live here).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "etl"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

# The first ClickHouse test includes the container start-up (see
# test_seed_demo_data.py: the marker is registered only by pytest-timeout).
_TIMEOUT = [pytest.mark.timeout(300)] if importlib.util.find_spec("pytest_timeout") else []
pytestmark = [pytest.mark.integration, *_TIMEOUT]


@pytest.fixture()
def ch_client(migrated_clickhouse_url):
    import clickhouse_connect
    client = clickhouse_connect.get_client(**migrated_clickhouse_url)
    yield client
    client.close()


def _count_loaded(client, ids: list[str]) -> int:
    return int(client.command(
        "SELECT count() FROM transactions_raw WHERE transaction_id IN %(ids)s",
        parameters={"ids": tuple(ids)},
    ))


def _record(**overrides):
    base = {
        "transaction_id": "tx-001",
        "user_id": "00000000-0000-0000-0000-000000000001",
        "mcc_code": "5411",
        "amount": "1234.56",
        "currency": "RUB",
        "transaction_date": datetime.now(UTC).isoformat(timespec="seconds"),
        "channel": "POS",
        "merchant_id": "MID-1",
        "metadata": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
async def test_staging_writer_round_trips_parquet(tmp_path):
    from app.kafka_consumer import StagingWriter

    staging = StagingWriter(tmp_path)
    out = staging.write_batch([_record(transaction_id=f"tx-{i}") for i in range(20)])
    assert out.exists()
    table = pq.read_table(out)
    assert table.num_rows == 20
    cols = set(table.column_names)
    assert {"transaction_id", "user_id", "mcc_code", "amount"}.issubset(cols)


async def test_staging_writer_rejects_empty_batch(tmp_path):
    from app.kafka_consumer import StagingWriter
    with pytest.raises(ValueError):
        StagingWriter(tmp_path).write_batch([])


async def test_ch_loader_loads_parquet_into_clickhouse(tmp_path, ch_client):
    from app.ch_loader import ClickHouseLoader
    from app.kafka_consumer import StagingWriter

    ids = [f"tx-load-{uuid.uuid4().hex[:8]}-{i}" for i in range(7)]
    path = StagingWriter(tmp_path).write_batch(
        [_record(transaction_id=t, amount=f"{100 + i}.50") for i, t in enumerate(ids)]
    )

    rows = ClickHouseLoader(ch_client).load_parquet(path)
    assert rows == 7
    # The buffer table is flushed by the loader: rows are in the MergeTree.
    assert _count_loaded(ch_client, ids) == 7
    total = ch_client.command(
        "SELECT toString(sum(amount)) FROM transactions_raw "
        "WHERE transaction_id IN %(ids)s", parameters={"ids": tuple(ids)},
    )
    assert Decimal(total) == sum(Decimal(f"{100 + i}.50") for i in range(7))


async def test_ch_loader_reload_is_idempotent(tmp_path, ch_client):
    """A retried load (Kafka redelivery) must not insert the same rows twice."""
    from app.ch_loader import ClickHouseLoader
    from app.kafka_consumer import StagingWriter

    ids = [f"tx-reload-{uuid.uuid4().hex[:8]}-{i}" for i in range(4)]
    path = StagingWriter(tmp_path).write_batch([_record(transaction_id=t) for t in ids])

    loader = ClickHouseLoader(ch_client)
    assert loader.load_parquet(path) == 4
    assert loader.load_parquet(path) == 0
    assert _count_loaded(ch_client, ids) == 4


async def test_ch_loader_skips_empty_parquet(tmp_path):
    import pyarrow as pa
    from app.ch_loader import ClickHouseLoader

    empty = tmp_path / "empty.parquet"
    pq.write_table(pa.table({"x": pa.array([], type=pa.int32())}), empty)

    class _Client:
        def insert_arrow(self, *a, **kw):  # noqa: ARG002
            raise AssertionError("must not be called for empty")

    rows = ClickHouseLoader(_Client()).load_parquet(empty)
    assert rows == 0


async def test_ch_loader_errors_when_path_missing():
    from app.ch_loader import ClickHouseLoader
    class _Client:
        def insert_arrow(self, *a, **kw):  # noqa: ARG002
            raise AssertionError("must not be called")
    with pytest.raises(FileNotFoundError):
        ClickHouseLoader(_Client()).load_parquet("/no/such/path.parquet")


async def test_consumer_stages_batch_and_tracks_offsets(tmp_path):
    """Drive TransactionConsumer.consume_batch() with a stub AIOKafkaConsumer.

    We can't avro-encode without a Schema Registry, so the test patches the
    consumer's deserialiser to return our pre-built dicts directly.
    """
    from aiokafka import TopicPartition
    from app.kafka_consumer import StagingWriter, TransactionConsumer

    class _Msg:
        def __init__(self, value, offset):
            self.value = value
            self.topic = "transactions.raw"
            self.partition = 0
            self.offset = offset

    class _Consumer:
        def __init__(self, batches):
            self._batches = list(batches)
            self.committed = False

        async def start(self):
            return None

        async def stop(self):
            return None

        async def getmany(self, *, timeout_ms, max_records):  # noqa: ARG002
            if not self._batches:
                return {}
            return {"tp": self._batches.pop(0)}

        async def commit(self):
            self.committed = True

    # The Schema Registry client is created lazily — nothing connects here.
    consumer = TransactionConsumer(
        bootstrap_servers="unused:9092",
        schema_registry_url="http://unused:8081",
        topic="transactions.raw",
        staging=StagingWriter(tmp_path),
    )
    consumer._consumer = _Consumer([
        [_Msg(_record(transaction_id=f"tx-{i}"), offset=i) for i in range(3)],
    ])
    consumer._deserializer = lambda value, ctx: value  # bypass Avro for the test

    count, path = await consumer.consume_batch()
    assert count == 3
    assert path is not None and Path(path).exists()
    assert pq.read_table(path).num_rows == 3
    # Offsets are only tracked: committing before the ClickHouse load would
    # lose the batch if the load fails (B43) — the DAG commits them later.
    assert consumer._consumer.committed is False
    assert consumer.pending_offsets() == {TopicPartition("transactions.raw", 0): 3}
