"""TransactionListener — chapter 3.2, listing 3.11 (stage C: B45).

Consumes Avro-serialised TransactionEvent records from
``transactions.raw`` (group ``cashback-accrual-group`` — *separate*
from the ETL group), and triggers an accrual when the user has an
active accepted offer for the same MCC.

The hot-path check is O(1) — we look up Redis ``accepted_offers:{user_id}:{mcc_code}``
and short-circuit when no key is present. Postgres (``AccrualEngine``) is
the authority: the offer is consumed in the accrual transaction.

Delivery semantics — at-least-once; the engine is idempotent, so a replay
ends in ``duplicate``:

* an offset is committed only past messages that are done: accrued,
  skipped by a business rule, or dead-lettered;
* a *retryable* failure — Postgres / Redis / Schema Registry unreachable,
  timeouts, deadlocks, serialisation failures, anything not provably about
  the message itself — is NOT committed: the failed partition is sought
  back to the failed offset (every other partition with unprocessed records
  of the batch to its first one), offsets before it are committed, and the
  batch is re-read after a pause of 1 s doubling up to 30 s;
* a *permanent* failure — undecodable Avro, missing or invalid fields, a
  data error from Postgres (SQLSTATE class 22/23) — publishes the raw
  message to the DLQ topic ``transactions.accrual.dlq`` (created on demand;
  the error and the source coordinates in headers) and is then committed
  past. A failed DLQ publish is retryable;
* ``auto_offset_reset='earliest'``: a new group starts at the beginning of
  the topic, so transactions published before its first commit are not
  skipped;
* a failed commit (e.g. during a rebalance) is logged and the loop goes on:
  the next commit supersedes it, a new owner re-reads from the last one.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import fastavro
import structlog
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.schema_registry.error import SchemaRegistryError
from confluent_kafka.serialization import MessageField, SerializationContext
from prometheus_client import Counter
from redis.exceptions import ResponseError as RedisResponseError

log = structlog.get_logger("listener")


PROCESSED = Counter(
    "transaction_listener_processed_total",
    "Transactions consumed from Kafka",
    ["outcome"],
)
RETRIES = Counter(
    "transaction_listener_retries_total",
    "Batches re-read after a retryable failure (offset not committed)",
)
COMMIT_FAILURES = Counter(
    "transaction_listener_commit_failures_total",
    "Offset commits that failed (e.g. during a rebalance)",
)

# Confluent Schema Registry wire format: magic byte 0, 4-byte big-endian schema id.
_MAGIC_BYTE = 0
_HEADER_SIZE = 5
# Schema Registry "Schema not found" (GET /schemas/ids/{id} -> 404).
_SR_SCHEMA_NOT_FOUND = 40403
# HTTP stacks of the Schema Registry client (requests in older, httpx in
# newer confluent-kafka releases).
_TRANSPORT_MODULES = frozenset({"requests", "urllib3", "httpx", "httpcore",
                                "httpx2", "httpcore2"})
# Postgres errors about the data of THIS message: data exception (22…),
# integrity constraint violation (23…). Every other SQLSTATE — connection
# (08), rollback / deadlock / serialisation (40), resources (53), operator
# intervention (57), undefined function during a deploy (42) — is retried.
_PERMANENT_SQLSTATE_CLASSES = frozenset({"22", "23"})
_KAFKA_TOPIC_ALREADY_EXISTS = 36

REQUIRED_FIELDS = ("transaction_id", "user_id", "mcc_code", "amount", "transaction_date")


class PermanentMessageError(ValueError):
    """The message can never be processed; it goes to the DLQ."""


def _sqlstate(exc: BaseException) -> Optional[str]:
    """SQLSTATE of a (SQLAlchemy-wrapped) asyncpg error, if any."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        state = getattr(cur, "sqlstate", None)
        if isinstance(state, str) and state:
            return state
        cur = getattr(cur, "orig", None) or cur.__cause__
    return None


def is_retryable(exc: BaseException) -> bool:
    """``False`` only when the failure is provably about the message itself.

    Anything else — including an error nobody anticipated — is retried: a
    stuck partition is visible (``transaction_listener_retries_total``) and
    recovers, a dead-lettered accrual is silently missing money.
    """
    if isinstance(exc, PermanentMessageError):
        return False
    state = _sqlstate(exc)
    if state is not None:
        return state[:2] not in _PERMANENT_SQLSTATE_CLASSES
    if isinstance(exc, RedisResponseError) and str(exc).startswith("WRONGTYPE"):
        return False  # the offer key holds a non-string: no retry fixes it
    return True


