"""Kafka helpers: publish TransactionEvents like tx_simulator, read topics.

Values are Confluent wire-format Avro (``AvroSerializer`` + Schema Registry,
schema ``infrastructure/kafka/schemas/transaction_event.avsc``), zstd
compressed, keyed by ``user_id`` — exactly what the listener decodes in
production.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_PATH = (Path(__file__).resolve().parents[4]
               / "infrastructure" / "kafka" / "schemas" / "transaction_event.avsc")


def transaction_event(*, user_id: str, mcc_code: str = "5411", amount: str = "1500.00",
                      transaction_id: Optional[str] = None,
                      transaction_date: Optional[str] = None) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id or f"tx-it-{uuid.uuid4().hex}",
        "user_id": user_id,
        "mcc_code": mcc_code,
        "amount": amount,
        "currency": "RUB",
        "transaction_date": transaction_date or datetime.now(UTC).isoformat(),
        "channel": "POS",
        "merchant_id": "M-IT",
        "metadata": None,
    }


def _publish_avro(bootstrap: str, registry_url: str, topic: str,
                  events: Iterable[dict[str, Any]], partition: Optional[int]) -> None:
    from confluent_kafka import Producer
    from confluent_kafka.schema_registry import SchemaRegistryClient
    from confluent_kafka.schema_registry.avro import AvroSerializer
    from confluent_kafka.serialization import MessageField, SerializationContext

    serializer = AvroSerializer(SchemaRegistryClient({"url": registry_url}),
                                SCHEMA_PATH.read_text(encoding="utf-8"))
    ctx = SerializationContext(topic, MessageField.VALUE)
    producer = Producer({"bootstrap.servers": bootstrap, "compression.type": "zstd"})
    errors: list[Any] = []
    for evt in events:
        kwargs = {} if partition is None else {"partition": partition}
        producer.produce(topic, key=evt["user_id"].encode("utf-8"),
                         value=serializer(evt, ctx),
                         on_delivery=lambda err, _msg: err and errors.append(err),
                         **kwargs)
    remaining = producer.flush(30)
    if remaining or errors:
        raise RuntimeError(f"publish failed: {remaining} unsent, errors={errors}")


async def publish_avro(bootstrap: str, registry_url: str, topic: str,
                       events: Iterable[dict[str, Any]], *,
                       partition: Optional[int] = None) -> None:
    await asyncio.to_thread(_publish_avro, bootstrap, registry_url, topic,
                            list(events), partition)


async def publish_raw(bootstrap: str, topic: str, value: bytes, *,
                      key: Optional[bytes] = None) -> None:
    from aiokafka import AIOKafkaProducer

    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await producer.start()
    try:
        await producer.send_and_wait(topic, value=value, key=key)
    finally:
        await producer.stop()


async def create_topic(bootstrap: str, topic: str, partitions: int = 1) -> None:
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(topic, num_partitions=partitions,
                                            replication_factor=1)])
    finally:
        await admin.close()


async def read_all(bootstrap: str, topic: str, *, expect: int,
                   timeout: float = 30.0) -> list[Any]:
    """Records of ``topic`` from the beginning, once ``expect`` are there."""
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(topic, bootstrap_servers=bootstrap,
                                group_id=None, auto_offset_reset="earliest",
                                enable_auto_commit=False)
    await consumer.start()
    records: list[Any] = []
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(records) < expect and loop.time() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            for msgs in batch.values():
                records.extend(msgs)
    finally:
        await consumer.stop()
    return records


async def committed(bootstrap: str, group: str, topic: str,
                    partitions: int = 1) -> dict[int, Optional[int]]:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    consumer = AIOKafkaConsumer(bootstrap_servers=bootstrap, group_id=group,
                                enable_auto_commit=False)
    await consumer.start()
    try:
        return {p: await consumer.committed(TopicPartition(topic, p))
                for p in range(partitions)}
    finally:
        await consumer.stop()
