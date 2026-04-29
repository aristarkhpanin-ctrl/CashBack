"""Kafka consumer + Parquet staging — chapter 3.1, listing 3.2."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import structlog
from aiokafka import AIOKafkaConsumer, TopicPartition
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext

log = structlog.get_logger("etl.kafka_consumer")


class StagingWriter:
    """Append-only Parquet writer; rolls a new file per batch."""

    def __init__(self, base_dir: str | os.PathLike) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def write_batch(self, records: list[dict]) -> Path:
        if not records:
            raise ValueError("empty batch — nothing to write")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        out_dir = self.base_dir / datetime.now(timezone.utc).strftime("dt=%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"batch_{ts}.parquet"

        df = pd.DataFrame.from_records(records)
        # Coerce to consistent types for ClickHouse Parquet ingestion.
        if "amount" in df.columns:
            df["amount"] = df["amount"].astype(str)
        if "transaction_date" in df.columns:
            df["transaction_date"] = pd.to_datetime(df["transaction_date"], utc=True)
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, path, compression="snappy")
        log.info("staged_batch", path=str(path), records=len(records))
        return path


class TransactionConsumer:
    """Consumes Avro-encoded TransactionEvent messages and stages them.

    Constants come straight from listing 3.2:

        BATCH_SIZE   = 5000
        POLL_TIMEOUT = 50  (milliseconds)
    """

    BATCH_SIZE: int = 5000
    POLL_TIMEOUT: int = 50  # milliseconds

    def __init__(
        self,
        bootstrap_servers: str,
        schema_registry_url: str,
        topic: str,
        staging: StagingWriter,
        group_id: str = "cashback-etl-group",
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._sr_url = schema_registry_url
        self._topic = topic
        self._group_id = group_id
        self._staging = staging
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._deserializer = self._build_deserializer()

    # ------------------------------------------------------------------ setup
    def _build_deserializer(self) -> AvroDeserializer:
        sr = SchemaRegistryClient({"url": self._sr_url})
        return AvroDeserializer(sr)

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._topic,
            bootstrap_servers=self._bootstrap,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_poll_records=self.BATCH_SIZE,
        )
        await self._consumer.start()
        log.info(
            "consumer_started",
            topic=self._topic,
            group=self._group_id,
            batch_size=self.BATCH_SIZE,
        )

    async def stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    # ------------------------------------------------------------------ poll
    async def _poll_once(self) -> list[Any]:
        assert self._consumer is not None
        result = await self._consumer.getmany(
            timeout_ms=self.POLL_TIMEOUT,
            max_records=self.BATCH_SIZE,
        )
        msgs: list[Any] = []
        for _tp, batch in result.items():
            msgs.extend(batch)
        return msgs

    def _deserialize(self, msg: Any) -> Optional[dict]:
        ctx = SerializationContext(self._topic, MessageField.VALUE)
        try:
            return self._deserializer(msg.value, ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("deserialise_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------ public API
    async def consume_batch(self) -> tuple[int, Optional[Path]]:
        """Drain up to ``BATCH_SIZE`` messages, stage them, commit offsets.

        Returns (record_count, parquet_path_or_None).
        """
        assert self._consumer is not None, "call start() first"
        records: list[dict] = []
        while len(records) < self.BATCH_SIZE:
            msgs = await self._poll_once()
            if not msgs:
                break
            for m in msgs:
                rec = self._deserialize(m)
                if rec is not None:
                    records.append(rec)
            if len(msgs) < self.BATCH_SIZE:
                break

        if not records:
            return 0, None

        path = self._staging.write_batch(records)
        await self._consumer.commit()
        log.info("batch_committed", count=len(records))
        return len(records), path

    async def stream_batches(
        self,
        on_batch: Callable[[int, Path], Any] | None = None,
        max_batches: Optional[int] = None,
    ) -> AsyncIterator[Path]:
        """Yield paths to staged parquet files until ``max_batches`` reached
        (or forever if ``None``).
        """
        i = 0
        while max_batches is None or i < max_batches:
            count, path = await self.consume_batch()
            if path is None:
                await asyncio.sleep(1.0)
                continue
            if on_batch is not None:
                on_batch(count, path)
            yield path
            i += 1
