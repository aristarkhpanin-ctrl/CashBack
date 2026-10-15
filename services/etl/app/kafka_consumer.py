"""Kafka consumer + Parquet staging — chapter 3.1, listing 3.2.

Offsets are **not** committed while staging. An extract run ends with an
:class:`ExtractManifest` (staged files + per-partition next offsets); the
DAG commits exactly those offsets with :func:`commit_offsets` only after
the files are loaded into ClickHouse, so a failed validate/load step makes
the next run re-read the same messages instead of losing them.

A message is dead-lettered (written to a JSONL file and skipped) only when it
can *never* be decoded: wrong framing, a schema id the registry does not
know (404 / 40403), an Avro payload that does not decode with its registered
schema, or a value that is not a record. Anything else -- Schema Registry
down, timeouts, 5xx, an unexpected error in the deserializer -- propagates,
so the extract task fails, Airflow retries it and no offset is committed.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

import fastavro
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import structlog
from aiokafka import AIOKafkaConsumer, TopicPartition
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import MessageField, SerializationContext

from app.jsonl import write_jsonl

log = structlog.get_logger("etl.kafka_consumer")

# Confluent Schema Registry wire format: magic byte 0, 4-byte big-endian schema id.
_MAGIC_BYTE = 0
_HEADER_SIZE = 5
# Schema Registry "Schema not found" (GET /schemas/ids/{id} -> 404).
_SR_SCHEMA_NOT_FOUND = 40403
# HTTP stacks of the Schema Registry client: requests (confluent-kafka 2.3, the
# Airflow image) and httpx / httpx2 (newer releases).
_TRANSPORT_MODULES = frozenset({"requests", "urllib3", "httpx", "httpcore", "httpx2", "httpcore2"})


class UndecodableMessageError(ValueError):
    """The message itself can never be decoded; it is dead-lettered and skipped."""


def _is_transport_error(exc: BaseException) -> bool:
    """Network-level failure talking to the Schema Registry (always transient)."""
    if isinstance(exc, OSError):  # incl. requests.RequestException, ConnectionError, TimeoutError
        return True
    return type(exc).__module__.partition(".")[0] in _TRANSPORT_MODULES


def _schema_not_found(exc: SchemaRegistryError) -> Optional[str]:
    """Reason to dead-letter if the registry says the schema id does not exist."""
    if exc.http_status_code == 404 and exc.error_code == _SR_SCHEMA_NOT_FOUND:
        return f"unknown schema id: {exc}"
    return None  # 5xx, auth, a URL that is not a Schema Registry (404 / -1) ...


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
        if "amount" in df.columns:
            df["amount"] = df["amount"].astype(str)
        # transaction_date stays the producer's string: parsing it here would
        # fail the whole extract on one odd value (pandas infers one format per
        # column). DataValidator R4 parses each value and the cleansed file
        # carries the normalised UTC timestamp.
        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(table, path, compression="snappy")
        log.info("staged_batch", path=str(path), records=len(records))
        return path

    def write_dead_letters(self, entries: list[dict]) -> Path:
        """Persist undecodable messages (JSON lines) for inspection / replay."""
        if not entries:
            raise ValueError("no dead letters to write")
        path = write_jsonl(self._out_path("dead_letter", ".jsonl"), entries)
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
        self._registry: Any = None
        self._deserializer = self._build_deserializer()
        # partition -> next offset to consume; committed later, after the load.
        self._next_offsets: dict[TopicPartition, int] = {}
        self.last_polled: int = 0
        self.dead_letter_count: int = 0
        self.dead_letter_paths: list[Path] = []

    # ------------------------------------------------------------------ setup
    def _build_deserializer(self) -> AvroDeserializer:
        self._registry = SchemaRegistryClient({"url": self._sr_url})
        return AvroDeserializer(self._registry)

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
        """Decode one message.

        Raises :class:`UndecodableMessageError` if the message can never be
        decoded; any other exception is (potentially) transient and must
        fail the extract.
        """
        ctx = SerializationContext(self._topic, MessageField.VALUE)
        try:
            rec = self._deserializer(msg.value, ctx)
        except Exception as exc:
            reason = self._permanent_failure(msg.value, exc)
            if reason is None:
                raise
            raise UndecodableMessageError(reason) from exc
        if not isinstance(rec, dict):
            raise UndecodableMessageError(
                f"expected a TransactionEvent record, got {type(rec).__name__}")
        return rec

    def _permanent_failure(self, value: Any, exc: Exception) -> Optional[str]:
        """Why ``value`` can never be decoded, or ``None`` if ``exc`` may be transient.

        Error types alone cannot tell these apart (newer confluent-kafka
        raises SerializationError for configuration problems, fastavro raises
        plain EOFError / UnicodeDecodeError / IndexError), so an error that is
        neither a registry answer nor a network failure is checked by decoding
        the payload directly with its registered writer schema.
        """
        if isinstance(exc, SchemaRegistryError):
            return _schema_not_found(exc)
        if _is_transport_error(exc):
            return None
        if self._registry is None or not isinstance(value, bytes | bytearray):
            return None  # nothing to check the bytes against
        if len(value) <= _HEADER_SIZE or value[0] != _MAGIC_BYTE:
            return (f"not in Schema Registry wire format (magic byte {_MAGIC_BYTE} + "
                    f"4-byte schema id): {type(exc).__name__}: {exc}")
        schema_id = int.from_bytes(value[1:_HEADER_SIZE], "big")
        try:
            schema = self._registry.get_schema(schema_id)  # Schema Registry down -> raises
        except SchemaRegistryError as sr_exc:
            reason = _schema_not_found(sr_exc)
            if reason is None:
                raise
            return reason
        if getattr(schema, "references", None):
            return None  # named references: cannot decode without the deserializer
        writer = fastavro.parse_schema(json.loads(schema.schema_str))
        try:
            fastavro.schemaless_reader(io.BytesIO(bytes(value[_HEADER_SIZE:])), writer)
        except Exception as decode_exc:  # noqa: BLE001 -- local decode of the bytes only
            return (f"Avro payload does not decode with schema id {schema_id}: "
                    f"{type(decode_exc).__name__}: {decode_exc}")
        return None  # the payload is fine: the deserializer failed for another reason

    def _dead_letter(self, msg: Any, exc: UndecodableMessageError) -> dict:
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
            "cause": None if exc.__cause__ is None else (
                f"{type(exc.__cause__).__name__}: {exc.__cause__}"),
        }

    def _advance(self, msg: Any) -> None:
        tp = TopicPartition(msg.topic, msg.partition)
        self._next_offsets[tp] = max(self._next_offsets.get(tp, 0), msg.offset + 1)

    def _decode_all(self, msgs: list[Any]) -> tuple[list[dict], list[dict]]:
        """Decode polled messages -> (records, dead-letter entries); tracks offsets."""
        records: list[dict] = []
        dead: list[dict] = []
        for m in msgs:
            try:
                records.append(self._deserialize(m))
            except UndecodableMessageError as exc:
                dead.append(self._dead_letter(m, exc))
            self._advance(m)
        return records, dead

    def pending_offsets(self) -> dict[TopicPartition, int]:
        """Next offsets of everything consumed so far (nothing is committed here)."""
        return dict(self._next_offsets)

    # ------------------------------------------------------------------ public API
    async def consume_batch(self) -> tuple[int, Optional[Path]]:
        """Drain up to ``BATCH_SIZE`` messages and stage them.

        Offsets are only tracked (see :meth:`pending_offsets`); messages that
        can never be decoded go to a dead-letter file in staging. Transient
        decode failures (Schema Registry unreachable, 5xx, ...) propagate.

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
            # Decode off the event loop: Schema Registry calls block (the
            # client retries for seconds while the registry is unreachable),
            # and a starved loop stops aiokafka's heartbeats -- the group then
            # evicts this consumer and re-delivers the same messages.
            decoded, undecodable = await asyncio.to_thread(self._decode_all, msgs)
            records.extend(decoded)
            dead.extend(undecodable)
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
