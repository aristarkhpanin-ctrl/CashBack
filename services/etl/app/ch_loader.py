"""ClickHouse loader — pumps staged Parquet files into the buffer table."""
from __future__ import annotations

import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import structlog

from app.validator import REQUIRED_FIELDS

log = structlog.get_logger("etl.ch_loader")

# Wrappers that do not change how a value travels over Arrow.
_CH_WRAPPER = re.compile(r"^(?:Nullable|LowCardinality)\((.*)\)$")
# ClickHouse types fed from Arrow strings (UUID / FixedString / Enum are parsed server-side).
_CH_STRING_PREFIXES: tuple[str, ...] = ("String", "UUID", "FixedString(", "Enum")
# Columns ClickHouse computes itself — they cannot appear in an INSERT.
_NON_INSERTABLE_KINDS: frozenset[str] = frozenset({"MATERIALIZED", "ALIAS"})


class SchemaMismatchError(ValueError):
    """A staged file cannot be mapped onto the target table."""


def _ch_base_type(ch_type: str) -> str:
    while (m := _CH_WRAPPER.match(ch_type)) is not None:
        ch_type = m.group(1)
    return ch_type


def _to_utc_timestamp(column: pa.ChunkedArray, name: str) -> pa.ChunkedArray:
    """Normalise a column to ``timestamp[tz=UTC]`` (naive values count as UTC, like validator R4)."""
    t = column.type
    if pa.types.is_timestamp(t):
        return column.cast(pa.timestamp(t.unit, tz="UTC"))
    if pa.types.is_null(t) or pa.types.is_date(t):
        return column.cast(pa.timestamp("s", tz="UTC"))
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        # ClickHouse rejects ISO-8601 strings for DateTime, so parse them here.
        try:
            return column.cast(pa.timestamp("ns", tz="UTC"))  # with offset / 'Z'
        except pa.ArrowInvalid:
            pass
        try:
            return column.cast(pa.timestamp("ns")).cast(pa.timestamp("ns", tz="UTC"))
        except pa.ArrowInvalid as exc:
            raise SchemaMismatchError(f"column {name!r}: unparseable timestamp ({exc})") from exc
    raise SchemaMismatchError(f"column {name!r}: {t} cannot be loaded as DateTime")


def _coerce(column: pa.ChunkedArray, ch_type: str, name: str) -> pa.ChunkedArray:
    """Give ``column`` an Arrow type ClickHouse accepts for ``ch_type``.

    Pandas round-trips produce ``large_string``, all-null ``null`` columns
    and similar drift between batches; pin those down per target column.
    Numeric/Decimal columns are left to ClickHouse's own conversion.
    """
    base = _ch_base_type(ch_type)
    if base.startswith(_CH_STRING_PREFIXES):
        return column.cast(pa.string())
    if base.startswith("DateTime"):
        return _to_utc_timestamp(column, name)
    return column