def _is_transport_error(exc: BaseException) -> bool:
    """Network-level failure talking to the Schema Registry (always transient)."""
    if isinstance(exc, OSError):
        return True
    return type(exc).__module__.partition(".")[0] in _TRANSPORT_MODULES


def _schema_not_found(exc: SchemaRegistryError) -> Optional[str]:
    if exc.http_status_code == 404 and exc.error_code == _SR_SCHEMA_NOT_FOUND:
        return f"unknown schema id: {exc}"
    return None  # 5xx, auth, a URL that is not a Schema Registry …


def parse_transaction_date(raw: Any) -> Optional[datetime]:
    """ISO-8601 string → aware UTC datetime (naive = UTC); ``None`` if unparsable."""
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    try:
        return ts.astimezone(UTC)
    except OverflowError:
        return None


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    user_id: str
    mcc: str
    amount: Decimal
    transaction_at: datetime


def parse_transaction(evt: Any) -> Transaction:
    """A decoded TransactionEvent → :class:`Transaction`, or PermanentMessageError."""
    if not isinstance(evt, dict):
        raise PermanentMessageError(
            f"expected a TransactionEvent record, got {type(evt).__name__}")
    missing = [f for f in REQUIRED_FIELDS
               if not isinstance(evt.get(f), str) or not evt[f].strip()]
    if missing:
        raise PermanentMessageError(f"missing or empty fields: {', '.join(missing)}")
    try:
        amount = Decimal(evt["amount"].strip())
    except InvalidOperation:
        raise PermanentMessageError(f"amount is not a number: {evt['amount']!r}") from None
    if not amount.is_finite():
        raise PermanentMessageError(f"amount is not finite: {evt['amount']!r}")
    transaction_at = parse_transaction_date(evt["transaction_date"])
    if transaction_at is None:
        raise PermanentMessageError(
            f"transaction_date is not ISO-8601: {evt['transaction_date']!r}")
    return Transaction(
        transaction_id=evt["transaction_id"].strip(),
        user_id=evt["user_id"].strip(),
        mcc=evt["mcc_code"].strip(),
        amount=amount,
        transaction_at=transaction_at,
    )


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


