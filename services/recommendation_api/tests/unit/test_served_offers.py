"""B29: одна строка ``recommendations`` на каждый оффер боевой выдачи.

Строку пишет только recommendation_api (раньше вторую, без model_version,
писал mobile_api, а отклик обновлял её — онлайн-CTR по версии модели и
holdout были пусты, таблица удваивалась). Здесь — что делает маршрут:

* на каждый оффер ответа — ровно одна строка с ``model_version`` модели,
  реально обслужившей запрос; её ``recommendation_id`` и ``expires_at``
  (LEAST(конец кампании, now() + OFFER_TTL_DAYS) по часам БД) — в ответе;
* событие ``recommendations.created`` несёт тот же ``recommendation_id``,
  срок и настоящее имя кампании;
* оффер, строка которого не записана, клиенту не отдаётся: кампании уже
  нет — его нет в ответе и в Kafka; сбой Postgres — 503, ни одного события;
  отметки показа, которые R3/R4 поставили ему при оценке BRE, сняты.

SQL записи (LEAST/make_interval, приведения типов) на настоящем Postgres
проверяет ``tests/integration``.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.unit.fakes import CANDIDATES, USER_ID, campaign_row

RANKED = sorted(CANDIDATES)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _serve(rec_app, mint, **params):
    return await rec_app.client.get(
        f"/recommendations/{USER_ID}", params=params,
        headers=_auth(mint(iss="mobile-api")),
    )


def _events(rec_app) -> list[dict]:
    return [event for _, _, event in rec_app.producer.sent]


async def _marks(rec_app) -> dict[str, str]:
    """Отметки показа BRE в Redis: R3 (anti-fatigue) и R4 (счётчик за 24 ч)."""
    return {key: value for key, (value, _) in (await rec_app.redis.snapshot()).items()
            if key.startswith(("recent_offer:", "rec_count:"))}


# ---------------------------------------------------------------------------
async def test_one_row_per_served_offer_with_its_id_and_model_version(rec_app, mint):
    resp = await _serve(rec_app, mint)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body["recommendations"]
    assert [it["mcc_code"] for it in items] == RANKED

    rows = rec_app.db.inserted
    assert len(rows) == len(items)                       # ни одной лишней строки
    assert [it["recommendation_id"] for it in items] == [row["rid"] for row in rows]
    assert len({row["rid"] for row in rows}) == len(rows)
    for item, row in zip(items, rows):
        assert row["uid"] == USER_ID
        assert (row["cid"], row["mcc"]) == (item["campaign_id"], item["mcc_code"])
        assert row["score"] == item["score"]
        assert row["model_version"] == body["model_version"] == "7"
        assert row["ttl_days"] == 7
    # Все строки — одной транзакцией выдачи.
    inserts = [q for q in rec_app.db.statements if q.startswith("INSERT INTO recommendations")]
    assert len(inserts) == len(items)


async def test_expires_at_is_campaign_end_or_offer_ttl_whichever_first(rec_app, mint):
    now = rec_app.db.now
    ends_soon = now + timedelta(days=2)
    rec_app.db.campaigns["5411"] = campaign_row("5411", 2, end_date=ends_soon)

    items = (await _serve(rec_app, mint)).json()["recommendations"]

    expires = {it["mcc_code"]: datetime.fromisoformat(it["expires_at"]) for it in items}
    assert expires["5411"] == ends_soon
    assert expires["5541"] == expires["5812"] == now + timedelta(days=7)


async def test_offer_ttl_days_comes_from_settings(rec_app, mint):
    st = rec_app.app.state
    st.settings = st.settings.model_copy(update={"offer_ttl_days": 3})

    items = (await _serve(rec_app, mint)).json()["recommendations"]

    assert {row["ttl_days"] for row in rec_app.db.inserted} == {3}
    assert {datetime.fromisoformat(it["expires_at"]) for it in items} \
        == {rec_app.db.now + timedelta(days=3)}


async def test_event_carries_the_row_id_expiry_and_real_campaign_name(rec_app, mint):
    rec_app.db.campaigns["5411"] = campaign_row("5411", 2, name="Продукты: осень")

    items = (await _serve(rec_app, mint)).json()["recommendations"]

    events = _events(rec_app)
    assert [e["recommendation_id"] for e in events] == [it["recommendation_id"] for it in items]
    by_mcc = {e["mcc_code"]: e for e in events}
    assert by_mcc["5411"]["campaign_name"] == "Продукты: осень"
    assert by_mcc["5541"]["campaign_name"] == "Кампания 5541"
    for item in items:
        event = by_mcc[item["mcc_code"]]
        assert event["campaign_id"] == item["campaign_id"]
        assert datetime.fromisoformat(event["expires_at"]) \
            == datetime.fromisoformat(item["expires_at"])
        assert datetime.fromisoformat(event["generated_at"]) == rec_app.db.now
        assert event["model_version"] == "7"


async def test_offer_whose_campaign_row_is_gone_is_not_served(rec_app, mint):
    gone = rec_app.db.campaigns["5541"]["campaign_id"]
    rec_app.db.missing_campaigns.add(gone)

    items = (await _serve(rec_app, mint)).json()["recommendations"]

    assert [it["mcc_code"] for it in items] == ["5411", "5812"]
    assert all(it["recommendation_id"] for it in items)
    assert [row["mcc"] for row in rec_app.db.inserted] == ["5411", "5812"]
    assert [e["mcc_code"] for e in _events(rec_app)] == ["5411", "5812"]


async def test_storage_failure_is_503_and_publishes_nothing(rec_app, mint):
    """Оффер без строки принять нельзя (respond — 404) и в аналитике его нет:
    сбой записи — 503, а не ответ с несуществующими офферами."""
    rec_app.db.insert_error = ConnectionResetError("connection lost")

    resp = await _serve(rec_app, mint)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "recommendations could not be stored, retry"
    assert rec_app.db.inserted == []
    assert rec_app.producer.sent == []


async def test_storage_failure_gives_back_the_bre_marks(rec_app, mint):
    """R3/R4 отмечают показ при оценке, до записи строк. После 503 оффер не
    отдан — отметки сняты: иначе кампании скрыты от клиента на
    ANTI_FATIGUE_TTL_DAYS (14 дней), а лимит R4 съеден без показа, и
    повтор после восстановления Postgres не отдал бы ни одного оффера."""
    rec_app.db.insert_error = ConnectionResetError("connection lost")
    assert (await _serve(rec_app, mint)).status_code == 503
    assert await _marks(rec_app) == {}

    rec_app.db.insert_error = None
    resp = await _serve(rec_app, mint)
    assert resp.status_code == 200, resp.text
    assert [it["mcc_code"] for it in resp.json()["recommendations"]] == RANKED
    assert (await _marks(rec_app))[f"rec_count:{USER_ID}"] == str(len(RANKED))


async def test_offer_whose_campaign_row_is_gone_gives_back_its_bre_marks(rec_app, mint):
    gone = rec_app.db.campaigns["5541"]["campaign_id"]
    rec_app.db.missing_campaigns.add(gone)

    items = (await _serve(rec_app, mint)).json()["recommendations"]

    assert await _marks(rec_app) == {
        **{f"recent_offer:{USER_ID}:{it['campaign_id']}": "1" for it in items},
        f"rec_count:{USER_ID}": "2",
    }
    assert gone not in {it["campaign_id"] for it in items}


async def test_redis_failure_while_giving_back_marks_keeps_the_503(rec_app, mint):
    async def broken_delete(*keys):
        raise ConnectionError("redis unavailable")

    rec_app.db.insert_error = ConnectionResetError("connection lost")
    rec_app.redis.delete = broken_delete

    resp = await _serve(rec_app, mint)

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == "recommendations could not be stored, retry"


async def test_no_offers_no_rows_no_events(rec_app, mint):
    rec_app.db.campaigns.clear()

    resp = await _serve(rec_app, mint)

    assert resp.status_code == 200, resp.text
    assert resp.json()["recommendations"] == []
    assert not any(q.startswith("INSERT") for q in rec_app.db.statements)
    assert rec_app.producer.sent == []


async def test_without_kafka_producer_rows_are_still_written(rec_app, mint):
    rec_app.app.state.kafka_producer = None

    items = (await _serve(rec_app, mint)).json()["recommendations"]

    assert len(items) == len(rec_app.db.inserted) == len(RANKED)


async def test_publish_failure_keeps_the_served_offers(rec_app, mint):
    async def broken_send(*args, **kwargs):
        raise ConnectionError("kafka unavailable")

    rec_app.producer.send = broken_send

    resp = await _serve(rec_app, mint)

    assert resp.status_code == 200, resp.text
    assert [it["recommendation_id"] for it in resp.json()["recommendations"]] \
        == [row["rid"] for row in rec_app.db.inserted]