class ClickHouseLoader:
    """Inserts staged Parquet batches into ClickHouse via the buffer table.

    The buffer table created in migration 003 fronts the persistent
    ``transactions_raw`` MergeTree, providing micro-batching at the
    storage layer.

    Each staged file is

    * projected onto the buffer table's insertable columns (extra fields
      such as Avro ``metadata`` are dropped, required ones must be present);
    * de-duplicated by ``transaction_id`` against ``transactions_raw`` —
      ``MergeTree`` does not deduplicate, so a retried load (or Kafka
      redelivery after a failed offset commit) must not insert twice;
    * flushed from the buffer right after the insert: Buffer keeps rows in
      RAM only, and offsets are committed / RFM is computed after the load.
    """

    BUFFER_TABLE: str = "transactions_buffer"
    TARGET_TABLE: str = "transactions_raw"
    # Every staged row must carry these (validator rule R1); the remaining
    # table columns (merchant_name, city, ...) fall back to their DEFAULTs.
    REQUIRED_COLUMNS: tuple[str, ...] = REQUIRED_FIELDS
    # transaction_ids per existence query (keeps the SQL below max_query_size).
    ID_CHUNK: int = 1000

    def __init__(self, ch_client: Any, *, buffer_table: str | None = None) -> None:
        self._client = ch_client
        if buffer_table:
            self.BUFFER_TABLE = buffer_table
        self._columns: dict[str, str] | None = None

    # ------------------------------------------------------------------ schema
    def _target_columns(self) -> dict[str, str]:
        """Insertable columns of the buffer table (name -> ClickHouse type), queried once."""
        if self._columns is None:
            result = self._client.query(
                "SELECT name, type, default_kind FROM system.columns "
                "WHERE database = currentDatabase() AND table = %(table)s "
                "ORDER BY position",
                parameters={"table": self.BUFFER_TABLE},
            )
            columns = {
                name: ch_type
                for name, ch_type, kind in result.result_rows
                if kind not in _NON_INSERTABLE_KINDS
            }
            absent = [c for c in self.REQUIRED_COLUMNS if c not in columns]
            if absent:
                raise SchemaMismatchError(
                    f"table {self.BUFFER_TABLE!r} lacks required columns {absent}"
                )
            self._columns = columns
        return self._columns

    def _project(self, table: pa.Table, path: Path) -> pa.Table:
        target = self._target_columns()
        present = set(table.column_names)
        missing = [c for c in self.REQUIRED_COLUMNS if c not in present]
        if missing:
            raise SchemaMismatchError(f"{path}: required columns missing: {missing}")
        extra = [c for c in table.column_names if c not in target]
        if extra:
            log.info("dropping_extra_columns", path=str(path), columns=extra,
                     table=self.BUFFER_TABLE)
        names = [c for c in target if c in present]
        return pa.Table.from_arrays(
            [_coerce(table.column(c), target[c], c) for c in names], names=names
        )

    # ------------------------------------------------------------------ dedup
    def _existing_ids(self, table: pa.Table) -> set[str]:
        """transaction_ids of ``table`` that are already in ``TARGET_TABLE``."""
        bounds = pc.min_max(table.column("transaction_date")).as_py()
        if bounds["min"] is None:
            raise SchemaMismatchError("transaction_date is empty in every row")
        # One day of slack: the DateTime column is compared in the server time zone.
        lo = (bounds["min"] - timedelta(days=1)).date()
        hi = (bounds["max"] + timedelta(days=1)).date()
        ids = pc.unique(table.column("transaction_id")).to_pylist()
        existing: set[str] = set()
        for i in range(0, len(ids), self.ID_CHUNK):
            result = self._client.query(
                f"SELECT DISTINCT transaction_id FROM {self.TARGET_TABLE} "
                "WHERE toDate(transaction_date) BETWEEN %(lo)s AND %(hi)s "
                "AND transaction_id IN %(ids)s",
                parameters={"lo": lo, "hi": hi, "ids": tuple(ids[i:i + self.ID_CHUNK])},
            )
            existing.update(row[0] for row in result.result_rows)
        return existing

    # ------------------------------------------------------------------
    def load_parquet(self, path: str | os.PathLike) -> int:
        """Load one Parquet file. Returns number of rows inserted.

        Idempotent: rows whose ``transaction_id`` is already loaded are
        skipped, so re-running a failed load never duplicates data.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)

        table = pq.read_table(path)
        if table.num_rows == 0:
            log.info("skip_empty_parquet", path=str(path))
            return 0
        table = self._project(table, path)

        # Rows from an earlier (failed) attempt may still sit in the buffer;
        # push them into TARGET_TABLE so the existence check sees them.
        self.flush()
        existing = self._existing_ids(table)
        if existing:
            keep = pc.invert(pc.is_in(
                table.column("transaction_id"),
                value_set=pa.array(sorted(existing), type=pa.string()),
            ))
            table = table.filter(keep)
            log.info("skip_already_loaded", path=str(path), rows=len(existing))
        rows = table.num_rows
        if rows == 0:
            return 0

        # clickhouse-connect knows how to ingest a pyarrow Table directly.
        self._client.insert_arrow(self.BUFFER_TABLE, table)
        self.flush()
        log.info("loaded_parquet", path=str(path), rows=rows,
                 table=self.BUFFER_TABLE)
        return rows

    def load_paths(self, paths: Iterable[str | os.PathLike]) -> int:
        total = 0
        for p in paths:
            total += self.load_parquet(p)
        return total

    def flush(self) -> None:
        """Force the buffer table to flush to the underlying MergeTree."""
        self._client.command(f"OPTIMIZE TABLE {self.BUFFER_TABLE}")
