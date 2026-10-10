"""In-memory stand-ins for ClickHouse and Kafka used by the ETL unit tests."""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Optional

import pyarrow as pa

# system.columns of ``transactions_buffer`` as created by migration 003.
BUFFER_COLUMNS: list[tuple[str, str, str]] = [
    ("transaction_id", "String", ""),
    ("user_id", "UUID", ""),
    ("transaction_date", "DateTime", ""),
    ("amount", "Decimal(15, 2)", ""),
    ("currency", "LowCardinality(String)", ""),
    ("mcc_code", "FixedString(4)", ""),
    ("merchant_id", "String", ""),
    ("merchant_name", "String", ""),
    ("channel", "LowCardinality(String)", ""),
    ("city", "LowCardinality(String)", ""),
    ("country", "LowCardinality(String)", ""),
    ("inserted_at", "DateTime", "DEFAULT"),
]


class FakeClickHouse:
    """Buffer table in front of a MergeTree, in memory.

    Inserts land in ``buffer`` and reach ``raw`` (``transactions_raw``) only
    on ``OPTIMIZE TABLE transactions_buffer`` — like the real Buffer engine,
    which flushes lazily. Like ClickHouse, an INSERT naming a column the
    table does not have is rejected (code 16, NO_SUCH_COLUMN_IN_TABLE).
    """

    def __init__(self, columns: list[tuple[str, str, str]] | None = None) -> None:
        self.columns = list(columns if columns is not None else BUFFER_COLUMNS)
        self.buffer: list[dict] = []
        self.raw: list[dict] = []
        self.calls: list[str] = []
        self.inserts: list[pa.Table] = []
        self.existing_queries: list[dict] = []
        self.fail_next_inserts = 0

    # -- clickhouse_connect.driver.Client surface -------------------------
    def query(self, sql: str, parameters: Optional[dict] = None):
        if "system.columns" in sql:
            self.calls.append("columns")
            assert parameters == {"table": "transactions_buffer"}
            return SimpleNamespace(result_rows=list(self.columns))
        if "FROM transactions_raw" in sql and parameters and "ids" in parameters:
            self.calls.append("existing")
            self.existing_queries.append(parameters)
            ids, lo, hi = set(parameters["ids"]), parameters["lo"], parameters["hi"]
            found = {
                r["transaction_id"] for r in self.raw
                if r["transaction_id"] in ids and lo <= r["transaction_date"].date() <= hi
            }
            return SimpleNamespace(result_rows=[(tid,) for tid in sorted(found)])
        if "FROM transactions_raw" in sql and "inserted_at" in sql:
            # DataValidator R5 (recent ids).
            return SimpleNamespace(result_rows=[(r["transaction_id"],) for r in self.raw])
        raise AssertionError(f"unexpected query: {sql}")

    def insert_arrow(self, table: str, arrow_table: pa.Table, *a: Any, **kw: Any) -> None:
        self.calls.append("insert")
        if self.fail_next_inserts:
            self.fail_next_inserts -= 1
            raise RuntimeError("Code: 210. Connection refused")
        assert table == "transactions_buffer"
        insertable = {n for n, _t, kind in self.columns if kind not in ("MATERIALIZED", "ALIAS")}
        unknown = [c for c in arrow_table.column_names if c not in insertable]
        if unknown:
            raise RuntimeError(
                f"Code: 16. DB::Exception: No such column {unknown[0]} in table "
                "cashback.transactions_buffer. (NO_SUCH_COLUMN_IN_TABLE)"
            )
        self.inserts.append(arrow_table)
        self.buffer.extend(arrow_table.to_pylist())

    def command(self, sql: str) -> None:
        assert sql == "OPTIMIZE TABLE transactions_buffer", sql
        self.calls.append("flush")
        self.raw.extend(self.buffer)
        self.buffer.clear()

    # -- helpers ------------------------------------------------------------
    def raw_ids(self) -> list[str]:
        return [r["transaction_id"] for r in self.raw]


@dataclass
class Msg:
    """Subset of aiokafka.ConsumerRecord the ETL consumer touches."""

    topic: str
    partition: int
    offset: int
    value: Optional[bytes]
    key: Optional[bytes] = None
    timestamp: int = 1_760_000_000_000


def json_deserializer(value: Optional[bytes], _ctx: Any) -> Any:
    """Stand-in for AvroDeserializer: JSON instead of Avro, same None handling."""
    if value is None:
        return None
    return json.loads(value)


def msg(record: dict, *, partition: int = 0, offset: int = 0,
        topic: str = "transactions.raw") -> Msg:
    return Msg(topic, partition, offset, json.dumps(record).encode())


class FakeKafka:
    """One topic + one consumer group, in memory.

    Every AIOKafkaConsumer created through :meth:`factory` serves
    ``batches`` (lists of :class:`Msg`) from getmany() and commits into
    ``group_committed`` instead of a broker. ``commits`` lists every
    commit() call — ``None`` for the argument-less "commit all consumed".
    """

    def __init__(self, batches: list[list[Msg]] | None = None, *,
                 committed: dict | None = None, fail_commit: bool = False) -> None:
        self.batches = list(batches or [])
        self.group_committed: dict = dict(committed or {})
        self.fail_commit = fail_commit
        self.consumers: list[FakeConsumer] = []
        self.commits: list[Any] = []

    def factory(self, *topics: str, **kwargs: Any) -> FakeConsumer:
        consumer = FakeConsumer(self, topics, kwargs)
        self.consumers.append(consumer)
        return consumer


class FakeConsumer:
    def __init__(self, kafka: FakeKafka, topics: tuple[str, ...], kwargs: dict) -> None:
        self._kafka = kafka
        self.topics = topics
        self.kwargs = kwargs
        self.assigned: list = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def getmany(self, *, timeout_ms: int, max_records: int) -> dict:
        if not self._kafka.batches:
            return {}
        batch = self._kafka.batches.pop(0)
        by_tp: dict[tuple[str, int], list[Msg]] = {}
        for m in batch:
            by_tp.setdefault((m.topic, m.partition), []).append(m)
        return by_tp

    def assign(self, partitions: list) -> None:
        self.assigned = list(partitions)

    async def committed(self, tp: Any) -> Optional[int]:
        assert tp in self.assigned, "committed() needs the partition assigned"
        return self._kafka.group_committed.get(tp)

    async def commit(self, offsets: Any = None) -> None:
        if offsets is not None:
            assert all(tp in self.assigned for tp in offsets), "commit() needs partitions assigned"
        if self._kafka.fail_commit:
            raise RuntimeError("CommitFailedError")
        self._kafka.commits.append(None if offsets is None else dict(offsets))
        if offsets is not None:
            self._kafka.group_committed.update(offsets)
