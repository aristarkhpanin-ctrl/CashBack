"""AccrualEngine on Postgres at alembic head + real Redis (stage C §2, §7).

The money rules are the production SQL (``eligible_campaigns``,
``calculate_cashback`` of migration 009), so every test here would pass or
fail exactly like the deployed listener:

* an accepted offer pays at most once — a second purchase, a redelivered
  transaction, two concurrent purchases, a lost Redis DEL;
* a purchase pays at most once — a replay under another campaign's offer,
  concurrent replays under different offers;
* the offer must be ACCEPTED before the purchase, not expired at the
  TRANSACTION time and belong to (user, campaign, MCC);
* eligibility at the transaction time: date window, status / archive, MCC
  category and its minimum, segment, the latest consent, RFM;
* daily limit and budget under the campaign row lock — 20 concurrent
  purchases never exceed the daily limit, a purchase dated tomorrow sees
  today's spending; the lock does not block offers written for the campaign;
* the Redis key is compare-and-deleted: a newer offer on the same key survives.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from app.accrual import engine as engine_module
from app.accrual.engine import AccrualEngine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.integration.seed import Seed

pytestmark = pytest.mark.integration


class RecordingProducer:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, topic: str, *, key=None, value=None) -> None:
        self.sent.append({"topic": topic, "key": key.decode("utf-8"),
                          "value": json.loads(value.decode("utf-8"))})


@pytest.fixture()
async def env(db, redis):
    producer = RecordingProducer()
    return Seed(db, redis), AccrualEngine(db, producer, redis), producer


def _now() -> datetime:
    return datetime.now(UTC)


async def _accrue(engine, offer, *, amount: str = "1500.00", tx: str | None = None,
                  at: datetime | None = None, mcc: str | None = None,
                  user_id: str | None = None, campaign_id: str | None = None):
    return await engine.accrue(
        user_id=user_id or offer.user_id,
        campaign_id=campaign_id or offer.campaign_id,
        recommendation_id=offer.recommendation_id,
        transaction_id=tx or f"tx-{uuid.uuid4().hex}",
        mcc=mcc or offer.mcc,
        amount=Decimal(amount),
        transaction_at=at or _now(),
    )


# ---------------------------------------------------------------------------
# single-use offer
# ---------------------------------------------------------------------------
async def test_accepted_offer_accrues_and_consumes_the_key(env):
    seed, engine, producer = env
    cid = await seed.campaign(rate="5.00")
    offer = await seed.accepted_offer(cid)
    at = _now() - timedelta(minutes=5)

    result = await _accrue(engine, offer, tx="tx-one", at=at)

    assert result.accrued, result
    assert result.cashback_amount == Decimal("75.00")
    [row] = await seed.accruals(campaign_id=cid)
    assert row.recommendation_id == offer.recommendation_id
    assert row.transaction_id == "tx-one"
    assert row.cashback_amount == Decimal("75.00")
    assert await seed.budget_spent(cid) == Decimal("75.00")
    assert await seed.redis.get(offer.key) is None
    [event] = producer.sent
    assert event["topic"] == "cashback.accrued"
    assert event["value"]["recommendation_id"] == offer.recommendation_id
    assert event["value"]["transaction_at"] == at.isoformat()
    assert event["value"]["cashback_amount"] == "75.00"


async def test_second_purchase_after_a_lost_del_is_a_duplicate(env):
    """The Redis DEL after the commit was lost: the key still says "accepted".
    The next purchase must not be paid, and it retries the delete."""
    seed, engine, producer = env
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)
    assert (await _accrue(engine, offer)).accrued
    await seed.activate(offer.user_id, offer.mcc, cid, offer.recommendation_id)

    second = await _accrue(engine, offer, amount="3000.00")

    assert not second.accrued
    assert second.skipped_reason == "duplicate"
    assert len(await seed.accruals(campaign_id=cid)) == 1
    assert await seed.budget_spent(cid) == Decimal("75.00")
    assert await seed.redis.get(offer.key) is None
    assert len(producer.sent) == 1


async def test_redelivered_transaction_is_a_duplicate(env):
    seed, engine, producer = env
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)
    at = _now()
    assert (await _accrue(engine, offer, tx="tx-redelivered", at=at)).accrued

    again = await _accrue(engine, offer, tx="tx-redelivered", at=at)

    assert again.skipped_reason == "duplicate"
    assert len(await seed.accruals(campaign_id=cid)) == 1
    assert await seed.budget_spent(cid) == Decimal("75.00")
    assert len(producer.sent) == 1


async def test_concurrent_purchases_on_one_offer_pay_once(env):
    seed, engine, _ = env
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)

    results = await asyncio.gather(*(_accrue(engine, offer) for _ in range(5)))

    assert sorted(r.accrued for r in results) == [False] * 4 + [True]
    assert {r.skipped_reason for r in results if not r.accrued} == {"duplicate"}
    assert len(await seed.accruals(recommendation_id=offer.recommendation_id)) == 1
    assert await seed.budget_spent(cid) == Decimal("75.00")


async def test_duplicate_keeps_a_newer_offer_on_the_same_key(env):
    """Compare-and-delete: after offer 1 was paid the user accepted offer 2
    for the same MCC; a replay of the old purchase must not delete it."""
    seed, engine, _ = env
    cid = await seed.campaign()
    first = await seed.accepted_offer(cid)
    at = _now()
    assert (await _accrue(engine, first, tx="tx-old", at=at)).accrued
    second_rec = await seed.recommendation(first.user_id, cid)
    await seed.activate(first.user_id, first.mcc, cid, second_rec)

    replay = await _accrue(engine, first, tx="tx-old", at=at)

    assert replay.skipped_reason == "duplicate"
    stored = json.loads(await seed.redis.get(first.key))
    assert stored["recommendation_id"] == second_rec


async def test_same_transaction_under_a_new_offer_does_not_consume_it(env):
    """(transaction_id, campaign_id) was paid by offer 1; the replay meets
    offer 2's key: a duplicate, but offer 2 stays unpaid and keeps its key."""
    seed, engine, _ = env
    cid = await seed.campaign()
    first = await seed.accepted_offer(cid)
    at = _now()
    assert (await _accrue(engine, first, tx="tx-shared", at=at)).accrued
    second = await seed.accepted_offer(cid, user_id=first.user_id)

    replay = await _accrue(engine, second, tx="tx-shared", at=at)

    assert replay.skipped_reason == "duplicate"
    assert await seed.accruals(recommendation_id=second.recommendation_id) == []
    stored = json.loads(await seed.redis.get(second.key))
    assert stored["recommendation_id"] == second.recommendation_id
    # … and a real purchase still pays it.
    assert (await _accrue(engine, second)).accrued


