"""Rows the accrual path reads, written the way the services write them.

* campaigns / categories — as campaign_manager stores them;
* users + ``personalised_cashback`` consents — as the seeder / mobile app;
* an accepted offer = a ``recommendations`` row in ACCEPTED plus the Redis
  hot-path key with the payload mobile_api writes on respond
  (``{"campaign_id","recommendation_id","accepted_at","activation_id"}``).
  It was accepted a week ago unless a test says otherwise
  (``accepted_at``): the listener pays only purchases made after the accept.

Every call creates fresh ids, so tests sharing the session database do not
see each other's campaigns, budgets or daily limits.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import text

KEY_FMT = "accepted_offers:{user_id}:{mcc_code}"


@dataclass(frozen=True)
class Offer:
    user_id: str
    campaign_id: str
    recommendation_id: str
    mcc: str
    key: str


class Seed:
    def __init__(self, db: Any, redis: Any) -> None:
        self.db = db
        self.redis = redis

    # ------------------------------------------------------------------
    async def user(self, *, segment_id: Optional[int] = 5, rfm_score: Optional[int] = None,
                   consent: Optional[str] = "GRANTED") -> str:
        user_id = str(uuid.uuid4())
        async with self.db.begin() as conn:
            await conn.execute(text(
                "INSERT INTO users (user_id, external_id, segment_id, rfm_score) "
                "VALUES (:u, :ext, :seg, :rfm)"),
                {"u": user_id, "ext": f"tl-it-{user_id}", "seg": segment_id,
                 "rfm": rfm_score})
        if consent is not None:
            await self.consent(user_id, consent)
        return user_id

    async def consent(self, user_id: str, status: str,
                      created_at: Optional[datetime] = None) -> None:
        async with self.db.begin() as conn:
            await conn.execute(text(
                "INSERT INTO user_consents (user_id, consent_type, status, "
                "document_version, created_at) "
                "VALUES (:u, 'personalised_cashback', CAST(:s AS consent_status), "
                "'v1', :at)"),
                {"u": user_id, "s": status, "at": created_at or datetime.now(UTC)})

    async def campaign(
        self, *, rate: str = "5.00", min_amount: Optional[str] = "100",
        budget: str = "100000", daily_limit: Optional[str] = None,
        status: str = "ACTIVE", start: Optional[datetime] = None,
        end: Optional[datetime] = None, segments: Iterable[int] = (5,),
        rfm_min: Optional[int] = None, rfm_max: Optional[int] = None,
        mccs: Optional[dict[str, Optional[str]]] = None, archived: bool = False,
    ) -> str:
        now = datetime.now(UTC)
        campaign_id = str(uuid.uuid4())
        async with self.db.begin() as conn:
            await conn.execute(text(
                """INSERT INTO cashback_campaigns
                       (campaign_id, name, target_segment_ids, cashback_rate,
                        min_transaction_amount, budget_total, status, start_date,
                        end_date, allowed_channels, daily_limit, rfm_min, rfm_max,
                        archived_at)
                   VALUES (:c, :name, :segs, :rate, :min, :budget,
                           CAST(:status AS campaign_status), :start, :end,
                           ARRAY['POS'], :daily, :rfm_min, :rfm_max, :archived)"""),
                {"c": campaign_id, "name": f"tl-it-{campaign_id[:8]}",
                 "segs": list(segments), "rate": Decimal(rate),
                 "min": None if min_amount is None else Decimal(min_amount),
                 "budget": Decimal(budget), "status": status,
                 "start": start or now - timedelta(days=1),
                 "end": end or now + timedelta(days=30),
                 "daily": None if daily_limit is None else Decimal(daily_limit),
                 "rfm_min": rfm_min, "rfm_max": rfm_max,
                 "archived": now if archived else None})
            for mcc, cat_min in (mccs or {"5411": None}).items():
                await conn.execute(text(
                    "INSERT INTO campaign_categories (campaign_id, mcc_code, "
                    "min_transaction_amount) VALUES (:c, :m, :min)"),
                    {"c": campaign_id, "m": mcc,
                     "min": None if cat_min is None else Decimal(cat_min)})
        return campaign_id

    async def recommendation(self, user_id: str, campaign_id: str, *, mcc: str = "5411",
                             status: str = "ACCEPTED",
                             expires_at: Optional[datetime] = None,
                             accepted_at: Optional[datetime] = None) -> str:
        """``accepted_at`` → ``responded_at`` (mobile_api: ``now()`` of the accept)."""
        rec_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        async with self.db.begin() as conn:
            await conn.execute(text(
                """INSERT INTO recommendations
                       (recommendation_id, user_id, campaign_id, mcc_code, model_score,
                        response_status, expires_at, responded_at)
                   VALUES (:r, :u, :c, :m, 0.9,
                           CAST(:s AS recommendation_response_status), :exp, :resp)"""),
                {"r": rec_id, "u": user_id, "c": campaign_id, "m": mcc, "s": status,
                 "exp": expires_at or now + timedelta(days=7),
                 "resp": accepted_at or now - timedelta(days=7)})
        return rec_id

    async def activate(self, user_id: str, mcc: str, campaign_id: str,
                       recommendation_id: Optional[str]) -> str:
        """The Redis key mobile_api writes on ACCEPTED (``None`` → legacy payload)."""
        key = KEY_FMT.format(user_id=user_id, mcc_code=mcc)
        payload: dict[str, Any] = {"campaign_id": campaign_id,
                                   "accepted_at": datetime.now(UTC).isoformat(),
                                   "activation_id": uuid.uuid4().hex}
        if recommendation_id is not None:
            payload["recommendation_id"] = recommendation_id
        await self.redis.set(key, json.dumps(payload), ex=3600)
        return key

    async def accepted_offer(self, campaign_id: str, *, mcc: str = "5411",
                             user_id: Optional[str] = None,
                             expires_at: Optional[datetime] = None,
                             accepted_at: Optional[datetime] = None,
                             status: str = "ACCEPTED") -> Offer:
        user_id = user_id or await self.user()
        rec_id = await self.recommendation(user_id, campaign_id, mcc=mcc,
                                           status=status, expires_at=expires_at,
                                           accepted_at=accepted_at)
        key = await self.activate(user_id, mcc, campaign_id, rec_id)
        return Offer(user_id, campaign_id, rec_id, mcc, key)

    # ------------------------------------------------------------------
    async def accruals(self, *, campaign_id: Optional[str] = None,
                       recommendation_id: Optional[str] = None) -> list[Any]:
        where, params = [], {}
        if campaign_id is not None:
            where.append("campaign_id = :c")
            params["c"] = campaign_id
        if recommendation_id is not None:
            where.append("recommendation_id = :r")
            params["r"] = recommendation_id
        async with self.db.connect() as conn:
            return list((await conn.execute(text(
                "SELECT accrual_id::text AS accrual_id, user_id::text AS user_id,"
                " campaign_id::text AS campaign_id,"
                " recommendation_id::text AS recommendation_id, transaction_id,"
                " mcc_code, transaction_amount, cashback_amount, accrued_at"
                " FROM cashback_accruals WHERE " + " AND ".join(where)
                + " ORDER BY accrued_at"), params)).all())

    async def budget_spent(self, campaign_id: str) -> Decimal:
        async with self.db.connect() as conn:
            return (await conn.execute(text(
                "SELECT budget_spent FROM cashback_campaigns WHERE campaign_id = :c"),
                {"c": campaign_id})).scalar_one()
