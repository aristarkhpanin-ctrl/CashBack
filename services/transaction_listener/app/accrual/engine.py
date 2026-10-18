"""AccrualEngine — atomic, single-use cashback accrual (stage C: B45, B21, B22, B25, B26).

One PG transaction per accrual:

    1. ``SELECT … FROM recommendations … FOR UPDATE`` — the accepted offer.
       It must exist and belong to (user, campaign, MCC); an offer that
       already has an accrual, or a purchase that already has one under any
       offer, is a ``duplicate`` (UNIQUE recommendation_id and transaction_id
       — an offer and a purchase pay at most once); otherwise it must be
       ``ACCEPTED`` before the purchase and not expired at the TRANSACTION
       time.
    2. ``SELECT … FROM cashback_campaigns … FOR NO KEY UPDATE`` — serialises
       the budget and the daily limit of the campaign (without blocking the
       FK checks of offers being written for it).
    3. ``eligible_campaigns(user, mcc, amount, transaction_at)`` (migration
       009) — the one definition of "who gets which campaign": status,
       archive, date window, category + its minimum, segments, the latest
       consent, RFM, budget left, daily limit left.
    4. ``calculate_cashback(campaign, mcc, amount)``; 0 → skip.
    5. daily limit and budget — no partial payout.
    6. INSERT the accrual with its ``recommendation_id``; a conflict on
       transaction_id or on recommendation_id → ``duplicate``, the budget is
       not touched; else ``budget_spent += cashback``.

After the commit (best effort — the ledger is already right):
    7. compare-and-delete the Redis hot-path key
       ``accepted_offers:{user_id}:{mcc_code}``: only while it still holds
       THIS offer (mobile_api may already have written a newer one). Also
       after a duplicate of a consumed offer — a lost DEL is retried by the
       next purchase instead of lingering until the TTL;
    8. publish ``cashback.accrued`` (with recommendation_id / transaction_at).

Infrastructure errors (Postgres / Redis down, deadlock, timeout …) are
raised to the caller: the listener does not commit the Kafka offset and
retries, which is safe because a replay ends in ``duplicate``.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
_LOCK_OFFER_SQL = text(
    """
    SELECT user_id::text         AS user_id,
           campaign_id::text     AS campaign_id,
           mcc_code,
           response_status::text AS response_status,
           responded_at,
           expires_at
      FROM recommendations
     WHERE recommendation_id = :rid
     FOR UPDATE
    """
)

# Accruals of THIS offer, or of THIS purchase under any offer (or none: a
# row written before migration 009) — either means "already paid".
_PAID_SQL = text(
    """
    SELECT accrual_id::text        AS accrual_id,
           recommendation_id::text AS recommendation_id
      FROM cashback_accruals
     WHERE recommendation_id = :rid OR transaction_id = :tid
    """
)

# NO KEY UPDATE: conflicts with other accruals and status changes of the
# campaign (UPDATE takes the same lock), not with the FOR KEY SHARE that an
# INSERT of an offer referencing the campaign takes for its FK check.
_LOCK_CAMPAIGN_SQL = text(
    """
    SELECT campaign_id::text AS campaign_id
      FROM cashback_campaigns
     WHERE campaign_id = :cid
     FOR NO KEY UPDATE
    """
)

_ELIGIBLE_SQL = text(
    """
    SELECT budget_total, budget_spent, daily_limit, spent_today
      FROM eligible_campaigns(:uid, :mcc, :amount, :at)
     WHERE campaign_id = :cid
    """
)

_CASHBACK_SQL = text("SELECT calculate_cashback(:cid, :mcc, :amount) AS cashback")

# No conflict target: the PK, (transaction_id, campaign_id), transaction_id
# (rows with an offer) and recommendation_id are all unique — any of them
# means "already paid".
_INSERT_ACCRUAL_SQL = text(
    """
    INSERT INTO cashback_accruals (
        accrual_id, user_id, campaign_id, recommendation_id, transaction_id,
        mcc_code, transaction_amount, cashback_amount, status, accrued_at
    ) VALUES (
        :accrual_id, :user_id, :campaign_id, :recommendation_id, :transaction_id,
        :mcc_code, :transaction_amount, :cashback_amount, 'PENDING', :accrued_at
    )
    ON CONFLICT DO NOTHING
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

