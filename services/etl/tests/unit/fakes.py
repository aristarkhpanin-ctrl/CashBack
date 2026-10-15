"""In-memory stand-ins for ClickHouse, Kafka and the Schema Registry used by the ETL unit tests."""
from __future__ import annotations

import io
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import fastavro
import pyarrow as pa
from clickhouse_connect.driver.exceptions import DatabaseError, OperationalError
from confluent_kafka.schema_registry import Schema
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import SerializationError

AVRO_SCHEMA = (Path(__file__).resolve().parents[4] / "infrastructure" / "kafka" / "schemas"
               / "transaction_event.avsc").read_text(encoding="utf-8")
SCHEMA_ID = 1
STRING_SCHEMA_ID = 2  # a registered non-record schema ("string")

# Values ClickHouse cannot convert, as its parsers see them (see ch_loader).
_CH_UUID = re.compile(r"[0-9a-fA-F]{8}-?(?:[0-9a-fA-F]{4}-?){3}[0-9a-fA-F]{12}")
_CH_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _ch_error(code: int, text: str) -> DatabaseError:
    """A server-side error as clickhouse-connect raises it."""
    return DatabaseError(
        f"Received ClickHouse exception, code: {code}, server response: "
        f"Code: {code}. DB::Exception: {text}. (version 24.3.18.7 (official build))"
    )

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
    table does not have is rejected (code 16, NO_SUCH_COLUMN_IN_TABLE), and
    so is a whole INSERT holding a value its column cannot take: a non-UUID
    ``user_id`` (376), an unparseable ``amount`` (6), a ``mcc_code`` longer
    than FixedString(4) (131) or a ``transaction_id`` in ``reject_ids`` (6).
    """

    def __init__(self, columns: list[tuple[str, str, str]] | None = None) -> None:
        self.columns = list(columns if columns is not None else BUFFER_COLUMNS)
        self.buffer: list[dict] = []
        self.raw: list[dict] = []
        self.calls: list[str] = []
        self.inserts: list[pa.Table] = []
        self.existing_queries: list[dict] = []
        self.fail_next_inserts = 0
        self.reject_ids: set[str] = set()

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
            raise OperationalError("Error HTTPConnectionPool(host='clickhouse', port=8123): "
                                   "Max retries exceeded ([Errno 111] Connection refused)")
        assert table == "transactions_buffer"
        insertable = {n for n, _t, kind in self.columns if kind not in ("MATERIALIZED", "ALIAS")}
        unknown = [c for c in arrow_table.column_names if c not in insertable]
        if unknown:
            raise _ch_error(16, f"No such column {unknown[0]} in table "
                                "cashback.transactions_buffer. (NO_SUCH_COLUMN_IN_TABLE)")
        rows = arrow_table.to_pylist()
        for row in rows:  # the whole INSERT fails on the first bad value
            self._check_values(row)
        self.inserts.append(arrow_table)
        self.buffer.extend(rows)

    def _check_values(self, row: dict) -> None:
        if row["transaction_id"] in self.reject_ids:
            raise _ch_error(6, f"Cannot parse input: row {row['transaction_id']}")
        if not _CH_UUID.fullmatch(str(row["user_id"])):
            raise _ch_error(376, f"Cannot parse uuid {row['user_id']}: Cannot parse UUID "
                                 "from String: while converting column `user_id`")
        if not _CH_DECIMAL.fullmatch(str(row["amount"])):
            raise _ch_error(6, f"Cannot parse string '{row['amount']}' as Decimal(15, 2)")
        if len(str(row["mcc_code"]).encode()) > 4:
            raise _ch_error(131, "String too long for type FixedString(4)")

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


def avro_value(record: Any, schema_id: int = SCHEMA_ID) -> bytes:
    """``record`` in the Schema Registry wire format, as AvroSerializer writes it."""
    schema = AVRO_SCHEMA if schema_id == SCHEMA_ID else '"string"'
    buf = io.BytesIO()
    fastavro.schemaless_writer(buf, fastavro.parse_schema(json.loads(schema)), record)
    return struct.pack(">bI", 0, schema_id) + buf.getvalue()


def msg(record: dict, *, partition: int = 0, offset: int = 0,
        topic: str = "transactions.raw") -> Msg:
    return Msg(topic, partition, offset, avro_value(record))


class FakeSchemaRegistry:
    """SchemaRegistryClient.get_schema() over a dict of registered schemas.

    While ``error`` is set every call raises it (registry down, 5xx, ...);
    an unknown id answers like the real registry: 404 / 40403.
    """

    def __init__(self, conf: Optional[dict] = None) -> None:
        self.conf = conf
        self.schemas = {SCHEMA_ID: Schema(AVRO_SCHEMA, "AVRO"),
                        STRING_SCHEMA_ID: Schema('"string"', "AVRO")}
        self.error: Optional[BaseException] = None
        self.calls = 0

    def get_schema(self, schema_id: int, *_args: Any, **_kwargs: Any) -> Schema:
        self.calls += 1
        if self.error is not None:
            raise self.error
        try:
            return self.schemas[schema_id]
        except KeyError:
            raise SchemaRegistryError(404, 40403, f"Schema {schema_id} not found") from None


class FakeAvroDeserializer:
    """AvroDeserializer's decode path (confluent-kafka 2.3, the Airflow image).

    Framing errors raise SerializationError, the writer schema comes from the
    registry (cached per id), the payload is decoded with fastavro. ``bug``
    makes every call raise it, like a deserializer that fails for reasons
    unrelated to the message.
    """

    def __init__(self, registry: FakeSchemaRegistry) -> None:
        self._registry = registry
        self._schemas: dict[int, Any] = {}
        self.bug: Optional[BaseException] = None

    def __call__(self, data: Optional[bytes], _ctx: Any) -> Any:
        if self.bug is not None:
            raise self.bug
        if data is None:
            return None
        if len(data) <= 5:
            raise SerializationError(f"Expecting data framing of length 6 bytes or more "
                                     f"but total data size is {len(data)} bytes")
        magic, schema_id = struct.unpack(">bI", data[:5])
        if magic != 0:
            raise SerializationError(f"Unexpected magic byte {magic}")
        if schema_id not in self._schemas:
            schema = self._registry.get_schema(schema_id)
            self._schemas[schema_id] = fastavro.parse_schema(json.loads(schema.schema_str))
        return fastavro.schemaless_reader(io.BytesIO(data[5:]), self._schemas[schema_id])


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