async def test_replay_under_another_campaigns_offer_is_a_duplicate(env):
    """Kafka redelivers a paid purchase after the user's key moved to an
    offer of ANOTHER campaign for the same MCC: (transaction_id, campaign_id)
    and recommendation_id are both new — the purchase itself is the key."""
    seed, engine, producer = env
    first_cid, second_cid = await seed.campaign(), await seed.campaign()
    first = await seed.accepted_offer(first_cid)
    second = await seed.accepted_offer(second_cid, user_id=first.user_id)
    at = _now()
    assert (await _accrue(engine, first, tx="tx-once", at=at)).accrued

    replay = await _accrue(engine, second, tx="tx-once", at=at)

    assert replay.skipped_reason == "duplicate"
    async with seed.db.connect() as conn:
        assert (await conn.execute(text(
            "SELECT count(*) FROM cashback_accruals WHERE transaction_id = 'tx-once'"),
        )).scalar_one() == 1
    assert await seed.budget_spent(second_cid) == Decimal("0")
    assert len(producer.sent) == 1
    # the newer offer is unpaid: it keeps its key and pays a real purchase
    assert json.loads(await seed.redis.get(second.key))["recommendation_id"] == (
        second.recommendation_id)
    assert (await _accrue(engine, second)).accrued


async def test_concurrent_replays_under_different_offers_pay_once(env):
    """Two consumers of one partition during a rebalance, each with another
    offer for the same purchase: the UNIQUE on transaction_id decides."""
    seed, engine, _ = env
    user_id = await seed.user()
    offers = [await seed.accepted_offer(await seed.campaign(), user_id=user_id)
              for _ in range(5)]
    at = _now()

    results = await asyncio.gather(*(_accrue(engine, o, tx="tx-race", at=at)
                                     for o in offers))

    assert sorted(r.accrued for r in results) == [False] * 4 + [True]
    assert {r.skipped_reason for r in results if not r.accrued} == {"duplicate"}
    spent = [await seed.budget_spent(o.campaign_id) for o in offers]
    assert sorted(spent) == [Decimal("0")] * 4 + [Decimal("75.00")]