# KEYS[1] — offer key, ARGV[1] — recommendation_id. Deletes the key only
# while it still holds this offer; a newer offer on the same user×MCC (or a
# value that is not an offer) is left alone.
_RELEASE_OFFER_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return 0
end
local ok, offer = pcall(cjson.decode, raw)
if ok and type(offer) == 'table' and offer['recommendation_id'] == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _same_uuid(a: Any, b: Any) -> bool:
    try:
        return uuid.UUID(str(a)) == uuid.UUID(str(b))
    except ValueError:
        return False


def _accepted_by(offer: Any, transaction_at: datetime) -> bool:
    """Was the offer accepted no later than the purchase?

    ``responded_at`` (mobile_api: ``now()`` of the accept) is compared at
    whole seconds, the precision producers stamp ``transaction_date`` with
    (tx_simulator, the e2e producer): a purchase in the second of the accept
    counts as after it. Unknown acceptance time → no.
    """
    return (offer.responded_at is not None
            and offer.responded_at.replace(microsecond=0) <= transaction_at)


class AccrualEngine:
    """Idempotent, single-use accrual pipeline."""

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
        recommendation_id: str,
        transaction_id: str,
        mcc: str,
        amount: Decimal,
        transaction_at: datetime,
    ) -> AccrualResult:
        if transaction_at.tzinfo is None:
            raise ValueError("transaction_at must be timezone-aware")
        accrual_id = uuid.uuid4()
        accrued_at = datetime.now(UTC)

        async with self._db.begin() as conn:
            # ---- 1) the accepted offer, locked --------------------------
            offer = (await conn.execute(
                _LOCK_OFFER_SQL, {"rid": recommendation_id},
            )).first()
            if (offer is None
                    or not _same_uuid(offer.user_id, user_id)
                    or not _same_uuid(offer.campaign_id, campaign_id)
                    or offer.mcc_code.strip() != mcc):
                return self._skip("offer_mismatch", recommendation_id=recommendation_id)

            paid = await self._paid(conn, recommendation_id, transaction_id)
            if paid:
                # This offer paid (consumed: its key goes), or this purchase
                # was paid by another offer — then this one stays unpaid.
                result = self._skip("duplicate", recommendation_id=recommendation_id,
                                    transaction_id=transaction_id,
                                    accrual_id=paid[0].accrual_id)
                consumed = self._pays_offer(paid, recommendation_id)
            elif offer.response_status != "ACCEPTED":
                return self._skip("offer_not_accepted", status=offer.response_status)
            elif not _accepted_by(offer, transaction_at):
                # Bought before the accept (processed after it: consumer lag,
                # a replay) — the key stays for a later purchase.
                return self._skip(
                    "accepted_after_purchase",
                    responded_at=offer.responded_at and offer.responded_at.isoformat(),
                    transaction_at=transaction_at.isoformat())
            elif offer.expires_at <= transaction_at:
                return self._skip("offer_expired",
                                  expires_at=offer.expires_at.isoformat())
            else:
                result, consumed = await self._accrue_locked(
                    conn,
                    accrual_id=accrual_id, accrued_at=accrued_at, user_id=user_id,
                    campaign_id=campaign_id, recommendation_id=recommendation_id,
                    transaction_id=transaction_id, mcc=mcc, amount=amount,
                    transaction_at=transaction_at,
                )
        # ─── PG transaction commits here ───

        # The key first: it is cheap, and a slow Kafka (metadata wait) must
        # not keep a consumed offer on the hot path.
        if consumed:
            await self._release_offer(user_id, mcc, recommendation_id)
        if result.accrued:
            await self._publish(result.metadata["event"], user_id)
            ACCRUED.labels(outcome="success").inc()
            log.info(
                "accrual_committed",
                accrual_id=str(result.accrual_id), user_id=user_id,
                campaign_id=campaign_id, recommendation_id=recommendation_id,
                transaction_id=transaction_id, cashback=str(result.cashback_amount),
            )
        return result

    # ------------------------------------------------------------------
    async def _accrue_locked(
        self, conn: Any, *, accrual_id: uuid.UUID, accrued_at: datetime,
        user_id: str, campaign_id: str, recommendation_id: str,
        transaction_id: str, mcc: str, amount: Decimal, transaction_at: datetime,
    ) -> tuple[AccrualResult, bool]:
        """Steps 2–6 under the offer lock → (result, offer consumed?)."""
        # ---- 2) lock the campaign row: budget + daily limit -----------
        await conn.execute(_LOCK_CAMPAIGN_SQL, {"cid": campaign_id})

        # ---- 3) eligibility at the transaction time --------------------
        elig = (await conn.execute(_ELIGIBLE_SQL, {
            "uid": user_id, "mcc": mcc, "amount": amount,
            "at": transaction_at, "cid": campaign_id,
        })).first()
        if elig is None:
            return self._skip("not_eligible", campaign_id=campaign_id), False

        # ---- 4) cashback (category + its minimum, rate tiers) -----------
        cashback = Decimal((await conn.execute(_CASHBACK_SQL, {
            "cid": campaign_id, "mcc": mcc, "amount": amount,
        })).scalar() or 0)
        if cashback <= 0:
            return self._skip("zero_cashback"), False

        # ---- 5) daily limit and budget: all or nothing ------------------
        if (elig.daily_limit is not None
                and Decimal(elig.spent_today) + cashback > Decimal(elig.daily_limit)):
            return self._skip(
                "daily_limit_reached", needed=str(cashback),
                spent_today=str(elig.spent_today), daily_limit=str(elig.daily_limit),
            ), False
        remaining = Decimal(elig.budget_total) - Decimal(elig.budget_spent)
        if cashback > remaining:
            return self._skip("budget_exhausted", remaining=str(remaining),
                              needed=str(cashback)), False

        # ---- 6) the ledger row (one per offer, one per purchase) -------
        inserted = (await conn.execute(_INSERT_ACCRUAL_SQL, {
            "accrual_id": accrual_id,
            "user_id": user_id,
            "campaign_id": campaign_id,
            "recommendation_id": recommendation_id,
            "transaction_id": transaction_id,
            "mcc_code": mcc,
            "transaction_amount": amount,
            "cashback_amount": cashback,
            "accrued_at": accrued_at,
        })).first()
        if inserted is None:
            # Paid meanwhile — the purchase by another offer (a concurrent
            # replay under a newer key) or this offer by a writer that did not
            # take the offer lock; only in the second case is it consumed.
            paid = await self._paid(conn, recommendation_id, transaction_id)
            result = self._skip("duplicate", transaction_id=transaction_id,
                                recommendation_id=recommendation_id)
            return result, self._pays_offer(paid, recommendation_id)

        await conn.execute(_UPDATE_BUDGET_SQL, {"amount": cashback, "cid": campaign_id})
        accrual_id = uuid.UUID(inserted[0])
        event = {
            "accrual_id": str(accrual_id),
            "user_id": user_id,
            "campaign_id": campaign_id,
            "recommendation_id": recommendation_id,
            "transaction_id": transaction_id,
            "mcc_code": mcc,
            "transaction_amount": str(amount),
            "cashback_amount": str(cashback),
            "transaction_at": transaction_at.isoformat(),
            "accrued_at": accrued_at.isoformat(),
        }
        return AccrualResult(accrual_id=accrual_id, cashback_amount=cashback,
                             metadata={"event": event}), True

    @staticmethod
    async def _paid(conn: Any, recommendation_id: str, transaction_id: str) -> list[Any]:
        return list((await conn.execute(
            _PAID_SQL, {"rid": recommendation_id, "tid": transaction_id},
        )).all())

    @staticmethod
    def _pays_offer(paid: list[Any], recommendation_id: str) -> bool:
        return any(_same_uuid(row.recommendation_id, recommendation_id) for row in paid)

    @staticmethod
    def _skip(reason: str, **metadata: Any) -> AccrualResult:
        ACCRUED.labels(outcome=reason).inc()
        return AccrualResult(skipped_reason=reason, metadata=metadata)

    async def _publish(self, event: dict[str, Any], user_id: str) -> None:
        try:
            await self._kafka.send(
                self._accruals_topic,
                key=user_id.encode("utf-8"),
                value=json.dumps(event).encode("utf-8"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("kafka_publish_failed", error=str(exc))

    async def _release_offer(self, user_id: str, mcc: str, recommendation_id: str) -> None:
        key = self._key_fmt.format(user_id=user_id, mcc_code=mcc)
        try:
            await self._redis.eval(_RELEASE_OFFER_LUA, 1, key, recommendation_id)
        except Exception as exc:  # noqa: BLE001
            # The offer is paid in PG; the next purchase ends in duplicate
            # and retries this delete.
            log.warning("redis_delete_failed", key=key, error=str(exc))
