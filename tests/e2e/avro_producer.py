"""Publish ``TransactionEvent`` records exactly like ``services/tx_simulator``.

transaction_listener (and the ETL consumer) decode ``transactions.raw`` with
Confluent's ``AvroDeserializer``: every value must be Confluent wire-format
Avro (magic byte + schema id + body) whose schema is registered in Schema
Registry. A JSON payload is dropped by the listener as ``bad_payload``, so a
test that publishes JSON can never observe an accrual.

Like the simulator, values are serialised with ``AvroSerializer`` and the
schema from ``infrastructure/kafka/schemas/transaction_event.avsc``
(default ``TopicNameStrategy`` → subject ``<topic>-value``), keys are the
UTF-8 ``user_id``.

Used by ``tests/e2e/test_full_cashback_cycle.py`` (live stack) and by
``tests/integration/test_listener_avro_accrual.py``, which proves against
real Kafka + Schema Registry that the listener accrues on these records.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "infrastructure" / "kafka" / "schemas" / "transaction_event.avsc"
)


def transaction_event(
    *,
    user_id: str,
    mcc_code: str,
    amount: str,
    transaction_id: str | None = None,
    channel: str = "POS",
    merchant_id: str | None = "M-TEST",
) -> dict[str, Any]:
    """A TransactionEvent dict in the shape the simulator produces."""
    return {
        "transaction_id": transaction_id or f"tx-test-{uuid.uuid4().hex}",
        "user_id": user_id,
        "mcc_code": mcc_code,
        "amount": amount,
        "currency": "RUB",
        "transaction_date": datetime.now(UTC).isoformat(timespec="seconds"),
        "channel": channel,
        "merchant_id": merchant_id,
        "metadata": None,
    }


def publish_transactions(
    bootstrap_servers: str,
    schema_registry_url: str,
    topic: str,
    events: Iterable[dict[str, Any]],
    *,
    timeout: float = 30.0,
) -> None:
    """Avro-serialise ``events`` and publish them; raise unless all are acked."""
    from confluent_kafka import Producer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import (
        MessageField,
        SerializationContext,
        StringSerializer,
    )

    serializer = AvroSerializer(
        SchemaRegistryClient({"url": schema_registry_url}),
        SCHEMA_PATH.read_text(encoding="utf-8"),
    )
    key_serializer = StringSerializer("utf_8")
    ctx = SerializationContext(topic, MessageField.VALUE)
    # Same compression as the simulator (the listener must read zstd, B18).
    producer = Producer({
        "bootstrap.servers": bootstrap_servers,
        "compression.type": "zstd",
        "acks": "all",
        "client.id": "cashback-tests",
    })

    errors: list[str] = []

    def _on_delivery(err, msg) -> None:  # noqa: ANN001 — librdkafka callback
        if err is not None:
            errors.append(f"{msg.key()!r}: {err}")

    for event in events:
        producer.produce(
            topic=topic,
            key=key_serializer(event["user_id"]),
            value=serializer(event, ctx),
            on_delivery=_on_delivery,
        )
    not_delivered = producer.flush(timeout)
    if not_delivered or errors:
        raise RuntimeError(
            f"publishing to {topic!r} via {bootstrap_servers} failed: "
            f"{not_delivered} message(s) not delivered, errors={errors}"
        )