async def test_release_is_a_compare_and_delete_on_real_redis(env):
    """The Lua script deletes the key only while it holds this offer."""
    seed, engine, _ = env
    user, rid, newer = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    key = f"accepted_offers:{user}:5411"

    await seed.redis.set(key, json.dumps({"campaign_id": "c", "recommendation_id": newer}))
    await engine._release_offer(user, "5411", rid)
    assert json.loads(await seed.redis.get(key))["recommendation_id"] == newer

    await seed.redis.set(key, "not an offer")
    await engine._release_offer(user, "5411", rid)
    assert await seed.redis.get(key) == "not an offer"

    await seed.redis.set(key, json.dumps({"campaign_id": "c", "recommendation_id": rid}))
    await engine._release_offer(user, "5411", rid)
    assert await seed.redis.get(key) is None

    await engine._release_offer(user, "5411", rid)          # no key: a no-op
    await seed.redis.delete(key)


# ---------------------------------------------------------------------------
# the offer row
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["PENDING", "DECLINED", "SNOOZE", "EXPIRED"])
async def test_offer_that_is_not_accepted_is_not_paid(env, status):
    seed, engine, _ = env
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid, status=status)

    result = await _accrue(engine, offer)

    assert result.skipped_reason == "offer_not_accepted"
    assert await seed.accruals(campaign_id=cid) == []
    # Not consumed: the key is left alone (mobile_api owns it).
    assert await seed.redis.get(offer.key) is not None


async def test_purchase_before_the_accept_is_not_paid_and_keeps_the_key(env):
    """Bought with no accepted offer, processed after the accept (consumer
    lag, a listener or Postgres outage, a replay from 'earliest')."""
    seed, engine, producer = env
    cid = await seed.campaign()
    accepted = _now() - timedelta(minutes=10)
    offer = await seed.accepted_offer(cid, accepted_at=accepted)

    before = await _accrue(engine, offer, at=accepted - timedelta(seconds=2))

    assert before.skipped_reason == "accepted_after_purchase"
    assert await seed.accruals(campaign_id=cid) == []
    assert await seed.redis.get(offer.key) is not None
    # the same second as the accept (transaction_date has whole seconds) and
    # later purchases are after it
    same_second = accepted.replace(microsecond=0)
    assert (await _accrue(engine, offer, at=same_second)).accrued
    assert producer.sent[0]["value"]["transaction_at"] == same_second.isoformat()


async def test_accepted_offer_without_an_acceptance_time_is_not_paid(env):
    seed, engine, _ = env
    cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)
    async with seed.db.begin() as conn:
        await conn.execute(text("UPDATE recommendations SET responded_at = NULL "
                                "WHERE recommendation_id = :r"),
                           {"r": offer.recommendation_id})

    assert (await _accrue(engine, offer)).skipped_reason == "accepted_after_purchase"


async def test_expiry_is_judged_at_the_transaction_time(env):
    seed, engine, _ = env
    cid = await seed.campaign()
    expires = _now() - timedelta(minutes=10)
    late = await seed.accepted_offer(cid, expires_at=expires)
    # bought before the offer expired, processed after (consumer lag) → paid
    on_time = await seed.accepted_offer(cid, expires_at=expires)

    expired = await _accrue(engine, late, at=expires + timedelta(seconds=1))
    paid = await _accrue(engine, on_time, at=expires - timedelta(seconds=1))

    assert expired.skipped_reason == "offer_expired"
    assert paid.accrued
    assert [r.recommendation_id for r in await seed.accruals(campaign_id=cid)] == [
        on_time.recommendation_id]


async def test_offer_must_belong_to_the_purchase(env):
    seed, engine, _ = env
    cid = await seed.campaign(mccs={"5411": None, "5812": None})
    other_cid = await seed.campaign()
    offer = await seed.accepted_offer(cid)
    stranger = await seed.user()

    results = [
        await _accrue(engine, offer, user_id=stranger),       # someone else's offer
        await _accrue(engine, offer, campaign_id=other_cid),  # other campaign
        await _accrue(engine, offer, mcc="5812"),             # other MCC
    ]
    ghost = await engine.accrue(
        user_id=offer.user_id, campaign_id=cid, recommendation_id=str(uuid.uuid4()),
        transaction_id="tx-ghost", mcc="5411", amount=Decimal("1500"),
        transaction_at=_now(),
    )

    assert [r.skipped_reason for r in [*results, ghost]] == ["offer_mismatch"] * 4
    assert await seed.accruals(campaign_id=cid) == []
    assert await seed.accruals(campaign_id=other_cid) == []


