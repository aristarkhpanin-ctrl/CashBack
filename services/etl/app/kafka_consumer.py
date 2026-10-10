"""Kafka consumer + Parquet staging — chapter 3.1, listing 3.2.

Offsets are **not** committed while staging. An extract run ends with an
:class:`ExtractManifest` (staged files + per-partition next offsets); the
DAG commits exactly those offsets with :func:`commit_offsets` only after
the files are loaded into ClickHouse, so a failed validate/load step makes
the next run re-read the same messages instead of losing them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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


@dataclass
class ExtractManifest:
    """What one extract run staged and which Kafka offsets that covers.

    ``offsets`` maps partition -> next offset to consume (last processed
    offset + 1), i.e. exactly what gets committed for ``group_id`` once the
    staged files are loaded. Dead-lettered messages count as processed.
    """

    topic: str
    group_id: str
    offsets: dict[int, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    records: int = 0
    dead_letter_files: list[str] = field(default_factory=list)
    dead_letter_count: int = 0

    def to_json(self) -> str:
        data = asdict(self)
        data["offsets"] = {str(p): o for p, o in sorted(self.offsets.items())}
        return json.dumps(data, indent=2)

    @classmethod
    def read(cls, path: str | os.PathLike) -> ExtractManifest:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        data["offsets"] = {int(p): int(o) for p, o in data["offsets"].items()}
        return cls(**data)


def _b64(raw: Optional[bytes]) -> Optional[str]:
    return None if raw is None else base64.b64encode(raw).decode("ascii")


class StagingWriter:
    """Append-only Parquet writer; rolls a new file per batch."""

    def __init__(self, base_dir: str | os.PathLike) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _out_path(self, prefix: str, suffix: str) -> Path:
        now = datetime.now(UTC)
        out_dir = self.base_dir / now.strftime("dt=%Y-%m-%d")
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{prefix}_{now.strftime('%Y%m%dT%H%M%S%f')}{suffix}"

    def write_batch(self, records: list[dict]) -> Path:
        if not records:
            raise ValueError("empty batch — nothing to write")
        path = self._out_path("batch", ".parquet")

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

    def write_dead_letters(self, entries: list[dict]) -> Path:
        """Persist undecodable messages (JSON lines) for inspection / replay."""
        if not entries:
            raise ValueError("no dead letters to write")
        path = self._out_path("dead_letter", ".jsonl")
        with path.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        log.warning("staged_dead_letters", path=str(path), count=len(entries))
        return path

    def write_manifest(self, manifest: ExtractManifest) -> Path:
        """Write the manifest atomically (a reader never sees a partial file)."""
        path = self._out_path("manifest", ".json")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(manifest.to_json(), encoding="utf-8")
        os.replace(tmp, path)
        log.info("staged_manifest", path=str(path), files=len(manifest.files),
                 offsets=manifest.offsets)
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
        # partition -> next offset to consume; committed later, after the load.
        self._next_offsets: dict[TopicPartition, int] = {}
        self.last_polled: int = 0
        self.dead_letter_count: int = 0
        self.dead_letter_paths: list[Path] = []

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

    def _deserialize(self, msg: Any) -> dict:
        """Decode one message; raises if it is not a TransactionEvent record."""
        ctx = SerializationContext(self._topic, MessageField.VALUE)
        rec = self._deserializer(msg.value, ctx)
        if not isinstance(rec, dict):
            raise ValueError(f"expected a TransactionEvent record, got {type(rec).__name__}")
        return rec

    def _dead_letter(self, msg: Any, exc: Exception) -> dict:
        self.dead_letter_count += 1
        log.warning(
            "deserialise_failed",
            topic=msg.topic, partition=msg.partition, offset=msg.offset,
            error=str(exc),
        )
        return {
            "topic": msg.topic,
            "partition": msg.partition,
            "offset": msg.offset,
            "timestamp": getattr(msg, "timestamp", None),
            "key": _b64(getattr(msg, "key", None)),
            "value": _b64(msg.value),
            "error": f"{type(exc).__name__}: {exc}",
        }

    def _advance(self, msg: Any) -> None:
        tp = TopicPartition(msg.topic, msg.partition)
        self._next_offsets[tp] = max(self._next_offsets.get(tp, 0), msg.offset + 1)

    def pending_offsets(self) -> dict[TopicPartition, int]:
        """Next offsets of everything consumed so far (nothing is committed here)."""
        return dict(self._next_offsets)

    # ------------------------------------------------------------------ public API
    async def consume_batch(self) -> tuple[int, Optional[Path]]:
        """Drain up to ``BATCH_SIZE`` messages and stage them.

        Offsets are only tracked (see :meth:`pending_offsets`); undecodable
        messages go to a dead-letter file in staging.

        Returns (record_count, parquet_path_or_None).
        """
        assert self._consumer is not None, "call start() first"
        records: list[dict] = []
        dead: list[dict] = []
        self.last_polled = 0
        while len(records) < self.BATCH_SIZE:
            msgs = await self._poll_once()
            if not msgs:
                break
            self.last_polled += len(msgs)
            for m in msgs:
                try:
                    records.append(self._deserialize(m))
                except Exception as exc:  # noqa: BLE001 — anything undecodable is dead-lettered
                    dead.append(self._dead_letter(m, exc))
                self._advance(m)
            if len(msgs) < self.BATCH_SIZE:
                break

        if dead:
            self.dead_letter_paths.append(self._staging.write_dead_letters(dead))
        if not records:
            return 0, None

        path = self._staging.write_batch(records)
        log.info("batch_staged", count=len(records), dead_letters=len(dead))
        return len(records), path

    async def drain(
        self,
        *,
        max_batches: int = 200,
        max_empty_polls: int = 3,
        idle_sleep: float = 0.5,
    ) -> ExtractManifest:
        """Stage batches until the topic is drained; describe them in a manifest."""
        files: list[str] = []
        records = 0
        empty_polls = 0
        while empty_polls < max_empty_polls and len(files) < max_batches:
            count, path = await self.consume_batch()
            if self.last_polled == 0:
                empty_polls += 1
                await asyncio.sleep(idle_sleep)
                continue
            empty_polls = 0
            if path is not None:
                files.append(str(path))
                records += count
        manifest = ExtractManifest(
            topic=self._topic,
            group_id=self._group_id,
            offsets={tp.partition: off for tp, off in self._next_offsets.items()},
            files=files,
            records=records,
            dead_letter_files=[str(p) for p in self.dead_letter_paths],
            dead_letter_count=self.dead_letter_count,
        )
        log.info("extract_drained", files=len(files), records=records,
                 dead_letters=self.dead_letter_count, offsets=manifest.offsets)
        return manifest

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


async def commit_offsets(
    manifest: ExtractManifest,
    bootstrap_servers: str,
    *,
    consumer_factory: Callable[..., Any] | None = None,
) -> dict[int, int]:
    """Commit the manifest's offsets for its consumer group.

    Idempotent: partitions whose committed offset is already at (or past)
    the manifest's are left alone, so a retried task never rewinds the
    group. Returns partition -> offset actually committed by this call.
    """
    if not manifest.offsets:
        log.info("no_offsets_to_commit", group=manifest.group_id)
        return {}
    consumer = (consumer_factory or AIOKafkaConsumer)(
        bootstrap_servers=bootstrap_servers,
        group_id=manifest.group_id,
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        wanted = {TopicPartition(manifest.topic, p): o for p, o in manifest.offsets.items()}
        consumer.assign(list(wanted))
        to_commit: dict[TopicPartition, int] = {}
        for tp, offset in wanted.items():
            current = await consumer.committed(tp)
            if current is not None and current >= offset:
                log.info("offset_already_committed", partition=tp.partition,
                         committed=current, manifest=offset)
                continue
            to_commit[tp] = offset
        if to_commit:
            await consumer.commit(to_commit)
        committed = {tp.partition: o for tp, o in to_commit.items()}
        log.info("offsets_committed", group=manifest.group_id, offsets=committed)
        return committed
    finally:
        await consumer.stop()
