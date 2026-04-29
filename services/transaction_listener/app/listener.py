"""TransactionListener — chapter 3.2, listing 3.11.

Consumes Avro-serialised TransactionEvent records from
``transactions.raw`` (group ``cashback-accrual-group`` — *separate*
from the ETL group), and triggers an accrual when the user has an
active accepted offer for the same MCC.

The hot-path check is O(1) — we look up Redis ``accepted_offers:{user_id}:{mcc_code}``
and short-circuit when no key is present.
"""
from __future__ import annotations

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any, Optional

import structlog
from aiokafka import AIOKafkaConsumer
from confluent_kafka.schema_registry import SchemaRegistryClient
from confluent_kafka.schema_registry.avro import AvroDeserializer
from confluent_kafka.serialization import MessageField, SerializationContext
from prometheus_client import Counter

log = structlog.get_logger("listener")


PROCESSED = Counter(
    "transaction_listener_processed_total",
    "Transactions consumed from Kafka",
    ["outcome"],
)


class TransactionListener:
    BATCH_SIZE: int = 500
    POLL_TIMEOUT_MS: int = 100

    def __init__(
        self,
        settings: Any,
        accrual_engine: Any,
        redis_client: Any,
    ) -> None:
        self._settings = settings
        self._accrual = accrual_engine
        self._redis = redis_client
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._deserializer: Optional[AvroDeserializer] = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    async def start(self) -> None:
        sr = SchemaRegistryClient({"url": self._settings.schema_registry_url})
        self._deserializer = AvroDeserializer(sr)
        self._consumer = AIOKafkaConsumer(
            self._settings.transactions_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.consumer_group_accrual,
            enable_auto_commit=False,
            auto_offset_reset="latest",
            max_poll_records=self.BATCH_SIZE,
        )
        await self._consumer.start()
        log.info(
            "listener_started",
            topic=self._settings.transactions_topic,
            group=self._settings.consumer_group_accrual,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._consumer is not None:
            await self._consumer.stop()
            self._consumer = None

    # ------------------------------------------------------------------
    def _deserialize(self, msg: Any) -> Optional[dict]:
        ctx = SerializationContext(self._settings.transactions_topic, MessageField.VALUE)
        try:
            return self._deserializer(msg.value, ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("deserialise_failed", error=str(exc))
            return None

    async def _process(self, msg: Any) -> None:
        evt = self._deserialize(msg)
        if evt is None:
            PROCESSED.labels(outcome="bad_payload").inc()
            return

        user_id = str(evt.get("user_id"))
        mcc = str(evt.get("mcc_code")).strip()
        if not user_id or not mcc:
            PROCESSED.labels(outcome="missing_fields").inc()
            return

        # ---- O(1) Redis check -----------------------------------------
        key = self._settings.accepted_offer_key_fmt.format(
            user_id=user_id, mcc_code=mcc,
        )
        offer_payload = await self._redis.get(key)
        if not offer_payload:
            PROCESSED.labels(outcome="no_offer").inc()
            return

        try:
            offer = json.loads(offer_payload)
        except (TypeError, ValueError):
            log.warning("offer_payload_corrupt", key=key)
            PROCESSED.labels(outcome="bad_offer").inc()
            return

        try:
            amount = Decimal(str(evt.get("amount")))
        except Exception:  # noqa: BLE001
            PROCESSED.labels(outcome="bad_amount").inc()
            return

        try:
            await self._accrual.accrue(
                user_id=user_id,
                campaign_id=offer["campaign_id"],
                transaction_id=str(evt["transaction_id"]),
                mcc=mcc,
                amount=amount,
            )
            PROCESSED.labels(outcome="accrued").inc()
        except Exception as exc:  # noqa: BLE001
            log.error("accrue_failed", error=str(exc), user_id=user_id, mcc=mcc)
            PROCESSED.labels(outcome="accrue_failed").inc()

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
                for _, msgs in batch.items():
                    for msg in msgs:
                        await self._process(msg)
                await self._consumer.commit()
        finally:
            await self.stop()