# ---------------------------------------------------------------------------
# eligibility at the transaction time (eligible_campaigns)
# ---------------------------------------------------------------------------
async def test_date_window_uses_the_transaction_time(env):
    seed, engine, _ = env
    start, end = _now() - timedelta(days=2), _now() - timedelta(hours=1)
    cid = await seed.campaign(start=start, end=end)
    before, after, inside = [await seed.accepted_offer(cid) for _ in range(3)]

    r_before = await _accrue(engine, before, at=start - timedelta(seconds=1))
    r_after = await _accrue(engine, after, at=end)
    r_inside = await _accrue(engine, inside, at=end - timedelta(seconds=1))

    assert r_before.skipped_reason == "not_eligible"
    assert r_after.skipped_reason == "not_eligible"
    assert r_inside.accrued
    assert len(await seed.accruals(campaign_id=cid)) == 1


@pytest.mark.parametrize("status,archived", [("PAUSED", False), ("COMPLETED", False),
                                             ("DRAFT", False), ("ACTIVE", True)])
async def test_campaign_that_is_not_running_does_not_pay(env, status, archived):
    seed, engine, _ = env
    cid = await seed.campaign(status=status, archived=archived)
    offer = await seed.accepted_offer(cid)

    assert (await _accrue(engine, offer)).skipped_reason == "not_eligible"
    assert await seed.budget_spent(cid) == Decimal("0")


async def test_revoked_or_missing_consent_is_not_paid(env):
    seed, engine, _ = env
    cid = await seed.campaign()
    revoked_user = await seed.user()
    await seed.consent(revoked_user, "REVOKED", created_at=_now() + timedelta(seconds=1))
    regranted_user = await seed.user(consent="REVOKED")
    await seed.consent(regranted_user, "GRANTED", created_at=_now() + timedelta(seconds=1))
    no_consent_user = await seed.user(consent=None)
    revoked = await seed.accepted_offer(cid, user_id=revoked_user)
    regranted = await seed.accepted_offer(cid, user_id=regranted_user)
    no_consent = await seed.accepted_offer(cid, user_id=no_consent_user)

    assert (await _accrue(engine, revoked)).skipped_reason == "not_eligible"
    assert (await _accrue(engine, no_consent)).skipped_reason == "not_eligible"
    assert (await _accrue(engine, regranted)).accrued   # the LATEST consent counts


async def test_segment_targeting(env):
    seed, engine, _ = env
    cid = await seed.campaign(segments=(5,))
    wrong = await seed.accepted_offer(cid, user_id=await seed.user(segment_id=3))
    right = await seed.accepted_offer(cid, user_id=await seed.user(segment_id=5))

    assert (await _accrue(engine, wrong)).skipped_reason == "not_eligible"
    assert (await _accrue(engine, right)).accrued


async def test_rfm_range_is_fail_closed(env):
    seed, engine, _ = env
    cid = await seed.campaign(rfm_min=3, rfm_max=4)
    low = await seed.accepted_offer(cid, user_id=await seed.user(rfm_score=2))
    unknown = await seed.accepted_offer(cid, user_id=await seed.user(rfm_score=None))
    inside = await seed.accepted_offer(cid, user_id=await seed.user(rfm_score=4))

    assert (await _accrue(engine, low)).skipped_reason == "not_eligible"
    assert (await _accrue(engine, unknown)).skipped_reason == "not_eligible"
    assert (await _accrue(engine, inside)).accrued


async def test_per_category_minimum(env):
    """The category minimum (500) wins over the campaign minimum (100)."""
    seed, engine, _ = env
    cid = await seed.campaign(min_amount="100", mccs={"5411": "500", "5812": None})
    below = await seed.accepted_offer(cid, mcc="5411")
    above = await seed.accepted_offer(cid, mcc="5411")
    other_category = await seed.accepted_offer(cid, mcc="5812")

    r_below = await _accrue(engine, below, amount="300.00")
    r_above = await _accrue(engine, above, amount="600.00")
    r_other = await _accrue(engine, other_category, amount="300.00")

    assert not r_below.accrued
    assert r_below.skipped_reason in {"not_eligible", "zero_cashback"}
    assert r_above.cashback_amount == Decimal("30.00")
    assert r_other.cashback_amount == Decimal("15.00")   # campaign minimum 100
    assert await seed.budget_spent(cid) == Decimal("45.00")


async def test_mcc_outside_the_campaign_categories_is_not_paid(env):
    """An offer row for an MCC the campaign does not cover (categories
    changed after the offer was made) pays nothing."""
    seed, engine, _ = env
    cid = await seed.campaign(mccs={"5812": None})
    offer = await seed.accepted_offer(cid, mcc="5411")

    assert (await _accrue(engine, offer)).skipped_reason == "not_eligible"


