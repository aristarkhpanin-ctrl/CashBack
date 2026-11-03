"""GET /v1/mobile/recommendations/{user_id} — офферы recommendation_api (B29).

Строку ``recommendations`` на каждый оффер пишет recommendation_api (с
``model_version``); раньше mobile_api вставлял вторую — без версии модели,
и отклик обновлял именно её: онлайн-CTR по версиям и holdout были пусты, а
каждый показ занимал две строки. Теперь mobile_api:

* ничего не вставляет и отдаёт офферы upstream 1:1 — с ИХ
  ``recommendation_id`` (на него клиент откликается, по нему начисление
  находит принятый оффер) и сроком оффера ``expires_at``;
* оффер без ``recommendation_id`` (несовместимый upstream) не отдаёт —
  принять его нельзя;
* отмечает показ в приложении: ``channel = 'IN_APP'`` одним UPDATE для всех
  офферов ответа, только строки этого клиента и только где канал ещё не
  записан (``channel IS NULL`` — push/email listener'а не перезаписывается);
  сбой этой отметки ответ не ломает.

Семантику UPDATE (IS NULL, владелец) на настоящем Postgres проверяет
``tests/integration``.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from tests.conftest import bearer, make_token
from tests.unit.fakes import CAMPAIGN_ID, build_app, upstream_offer


@pytest.fixture()
async def mobile():
    built = build_app()
    transport = httpx.ASGITransport(app=built.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        built.client = client
        built.me = uuid.uuid4()
        built.headers = bearer(make_token(sub=str(built.me)))
        yield built


async def _get(mobile) -> httpx.Response:
    return await mobile.client.get(f"/v1/mobile/recommendations/{mobile.me}",
                                   headers=mobile.headers)


def _statements(mobile, verb: str) -> list[tuple[str, dict]]:
    return [(sql, p) for sql, p in mobile.db.calls if sql.startswith(verb)]


async def test_offers_are_rec_api_rows_one_to_one_and_nothing_is_inserted(mobile):
    offers = [upstream_offer("5411"), upstream_offer("5812"), upstream_offer("5541")]
    mobile.upstream.offers = offers

    resp = await _get(mobile)

    assert resp.status_code == 200, resp.text
    items = resp.json()["recommendations"]
    assert [(it["recommendation_id"], it["mcc_code"]) for it in items] \
        == [(o["recommendation_id"], o["mcc_code"]) for o in offers]
    for item, offer in zip(items, offers):
        assert item["deeplink"] == f"cashback://offer/{offer['recommendation_id']}"
        # Срок оффера — из его строки, а не конец кампании.
        assert item["expires_at"] == "2026-11-01T12:00:00Z"
        assert item["campaign_id"] == CAMPAIGN_ID
        assert "top_factors" not in item and "score" not in item
    assert _statements(mobile, "INSERT") == []


async def test_shown_offers_are_marked_in_app_in_one_statement(mobile):
    offers = [upstream_offer("5411"), upstream_offer("5812")]
    mobile.upstream.offers = offers

    assert (await _get(mobile)).status_code == 200

    [(sql, params)] = _statements(mobile, "UPDATE recommendations")
    assert "SET channel = 'IN_APP'" in sql
    assert "channel IS NULL" in sql
    assert "user_id = :uid" in sql
    assert params["uid"] == mobile.me
    assert params["rids"] == [uuid.UUID(o["recommendation_id"]) for o in offers]


async def test_offer_without_recommendation_id_is_not_served(mobile):
    keep = upstream_offer("5812")
    mobile.upstream.offers = [upstream_offer("5411", recommendation_id=None), keep]

    resp = await _get(mobile)

    assert resp.status_code == 200, resp.text
    assert [it["recommendation_id"] for it in resp.json()["recommendations"]] \
        == [keep["recommendation_id"]]
    [(_, params)] = _statements(mobile, "UPDATE recommendations")
    assert params["rids"] == [uuid.UUID(keep["recommendation_id"])]


async def test_no_offers_no_writes(mobile):
    mobile.upstream.offers = []

    resp = await _get(mobile)

    assert resp.status_code == 200, resp.text
    assert resp.json()["recommendations"] == []
    assert _statements(mobile, "UPDATE") == [] and _statements(mobile, "INSERT") == []


async def test_failed_in_app_mark_does_not_break_serving(mobile):
    """Отметка канала — best-effort: оффер показан и принимается и без неё."""
    offer = upstream_offer("5411")
    mobile.upstream.offers = [offer]
    mobile.db.errors["UPDATE recommendations"] = ConnectionError("postgres is down")

    resp = await _get(mobile)

    assert resp.status_code == 200, resp.text
    assert [it["recommendation_id"] for it in resp.json()["recommendations"]] \
        == [offer["recommendation_id"]]