class TransactionListener:
    BATCH_SIZE: int = 500
    POLL_TIMEOUT_MS: int = 100

    def __init__(
        self,
        settings: Any,
        accrual_engine: Any,
        redis_client: Any,
        *,
        dlq_producer: Any = None,
    ) -> None:
        self._settings = settings
        self._accrual = accrual_engine
        self._redis = redis_client
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._registry: Optional[SchemaRegistryClient] = None
        self._deserializer: Optional[AvroDeserializer] = None
        # The DLQ producer is injected (tests) or created, acks=all, on the
        # first dead letter and closed with the consumer.
        self._dlq_producer = dlq_producer
        self._owns_dlq_producer = dlq_producer is None
        self._dlq_topic_ready = False
        self._retry_delay = settings.retry_backoff_initial_s
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._registry = SchemaRegistryClient({"url": self._settings.schema_registry_url})
        self._deserializer = AvroDeserializer(self._registry)
        self._consumer = AIOKafkaConsumer(
            self._settings.transactions_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.consumer_group_accrual,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            max_poll_records=self.BATCH_SIZE,
        )
        await self._consumer.start()
        log.info(
            "listener_started",
            topic=self._settings.transactions_topic,
            group=self._settings.consumer_group_accrual,
        )

    async def stop(self) -> None:
        """Shutdown: the loop does not restart."""
        self._stop.set()
        await self._close()

    async def _close(self) -> None:
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            with contextlib.suppress(Exception):
                await consumer.stop()
        if self._owns_dlq_producer and self._dlq_producer is not None:
            producer, self._dlq_producer = self._dlq_producer, None
            with contextlib.suppress(Exception):
                await producer.stop()

    # ------------------------------------------------------------------
    def _decode(self, value: Any) -> dict:
        """Avro value → dict. PermanentMessageError if it can never be decoded;
        any other exception (Schema Registry down, 5xx …) is transient.

        Runs in a worker thread: the deserializer talks HTTP to the registry.
        """
        ctx = SerializationContext(self._settings.transactions_topic, MessageField.VALUE)
        try:
            evt = self._deserializer(value, ctx)
        except Exception as exc:
            reason = self._permanent_decode_failure(value, exc)
            if reason is None:
                raise
            raise PermanentMessageError(reason) from exc
        if not isinstance(evt, dict):
            raise PermanentMessageError(
                f"expected a TransactionEvent record, got {type(evt).__name__}")
        return evt

    def _permanent_decode_failure(self, value: Any, exc: Exception) -> Optional[str]:
        """Why ``value`` can never be decoded, or ``None`` if ``exc`` may be transient.

        Same rules as the ETL consumer (services/etl/app/kafka_consumer.py):
        error types alone do not tell these apart, so a failure that is neither
        a registry answer nor a network error is checked by decoding the bytes
        with their registered writer schema.
        """
        if isinstance(exc, SchemaRegistryError):
            return _schema_not_found(exc)
        if _is_transport_error(exc):
            return None
        if not isinstance(value, bytes | bytearray):
            return f"value is not bytes: {type(value).__name__}"
        if len(value) <= _HEADER_SIZE or value[0] != _MAGIC_BYTE:
            return (f"not in Schema Registry wire format (magic byte {_MAGIC_BYTE} + "
                    f"4-byte schema id): {type(exc).__name__}: {exc}")
        schema_id = int.from_bytes(value[1:_HEADER_SIZE], "big")
        try:
            schema = self._registry.get_schema(schema_id)
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
        except Exception as decode_exc:  # noqa: BLE001 — local decode of the bytes only
            return (f"Avro payload does not decode with schema id {schema_id}: "
                    f"{type(decode_exc).__name__}: {decode_exc}")
        return None  # the payload is fine: the deserializer failed for another reason

    async def _process(self, msg: Any) -> str:
        """Handle one message → outcome label; raises on failure."""
        evt = await asyncio.to_thread(self._decode, msg.value)
        tx = parse_transaction(evt)

        # ---- O(1) Redis check -----------------------------------------
        key = self._settings.accepted_offer_key_fmt.format(
            user_id=tx.user_id, mcc_code=tx.mcc,
        )
        offer_payload = await self._redis.get(key)
        if not offer_payload:
            return "no_offer"

        try:
            offer = json.loads(offer_payload)
        except (TypeError, ValueError):
            offer = None
        if not isinstance(offer, dict):
            log.warning("offer_payload_corrupt", key=key)
            return "bad_offer"
        recommendation_id = offer.get("recommendation_id")
        if not recommendation_id:
            # Written before stage C: no offer row to consume — never paid.
            log.warning("legacy_offer_ignored", key=key)
            return "legacy_offer"
        if not (_is_uuid(recommendation_id) and _is_uuid(offer.get("campaign_id"))):
            log.warning("offer_payload_corrupt", key=key)
            return "bad_offer"
        if not _is_uuid(tx.user_id):
            raise PermanentMessageError(f"user_id is not a UUID: {tx.user_id!r}")

        result = await self._accrual.accrue(
            user_id=tx.user_id,
            campaign_id=str(offer["campaign_id"]),
            recommendation_id=str(recommendation_id),
            transaction_id=tx.transaction_id,
            mcc=tx.mcc,
            amount=tx.amount,
            transaction_at=tx.transaction_at,
        )
        return "accrued" if result.accrued else result.skipped_reason

    async def _handle(self, msg: Any) -> str:
        """Outcome of ``msg``; a permanent failure is dead-lettered. Raises
        when the message must be retried (incl. a failed DLQ publish)."""
        try:
            return await self._process(msg)
        except Exception as exc:
            if is_retryable(exc):
                raise
            await self._dead_letter(msg, exc)
            return "dead_lettered"

    # ------------------------------------------------------------------
    async def _dlq(self) -> Any:
        if self._dlq_producer is None:
            producer = AIOKafkaProducer(
                bootstrap_servers=self._settings.kafka_bootstrap_servers, acks="all",
            )
            await producer.start()
            self._dlq_producer = producer
        if not self._dlq_topic_ready:
            await self._ensure_dlq_topic()
        return self._dlq_producer

    async def _ensure_dlq_topic(self) -> None:
        """Create the DLQ topic once; if that fails the publish still tries
        (the broker may auto-create it) and its failure is retried."""
        topic = self._settings.dlq_topic
        admin = AIOKafkaAdminClient(bootstrap_servers=self._settings.kafka_bootstrap_servers)
        try:
            await admin.start()
            response = await admin.create_topics([NewTopic(
                topic, num_partitions=1,
                replication_factor=self._settings.dlq_replication_factor,
            )])
            for name, code, *_ in getattr(response, "topic_errors", ()):
                if name == topic and code not in (0, _KAFKA_TOPIC_ALREADY_EXISTS):
                    log.warning("dlq_topic_create_failed", topic=topic, error_code=code)
                    return
            self._dlq_topic_ready = True
        except Exception as exc:  # noqa: BLE001
            log.warning("dlq_topic_create_failed", topic=topic, error=str(exc))
        finally:
            with contextlib.suppress(Exception):
                await admin.close()

    async def _dead_letter(self, msg: Any, exc: BaseException) -> None:
        """Raw message + error to the DLQ, acknowledged before it is committed."""
        error = f"{type(exc).__name__}: {exc}"
        headers = [*(getattr(msg, "headers", None) or ()),
                   ("dlq.error", error.encode("utf-8")),
                   ("dlq.source.topic", str(msg.topic).encode("utf-8")),
                   ("dlq.source.partition", str(msg.partition).encode("utf-8")),
                   ("dlq.source.offset", str(msg.offset).encode("utf-8")),
                   ("dlq.consumer.group",
                    self._settings.consumer_group_accrual.encode("utf-8"))]
        producer = await self._dlq()
        await producer.send_and_wait(self._settings.dlq_topic, value=msg.value,
                                     key=msg.key, headers=headers)
        log.error("dead_lettered", topic=msg.topic, partition=msg.partition,
                  offset=msg.offset, dlq=self._settings.dlq_topic, error=error)

    # ------------------------------------------------------------------
    async def _commit(self, offsets: dict[TopicPartition, int]) -> None:
        if not offsets:
            return
        try:
            await self._consumer.commit(offsets)
        except Exception as exc:  # noqa: BLE001 — CommitFailedError on rebalance …
            # Not fatal: the next commit supersedes this one; a new owner of
            # the partition re-reads from the last commit (replays → duplicate).
            COMMIT_FAILURES.inc()
            log.warning("commit_failed", error=f"{type(exc).__name__}: {exc}",
                        offsets={f"{tp.topic}[{tp.partition}]": o
                                 for tp, o in offsets.items()})

    def _rewind(self, tp: TopicPartition, offset: int) -> None:
        try:
            self._consumer.seek(tp, offset)
        except Exception as exc:  # noqa: BLE001 — partition revoked meanwhile
            log.warning("seek_failed", topic=tp.topic, partition=tp.partition,
                        offset=offset, error=str(exc))

    async def _process_batch(self, batch: dict[TopicPartition, list[Any]]) -> bool:
        """Process a polled batch; ``False`` if it stopped on a retryable failure."""
        done: dict[TopicPartition, int] = {}
        items = list(batch.items())
        for index, (tp, msgs) in enumerate(items):
            for msg in msgs:
                try:
                    outcome = await self._handle(msg)
                except Exception as exc:  # noqa: BLE001 — retryable by _handle
                    RETRIES.inc()
                    log.warning("accrual_retry", topic=tp.topic, partition=tp.partition,
                                offset=msg.offset, retry_in=self._retry_delay,
                                error=f"{type(exc).__name__}: {exc}")
                    # The consumer's position is already past the whole batch:
                    # without a seek the unprocessed records would be skipped.
                    self._rewind(tp, msg.offset)
                    for later_tp, later_msgs in items[index + 1:]:
                        self._rewind(later_tp, later_msgs[0].offset)
                    await self._commit(done)
                    return False
                PROCESSED.labels(outcome=outcome).inc()
                done[tp] = msg.offset + 1
        await self._commit(done)
        return True

    async def _pause(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # ------------------------------------------------------------------
    async def run(self) -> None:
        await self.start()
        try:
            while not self._stop.is_set():
                batch = await self._consumer.getmany(
                    timeout_ms=self.POLL_TIMEOUT_MS,
                    max_records=self.BATCH_SIZE,
                )
                if not batch:
                    continue
                if await self._process_batch(batch):
                    self._retry_delay = self._settings.retry_backoff_initial_s
                else:
                    await self._pause(self._retry_delay)
                    self._retry_delay = min(self._retry_delay * 2,
                                            self._settings.retry_backoff_max_s)
        finally:
            await self._close()
