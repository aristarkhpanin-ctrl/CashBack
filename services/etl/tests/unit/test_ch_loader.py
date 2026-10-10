"""Unit tests for ClickHouseLoader (B43): projection onto the table schema
and idempotent loading through the Buffer table.

Staged files are produced exactly like the DAG does (StagingWriter, then
the validate step's pandas rewrite), so they carry the Avro ``metadata``
field and the type drift of a real run (``large_string``, all-null columns).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from app.ch_loader import ClickHouseLoader, SchemaMismatchError
from app.kafka_consumer import StagingWriter
from structlog.testing import capture_logs

from tests.unit.fakes import BUFFER_COLUMNS, FakeClickHouse

_NOW = datetime.now(UTC).replace(microsecond=0)


def _record(**overrides) -> dict:
    base = {
        "transaction_id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "mcc_code": "5411",
        "amount": "1234.56",
        "currency": "RUB",
        "transaction_date": (_NOW - timedelta(hours=1)).isoformat(),
        "channel": "POS",
        "merchant_id": None,
        "metadata": '{"device": "ios"}',
    }
    base.update(overrides)
    return base


def _stage_like_dag(tmp_path: Path, records: list[dict]) -> Path:
    """StagingWriter + validate_and_cleanse's rewrite — the DAG's file path."""
    staged = StagingWriter(tmp_path).write_batch(records)
    clean = Path(str(staged).replace(".parquet", ".clean.parquet"))
    pd.DataFrame.from_records(pq.read_table(staged).to_pylist()).to_parquet(clean, index=False)
    return clean


def _write(tmp_path: Path, table: pa.Table, name: str = "batch.parquet") -> Path:
    path = tmp_path / name
    pq.write_table(table, path)
    return path


# ---------------------------------------------------------------------------
# (a) projection onto the target table
# ---------------------------------------------------------------------------
def test_avro_metadata_field_is_dropped_and_rows_land(tmp_path):
    records = [_record() for _ in range(5)]
    path = _stage_like_dag(tmp_path, records)
    assert "metadata" in pq.read_table(path).column_names  # the bug's trigger

    ch = FakeClickHouse()
    with capture_logs() as logs:
        rows = ClickHouseLoader(ch).load_paths([path])

    assert rows == 5
    assert sorted(ch.raw_ids()) == sorted(r["transaction_id"] for r in records)
    inserted = ch.inserts[0]
    table_cols = [name for name, _t, _k in BUFFER_COLUMNS]
    assert inserted.column_names == [c for c in table_cols if c in inserted.column_names]
    assert "metadata" not in inserted.column_names
    dropped = [e for e in logs if e["event"] == "dropping_extra_columns"]
    assert dropped and dropped[0]["columns"] == ["metadata"]


def test_types_are_normalised_for_clickhouse(tmp_path):
    path = _stage_like_dag(tmp_path, [_record(), _record()])
    src = pq.read_table(path).schema
    assert src.field("merchant_id").type == pa.null()  # all-None column from pandas

    ch = FakeClickHouse()
    ClickHouseLoader(ch).load_paths([path])
    schema = ch.inserts[0].schema
    for name in ("transaction_id", "user_id", "mcc_code", "currency", "channel", "merchant_id"):
        assert schema.field(name).type == pa.string(), name
    assert schema.field("transaction_date").type.tz == "UTC"
    assert schema.field("amount").type == src.field("amount").type  # Decimal parsed by ClickHouse


def test_string_and_naive_timestamps_are_read_as_utc(tmp_path):
    base = [_record(transaction_id=f"tx-{i}") for i in range(3)]
    columns = {k: [r[k] for r in base] for k in base[0] if k != "transaction_date"}
    as_strings = {**columns, "transaction_date": [
        "2026-10-09T10:00:00+03:00", "2026-10-09T07:00:00Z", "2026-10-09T07:00:00+00:00",
    ]}
    naive_strings = {**columns, "transaction_date": ["2026-10-09T07:00:00"] * 3}
    naive_ts = {**columns, "transaction_date": pa.array(
        [datetime(2026, 10, 9, 7, 0)] * 3, type=pa.timestamp("us"))}
    expected = [datetime(2026, 10, 9, 7, 0, tzinfo=UTC)] * 3

    for i, cols in enumerate((as_strings, naive_strings, naive_ts)):
        ch = FakeClickHouse()
        ClickHouseLoader(ch).load_parquet(_write(tmp_path, pa.table(cols), f"f{i}.parquet"))
        got = ch.inserts[0].column("transaction_date")
        assert got.type == pa.timestamp(got.type.unit, tz="UTC")
        assert [v.astimezone(UTC) for v in got.to_pylist()] == expected


def test_unparseable_timestamp_is_a_clear_error(tmp_path):
    rec = _record(transaction_date="yesterday")
    table = pa.table({k: [v] for k, v in rec.items()})
    with pytest.raises(SchemaMismatchError, match="transaction_date"):
        ClickHouseLoader(FakeClickHouse()).load_parquet(_write(tmp_path, table))


def test_non_temporal_column_for_datetime_is_a_clear_error(tmp_path):
    rec = _record()
    cols = {k: [v] for k, v in rec.items()}
    cols["transaction_date"] = pa.array([1.5])
    with pytest.raises(SchemaMismatchError, match="cannot be loaded as DateTime"):
        ClickHouseLoader(FakeClickHouse()).load_parquet(_write(tmp_path, pa.table(cols)))


def test_null_and_date_columns_cast_to_timestamp(tmp_path):
    cols = {k: [v] for k, v in _record().items() if k != "transaction_date"}
    ch = FakeClickHouse(columns=BUFFER_COLUMNS)
    loader = ClickHouseLoader(ch)
    cols["transaction_date"] = pa.array([datetime(2026, 10, 9).date()], type=pa.date32())
    loader.load_parquet(_write(tmp_path, pa.table(cols), "d.parquet"))
    assert ch.inserts[0].column("transaction_date").to_pylist() == [
        datetime(2026, 10, 9, tzinfo=UTC)]
    # all-null dates cannot be de-duplicated by date window -> explicit error
    cols["transaction_date"] = pa.array([None], type=pa.null())
    with pytest.raises(SchemaMismatchError, match="transaction_date is empty"):
        loader.load_parquet(_write(tmp_path, pa.table(cols), "n.parquet"))


def test_missing_required_column_is_a_clear_error(tmp_path):
    table = pq.read_table(_stage_like_dag(tmp_path, [_record()])).drop_columns(["amount"])
    ch = FakeClickHouse()
    with pytest.raises(SchemaMismatchError, match=r"required columns missing: \['amount'\]"):
        ClickHouseLoader(ch).load_parquet(_write(tmp_path, table))
    assert ch.inserts == []


def test_table_without_required_column_is_a_clear_error(tmp_path):
    columns = [c for c in BUFFER_COLUMNS if c[0] != "channel"]
    with pytest.raises(SchemaMismatchError, match=r"lacks required columns \['channel'\]"):
        ClickHouseLoader(FakeClickHouse(columns)).load_parquet(
            _stage_like_dag(tmp_path, [_record()]))


def test_materialized_and_alias_columns_are_not_inserted(tmp_path):
    columns = BUFFER_COLUMNS + [("is_weekend", "UInt8", "MATERIALIZED"),
                                ("hour_alias", "UInt8", "ALIAS")]
    rec = _record()
    table = pa.table({**{k: [v] for k, v in rec.items()}, "is_weekend": [1], "hour_alias": [3]})
    ch = FakeClickHouse(columns)
    assert ClickHouseLoader(ch).load_parquet(_write(tmp_path, table)) == 1
    assert {"is_weekend", "hour_alias", "metadata"}.isdisjoint(ch.inserts[0].column_names)


def test_schema_is_queried_once_per_loader(tmp_path):
    ch = FakeClickHouse()
    loader = ClickHouseLoader(ch)
    loader.load_paths([
        _stage_like_dag(tmp_path / "a", [_record()]),
        _stage_like_dag(tmp_path / "b", [_record()]),
    ])
    assert ch.calls.count("columns") == 1


# ---------------------------------------------------------------------------
# (c) retries must not duplicate rows (transactions_raw is a plain MergeTree)
# ---------------------------------------------------------------------------
def test_reloading_the_same_file_inserts_nothing(tmp_path):
    path = _stage_like_dag(tmp_path, [_record() for _ in range(4)])
    ch = FakeClickHouse()
    assert ClickHouseLoader(ch).load_paths([path]) == 4
    # a new task try = a new loader instance
    assert ClickHouseLoader(ch).load_paths([path]) == 0
    assert len(ch.raw) == 4
    assert len(ch.inserts) == 1


def test_retry_after_failed_insert_loads_each_row_once(tmp_path):
    first = [_record() for _ in range(3)]
    second = [_record() for _ in range(2)]
    paths = [_stage_like_dag(tmp_path / "a", first), _stage_like_dag(tmp_path / "b", second)]
    ch = FakeClickHouse()

    loader = ClickHouseLoader(ch)
    loader.load_parquet(paths[0])
    ch.fail_next_inserts = 1
    with pytest.raises(RuntimeError, match="Connection refused"):
        loader.load_parquet(paths[1])

    assert ClickHouseLoader(ch).load_paths(paths) == 2  # Airflow retry of the task
    assert sorted(ch.raw_ids()) == sorted(r["transaction_id"] for r in first + second)


def test_rows_left_in_buffer_by_a_crashed_attempt_are_not_reinserted(tmp_path):
    path = _stage_like_dag(tmp_path, [_record() for _ in range(3)])
    ch = FakeClickHouse()
    # A previous attempt inserted into the buffer and died before the flush.
    ch.buffer.extend(ClickHouseLoader(ch)._project(pq.read_table(path), path).to_pylist())

    assert ClickHouseLoader(ch).load_paths([path]) == 0
    assert len(ch.raw) == 3


def test_partially_loaded_file_inserts_only_the_new_rows(tmp_path):
    old = [_record() for _ in range(2)]
    new = [_record() for _ in range(3)]
    ch = FakeClickHouse()
    ClickHouseLoader(ch).load_paths([_stage_like_dag(tmp_path / "a", old)])
    # Kafka redelivery: the next run stages old + new messages together.
    with capture_logs() as logs:
        rows = ClickHouseLoader(ch).load_paths([_stage_like_dag(tmp_path / "b", old + new)])
    assert rows == 3
    assert sorted(ch.raw_ids()) == sorted(r["transaction_id"] for r in old + new)
    assert {"event": "skip_already_loaded", "rows": 2} in [
        {k: e[k] for k in ("event", "rows")} for e in logs if e["event"] == "skip_already_loaded"]


def test_flush_brackets_the_insert(tmp_path):
    ch = FakeClickHouse()
    ClickHouseLoader(ch).load_paths([_stage_like_dag(tmp_path, [_record()])])
    # flush -> existence check -> insert -> flush (rows durable before offsets commit)
    assert ch.calls == ["columns", "flush", "existing", "insert", "flush"]
    assert ch.buffer == []


def test_existence_check_is_chunked_and_date_bounded(tmp_path):
    dates = [_NOW - timedelta(days=3), _NOW - timedelta(hours=2)]
    records = [_record(transaction_date=dates[i % 2].isoformat()) for i in range(5)]
    ch = FakeClickHouse()
    loader = ClickHouseLoader(ch)
    loader.ID_CHUNK = 2
    loader.load_paths([_stage_like_dag(tmp_path, records)])

    assert [len(q["ids"]) for q in ch.existing_queries] == [2, 2, 1]
    assert {q["lo"] for q in ch.existing_queries} == {(dates[0] - timedelta(days=1)).date()}
    assert {q["hi"] for q in ch.existing_queries} == {(dates[1] + timedelta(days=1)).date()}


# ---------------------------------------------------------------------------
# unchanged edge cases
# ---------------------------------------------------------------------------
def test_empty_parquet_is_skipped(tmp_path):
    ch = FakeClickHouse()
    path = _write(tmp_path, pa.table({"x": pa.array([], type=pa.int32())}))
    assert ClickHouseLoader(ch).load_parquet(path) == 0
    assert ch.calls == []


def test_missing_path_raises():
    with pytest.raises(FileNotFoundError):
        ClickHouseLoader(FakeClickHouse()).load_parquet("/no/such/path.parquet")


def test_custom_buffer_table_name():
    assert ClickHouseLoader(FakeClickHouse(), buffer_table="tx_buf").BUFFER_TABLE == "tx_buf"
