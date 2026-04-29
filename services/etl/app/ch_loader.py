"""ClickHouse loader — pumps staged Parquet files into the buffer table."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterable

import pyarrow.parquet as pq
import structlog

log = structlog.get_logger("etl.ch_loader")


class ClickHouseLoader:
    """Inserts staged Parquet batches into ClickHouse via the buffer table.

    The buffer table created in migration 003 fronts the persistent
    ``transactions_raw`` MergeTree, providing micro-batching at the
    storage layer.
    """

    BUFFER_TABLE: str = "transactions_buffer"
    TARGET_TABLE: str = "transactions_raw"

    def __init__(self, ch_client: Any, *, buffer_table: str | None = None) -> None:
        self._client = ch_client
        if buffer_table:
            self.BUFFER_TABLE = buffer_table

    # ------------------------------------------------------------------
    def load_parquet(self, path: str | os.PathLike) -> int:
        """Load one Parquet file. Returns number of rows inserted."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(path)

        table = pq.read_table(path)
        rows = table.num_rows
        if rows == 0:
            log.info("skip_empty_parquet", path=str(path))
            return 0

        # clickhouse-connect knows how to ingest a pyarrow Table directly.
        self._client.insert_arrow(self.BUFFER_TABLE, table)
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
