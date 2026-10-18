"""Stage C (B21, B22, B26, B42): кампания кандидату — только из eligible_campaigns.

Кому положена кампания, решает SQL-функция ``eligible_campaigns``
(миграция 009): статус и архив, окно дат, категория, сегменты, последнее
согласие, RFM, бюджет и дневной лимит. Её правила на Postgres проверяют
``db_migrations/tests`` и ``tests/integration`` этого сервиса; здесь —
что делает с её ответом сам сервис:

* один запрос на всех кандидатов — с клиентом и моментом выдачи, одинаково
  в боевой выдаче и в explain;
* кандидат без положенной кампании отбрасывается до BRE: не тратит лимит
  показов R4 и не получает отметку R3;
* сбой Postgres → ни одного оффера (а не оффер без проверки) и ни одной
  записи в Redis;
* исчерпанный дневной лимит кампании (R6) отсекает её в обоих маршрутах;
* событие Kafka несёт имя и ставку выбранной кампании (NUMERIC из asyncpg
  приходит Decimal).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.unit.fakes import CANDIDATES, USER_ID, campaign_row


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _serve(rec_app, mint, **params):
    resp = await rec_app.client.get(
        f"/recommendations/{USER_ID}", params=params,
        headers=_auth(mint(iss="mobile-api")),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _explain(rec_app, mint, **params):
    resp = await rec_app.client.get(
        f"/recommendations/{USER_ID}/explain", params=params,
        headers=_auth(mint(iss="campaign-manager")),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _offers(body: dict) -> dict[str, str]:
    return {it["mcc_code"]: it["campaign_id"] for it in body["recommendations"]}


async def _bre_marks(rec_app) -> list[str]:
    """Ключи, которые пишут R3/R4 боевой выдачи."""
    return sorted([*await rec_app.redis.keys("recent_offer:*"),
                   *await rec_app.redis.keys("rec_count:*")])


# ---------------------------------------------------------------------------
async def test_one_lookup_per_request_for_this_user_and_moment(rec_app, mint):
    """Оба маршрута спрашивают eligible_campaigns одним запросом: про этого
    клиента, про все MCC-кандидаты и про текущий момент (UTC)."""
    before = datetime.now(UTC)
    await _explain(rec_app, mint)
    await _serve(rec_app, mint)
    after = datetime.now(UTC)

    assert len(rec_app.db.lookups) == 2
    for lookup in rec_app.db.lookups:
        assert lookup["user_id"] == USER_ID
        assert lookup["mccs"] == CANDIDATES
        assert lookup["at"].tzinfo is not None
        assert before - timedelta(seconds=1) <= lookup["at"] <= after
    lookups = [q for q in rec_app.db.statements if "eligible_campaigns(" in q]
    assert len(lookups) == 2 and lookups[0] == lookups[1]
    assert not any("FOR UPDATE" in q for q in rec_app.db.statements)


async def test_candidate_without_eligible_campaign_is_dropped_before_bre(rec_app, mint):
    """Кандидаты без положенной клиенту кампании до BRE не доходят: лимит
    показов R4 считает только реальные офферы (раньше — каждого кандидата,
    и пять «пустых» MCC гасили выдачу на сутки)."""
    only = campaign_row("5541", 3)
    rec_app.db.campaigns.clear()
    rec_app.db.campaigns["5541"] = only

    explained = await _explain(rec_app, mint)
    assert _offers(explained) == {"5541": only["campaign_id"]}

    served = await _serve(rec_app, mint)
    assert _offers(served) == {"5541": only["campaign_id"]}
    assert served["candidates_considered"] == len(CANDIDATES)
    assert await rec_app.redis.get(f"rec_count:{USER_ID}") == "1"
    assert await _bre_marks(rec_app) == [
        f"rec_count:{USER_ID}", f"recent_offer:{USER_ID}:{only['campaign_id']}",
    ]
    assert [row["mcc"] for row in rec_app.db.inserted] == ["5541"]
    assert [event["mcc_code"] for _, _, event in rec_app.producer.sent] == ["5541"]


async def test_no_eligible_campaign_at_all_offers_nothing_and_marks_nothing(rec_app, mint):
    """Клиенту не положено ни одной кампании (нет согласия, чужой сегмент…):
    пустая выдача, и BRE не тронут — ни счётчика R4, ни отметок R3."""
    rec_app.db.campaigns.clear()
    assert (await _explain(rec_app, mint))["recommendations"] == []
    assert (await _serve(rec_app, mint))["recommendations"] == []
    assert await _bre_marks(rec_app) == []
    assert rec_app.db.inserted == [] and rec_app.producer.sent == []


async def test_lookup_failure_offers_nothing(rec_app, mint):
    """Сбой Postgres при поиске кампаний — ни одного оффера, а не оффер без
    проверки положенности, и ни одной записи в Redis."""
    rec_app.db.lookup_error = ConnectionRefusedError("postgres is down")
    assert (await _explain(rec_app, mint))["recommendations"] == []
    assert (await _serve(rec_app, mint))["recommendations"] == []
    assert len(rec_app.db.lookups) == 2
    assert await _bre_marks(rec_app) == []
    assert rec_app.db.inserted == [] and rec_app.producer.sent == []


async def test_daily_limit_used_up_drops_the_campaign_on_both_routes(rec_app, mint):
    """B22: кампания, у которой сегодняшние начисления уже дошли до
    daily_limit, не предлагается (R6, без блокировок); остаток лимита —
    предлагается (сумму выплаты решит начисление)."""
    rec_app.db.campaigns["5411"] = campaign_row(
        "5411", 2, daily_limit=Decimal("300.00"), spent_today=Decimal("300.00"))
    rec_app.db.campaigns["5541"] = campaign_row(
        "5541", 3, daily_limit=Decimal("300.00"), spent_today=Decimal("299.50"))

    assert sorted(_offers(await _explain(rec_app, mint))) == ["5541", "5812"]
    assert sorted(_offers(await _serve(rec_app, mint))) == ["5541", "5812"]


async def test_kafka_event_carries_the_chosen_campaign(rec_app, mint):
    """Имя и ставка в событии — выбранной eligible_campaigns кампании
    (раньше имя всегда было «Cashback offer»); NUMERIC — Decimal."""
    chosen = campaign_row(
        "5411", 2, name="Продукты: лучшая ставка", cashback_rate=Decimal("7.50"),
        min_transaction_amount=Decimal("1000.00"), budget_total=Decimal("5000.00"),
        budget_spent=Decimal("10.00"), daily_limit=Decimal("300.00"),
        spent_today=Decimal("0"))
    rec_app.db.campaigns["5411"] = chosen

    served = await _serve(rec_app, mint, transaction_amount=1500)
    assert _offers(served)["5411"] == chosen["campaign_id"]
    events = {event["mcc_code"]: event for _, _, event in rec_app.producer.sent}
    assert events["5411"]["campaign_id"] == chosen["campaign_id"]
    assert events["5411"]["campaign_name"] == "Продукты: лучшая ставка"
    assert events["5411"]["cashback_rate"] == 7.5


async def test_r2_uses_the_minimum_from_the_campaign_row(rec_app, mint):
    """B21: R2 сравнивает сумму покупки с min_transaction_amount строки
    eligible_campaigns — это уже минимум категории (COALESCE(cc.min,
    c.min)). Ниже минимума — нет оффера; explain суммы не знает."""
    rec_app.db.campaigns["5411"] = campaign_row(
        "5411", 2, min_transaction_amount=Decimal("1000.00"))
    assert "5411" not in _offers(await _serve(rec_app, mint, transaction_amount=999.99))
    assert "5411" in _offers(await _explain(rec_app, mint))
