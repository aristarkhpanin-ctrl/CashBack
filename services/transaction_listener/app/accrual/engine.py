"""AccrualEngine — atomic cashback accrual.

Pipeline (one PG transaction):

    1. compute cashback via stored function ``calculate_cashback``
       (rate_tiers, see Alembic 001_init_oltp.py)
    2. ``SELECT … FOR UPDATE`` on cashback_campaigns to lock the budget row
    3. INSERT cashback_accruals (idempotent on (transaction_id, campaign_id))
    4. UPDATE budget_spent += cashback

After the commit:
    5. publish ``cashback.accrued`` event to Kafka
    6. DELETE the Redis hot-path key ``accepted_offers:{user_id}:{mcc_code}``
       so the same offer can't be accrued twice.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import structlog
from prometheus_client import Counter
from sqlalchemy import text

log = structlog.get_logger("accrual")


ACCRUED = Counter(
    "accrual_engine_total",
    "Cashback accrual outcomes",
    ["outcome"],
)


@dataclass
class AccrualResult:
    accrual_id: Optional[uuid.UUID] = None
    cashback_amount: Decimal = Decimal("0")
    skipped_reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def accrued(self) -> bool:
        return self.accrual_id is not None


# ---------------------------------------------------------------------------
_CASHBACK_SQL = text("SELECT calculate_cashback(:cid, :amount) AS cashback")

_LOCK_BUDGET_SQL = text(
    """
    SELECT campaign_id::text  AS campaign_id,
           budget_total,
           budget_spent,
           status::text       AS status
      FROM cashback_campaigns
     WHERE campaign_id = :cid
     FOR UPDATE
    """
)

_INSERT_ACCRUAL_SQL = text(
    """
    INSERT INTO cashback_accruals (
        accrual_id, user_id, campaign_id, transaction_id,
        mcc_code, transaction_amount, cashback_amount, status, accrued_at
    ) VALUES (
        :accrual_id, :user_id, :campaign_id, :transaction_id,
        :mcc_code, :transaction_amount, :cashback_amount, 'PENDING', :accrued_at
    )
    ON CONFLICT (transaction_id, campaign_id) DO NOTHING
    RETURNING accrual_id::text
    """
)

_UPDATE_BUDGET_SQL = text(
    """
    UPDATE cashback_campaigns
       SET budget_spent = budget_spent + :amount
     WHERE campaign_id = :cid
    """
)


class AccrualEngine:
    """Idempotent accrual pipeline."""

    def __init__(
        self,
        db_engine: Any,
        kafka_producer: Any,
        redis_client: Any,
        *,
        accruals_topic: str = "cashback.accrued",
        accepted_offer_key_fmt: str = "accepted_offers:{user_id}:{mcc_code}",
    ) -> None:
        self._db = db_engine
        self._kafka = kafka_producer
        self._redis = redis_client
        self._accruals_topic = accruals_topic
        self._key_fmt = accepted_offer_key_fmt

    # ------------------------------------------------------------------
    async def accrue(
        self,
        *,
        user_id: str,
        campaign_id: str,
        transaction_id: str,
        mcc: str,
        amount: Decimal,
    ) -> AccrualResult:
        accrual_id = uuid.uuid4()

        async with self._db.begin() as conn:
            # ---- 1) compute cashback via stored function ---------------
            cashback_row = (
                await conn.execute(_CASHBACK_SQL, {"cid": campaign_id, "amount": amount})
            ).first()
            cashback = Decimal(cashback_row.cashback or 0)
            if cashback <= 0:
                ACCRUED.labels(outcome="zero_cashback").inc()
                return AccrualResult(skipped_reason="cashback=0")

            # ---- 2) lock budget row ----------------------------------
            row = (await conn.execute(_LOCK_BUDGET_SQL, {"cid": campaign_id})).first()
            if row is None:
                ACCRUED.labels(outcome="no_campaign").inc()
                return AccrualResult(skipped_reason="campaign_not_found")
            if row.status != "ACTIVE":
                ACCRUED.labels(outcome="not_active").inc()
                return AccrualResult(skipped_reason=f"status={row.status}")

            remaining = Decimal(row.budget_total) - Decimal(row.budget_spent)
            if cashback > remaining:
                ACCRUED.labels(outcome="budget_exhausted").inc()
                return AccrualResult(
                    skipped_reason="budget_exhausted",
                    metadata={"remaining": float(remaining), "needed": float(cashback)},
                )

            # ---- 3) insert accrual (idempotent) ----------------------
            insert_row = (await conn.execute(
                _INSERT_ACCRUAL_SQL,
                {
                    "accrual_id": accrual_id,
                    "user_id": user_id,
                    "campaign_id": campaign_id,
                    "transaction_id": transaction_id,
                    "mcc_code": mcc,
                    "transaction_amount": amount,
                    "cashback_amount": cashback,
                    "accrued_at": datetime.now(UTC),
                },
            )).first()
            if insert_row is None:
                # Conflict — already accrued. Don't double-debit budget.
                ACCRUED.labels(outcome="duplicate").inc()
                return AccrualResult(
                    skipped_reason="duplicate_transaction",
                    metadata={"transaction_id": transaction_id},
                )
            accrual_id = uuid.UUID(insert_row[0])

            # ---- 4) increment budget_spent ---------------------------
            await conn.execute(
                _UPDATE_BUDGET_SQL, {"amount": cashback, "cid": campaign_id}
            )
        # ─── PG transaction commits here ───

        # ---- 5) publish event ----------------------------------------
        event = {
            "accrual_id": str(accrual_id),
            "user_id": user_id,
            "campaign_id": campaign_id,
            "transaction_id": transaction_id,
            "mcc_code": mcc,
            "transaction_amount": str(amount),
            "cashback_amount": str(cashback),
            "accrued_at": datetime.now(UTC).isoformat(),
        }
        try:
            payload = json.dumps(event).encode("utf-8")
            await self._kafka.send(
                self._accruals_topic,
                key=user_id.encode("utf-8"),
                value=payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("kafka_publish_failed", error=str(exc))

        # ---- 6) delete Redis hot-path key -----------------------------
        try:
            key = self._key_fmt.format(user_id=user_id, mcc_code=mcc)
            await self._redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis_delete_failed", error=str(exc))

        ACCRUED.labels(outcome="success").inc()
        log.info(
            "accrual_committed",
            accrual_id=str(accrual_id), user_id=user_id, campaign_id=campaign_id,
            transaction_id=transaction_id, cashback=str(cashback),
        )
        return AccrualResult(
            accrual_id=accrual_id, cashback_amount=cashback,
            metadata={"event": event},
        )