# ---------------------------------------------------------------------------
# daily limit and budget
# ---------------------------------------------------------------------------
async def test_daily_limit_pays_nothing_partial(env):
    seed, engine, _ = env
    cid = await seed.campaign(daily_limit="100")
    first, second = [await seed.accepted_offer(cid) for _ in range(2)]

    assert (await _accrue(engine, first)).cashback_amount == Decimal("75.00")
    over = await _accrue(engine, second)          # 75 + 75 > 100

    assert over.skipped_reason == "daily_limit_reached"
    assert await seed.budget_spent(cid) == Decimal("75.00")
    assert await seed.redis.get(second.key) is not None


async def test_daily_limit_counts_only_the_transaction_day(env):
    """spent_today starts at UTC midnight of the transaction: yesterday's
    accruals do not use up today's limit."""
    seed, engine, _ = env
    cid = await seed.campaign(daily_limit="100", start=_now() - timedelta(days=5))
    yesterday, today = [await seed.accepted_offer(cid) for _ in range(2)]
    assert (await _accrue(engine, yesterday)).accrued
    async with seed.db.begin() as conn:
        await conn.execute(text(
            "UPDATE cashback_accruals SET accrued_at = accrued_at - interval '1 day' "
            "WHERE recommendation_id = :r"), {"r": yesterday.recommendation_id})

    assert (await _accrue(engine, today)).accrued


async def test_purchase_dated_tomorrow_sees_todays_spending(env):
    """The accrual is booked today (accrued_at = now) whatever the producer's
    clock says: today's exhausted limit holds for a purchase dated tomorrow."""
    seed, engine, _ = env
    cid = await seed.campaign(daily_limit="100")
    today, tomorrow = [await seed.accepted_offer(cid) for _ in range(2)]
    assert (await _accrue(engine, today)).accrued

    late = await _accrue(engine, tomorrow, at=_now() + timedelta(days=1))

    assert late.skipped_reason == "daily_limit_reached"
    assert await seed.budget_spent(cid) == Decimal("75.00")


async def test_campaign_lock_does_not_block_offers_for_the_campaign(env, db):
    """The accrual's campaign lock serialises budget writers, not the FK
    check (FOR KEY SHARE) of rec_api / mobile_api persisting an offer."""
    seed, _, _ = env
    cid = await seed.campaign()
    user_id = await seed.user()
    async with db.connect() as accrual_tx, db.connect() as other:
        await accrual_tx.begin()
        await accrual_tx.execute(engine_module._LOCK_CAMPAIGN_SQL, {"cid": cid})

        await other.begin()
        await other.execute(text("SET LOCAL lock_timeout = '2s'"))
        await other.execute(text(
            "INSERT INTO recommendations (user_id, campaign_id, mcc_code, model_score, "
            "expires_at) VALUES (:u, :c, '5411', 0.5, now() + interval '1 day')"),
            {"u": user_id, "c": cid})
        await other.commit()

        await other.begin()
        await other.execute(text("SET LOCAL lock_timeout = '1s'"))
        with pytest.raises(DBAPIError, match="lock timeout"):
            await other.execute(text(
                "UPDATE cashback_campaigns SET budget_spent = budget_spent + 1 "
                "WHERE campaign_id = :c"), {"c": cid})
        await other.rollback()
        await accrual_tx.rollback()


async def test_twenty_concurrent_purchases_never_exceed_the_daily_limit(env):
    """290 is not a multiple of 75: the 4th purchase (300) must be refused
    outright, not paid partially or in full."""
    seed, engine, _ = env
    cid = await seed.campaign(daily_limit="290", budget="100000")
    offers = [await seed.accepted_offer(cid) for _ in range(20)]

    results = await asyncio.gather(*(_accrue(engine, o) for o in offers))

    paid = [r for r in results if r.accrued]
    assert len(paid) == 3                         # 3 × 75 = 225 ≤ 290 < 300
    assert {r.skipped_reason for r in results if not r.accrued} == {
        "daily_limit_reached"}
    rows = await seed.accruals(campaign_id=cid)
    assert sum(r.cashback_amount for r in rows) == Decimal("225.00")
    assert await seed.budget_spent(cid) == Decimal("225.00")


async def test_budget_is_never_overspent(env):
    seed, engine, _ = env
    cid = await seed.campaign(budget="100")
    offers = [await seed.accepted_offer(cid) for _ in range(5)]

    results = await asyncio.gather(*(_accrue(engine, o) for o in offers))

    assert sum(r.accrued for r in results) == 1
    assert {r.skipped_reason for r in results if not r.accrued} == {"budget_exhausted"}
    assert await seed.budget_spent(cid) == Decimal("75.00")
