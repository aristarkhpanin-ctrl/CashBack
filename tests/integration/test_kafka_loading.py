"""Integration: Kafka consumer → ClickHouse loading pipeline.

The ETL service consumes Avro records from ``transactions.raw``, stages
them as Parquet, validates, and bulk-inserts into ClickHouse via the
``transactions_buffer`` table.

Spinning up Kafka + Schema Registry + ClickHouse in CI is heavy; we
exercise the *logic* of each step against:

  * a real Parquet round-trip on disk (StagingWriter)
  * an in-memory ClickHouse fake (ClickHouseLoader)
  * the asynchronous consume_batch() path with a stubbed AIOKafkaConsumer

This delivers six tests that cover the same code paths the production
deployment hits when wired through real Kafka.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow.parquet as pq
import pytest

# Use the etl service's `app` package (kafka_consumer, ch_loader live here).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "etl"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration


def _record(**overrides):
    base = {
        "transaction_id": "tx-001",
        "user_id": "00000000-0000-0000-0000-000000000001",
        "mcc_code": "5411",
        "amount": "1234.56",
        "currency": "RUB",
        "transaction_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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


async def test_ch_loader_reads_parquet_and_calls_insert_arrow(tmp_path):
    from app.ch_loader import ClickHouseLoader
    from app.kafka_consumer import StagingWriter

    staging = StagingWriter(tmp_path)
    path = staging.write_batch([_record(transaction_id=f"tx-{i}") for i in range(7)])

    captured: dict[str, Any] = {}
    class _Client:
        def insert_arrow(self, table, arrow):
            captured["table"] = table
            captured["rows"] = arrow.num_rows

    rows = ClickHouseLoader(_Client()).load_parquet(path)
    assert rows == 7
    assert captured["rows"] == 7
    assert captured["table"] == "transactions_buffer"


async def test_ch_loader_skips_empty_parquet(tmp_path):
    from app.ch_loader import ClickHouseLoader
    import pyarrow as pa

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


async def test_consumer_drains_batch_and_commits(tmp_path):
    """Drive TransactionConsumer.consume_batch() with a stub AIOKafkaConsumer.

    We can't avro-encode without a Schema Registry, so the test patches the
    consumer's deserialiser to return our pre-built dicts directly.
    """
    from app.kafka_consumer import StagingWriter, TransactionConsumer

    class _Msg:
        def __init__(self, value): self.value = value

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

    staging = StagingWriter(tmp_path)
    consumer = TransactionConsumer.__new__(TransactionConsumer)
    consumer._settings = SimpleNamespace(
        transactions_topic="transactions.raw",
        kafka_bootstrap_servers="localhost:9092",
        schema_registry_url="http://localhost:8081",
        consumer_group_accrual="cashback-accrual-group",
    )
    consumer._staging = staging
    consumer._stop = asyncio.Event()
    consumer._topic = "transactions.raw"
    consumer._group_id = "cashback-accrual-group"
    consumer._consumer = _Consumer([
        [_Msg(_record(transaction_id=f"tx-{i}")) for i in range(3)],
    ])
    consumer._deserializer = lambda value, ctx: value  # bypass Avro for the test

    count, path = await consumer.consume_batch()
    assert count == 3
    assert path is not None and Path(path).exists()
    assert consumer._consumer.committed is True
