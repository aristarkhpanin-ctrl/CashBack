"""B27: GET /recommendations/{user_id}/explain — без побочных эффектов.

Страница «ML-объяснения» раньше вызывала боевую выдачу: каждый просмотр
создавал PENDING-строки, слал recommendations.created (push/email клиенту)
и сжигал anti-fatigue и дневной лимит. Explain отдаёт тот же ответ, но не
пишет ни в Postgres, ни в Kafka, ни в Redis и не берёт блокировок бюджета;
боевая выдача свои эффекты сохраняет.
"""
from __future__ import annotations

import json

from tests.unit.fakes import CANDIDATES, FEATURES, USER_ID, FakeClickHouse, campaign_row

# Порядок по score у FakeModel: меньший MCC — выше.
RANKED = sorted(CANDIDATES)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _items(body: dict) -> list[tuple]:
    return [(it["mcc_code"], it["score"], it["campaign_id"], it["top_factors"],
             it["feature_values"]) for it in body["recommendations"]]


def _mccs(body: dict) -> list[str]:
    return [it["mcc_code"] for it in body["recommendations"]]


async def _serve(rec_app, mint, **params):
    return await rec_app.client.get(
        f"/recommendations/{USER_ID}", params=params,
        headers=_auth(mint(iss="mobile-api")),
    )


async def _explain_read_only(rec_app, mint, **params) -> dict:
    """explain + проверка: ни записи в Redis/Postgres, ни Kafka, ни FOR UPDATE."""
    redis_before = await rec_app.redis.snapshot()
    n_cmd, n_sql = len(rec_app.redis.commands), len(rec_app.db.statements)
    n_rows, n_sent = len(rec_app.db.inserted), len(rec_app.producer.sent)

    resp = await rec_app.client.get(
        f"/recommendations/{USER_ID}/explain", params=params,
        headers=_auth(mint(iss="campaign-manager")),
    )
    assert resp.status_code == 200, resp.text

    # Redis: только чтения — ни SETEX recent_offer:*, ни SETEX rec_count:*.
    commands = rec_app.redis.commands[n_cmd:]
    assert commands and set(commands) <= {"GET"}, commands
    # Postgres: только SELECT (кампании), без FOR UPDATE и без INSERT.
    statements = rec_app.db.statements[n_sql:]
    assert statements and all(q.startswith("SELECT") for q in statements), statements
    assert not any("FOR UPDATE" in q for q in statements)
    assert len(rec_app.db.inserted) == n_rows
    # Kafka: ни одного события.
    assert len(rec_app.producer.sent) == n_sent

    redis_after = await rec_app.redis.snapshot()
    assert redis_after.keys() == redis_before.keys()
    for key, (value, pttl) in redis_after.items():
        assert value == redis_before[key][0], key
        assert pttl <= redis_before[key][1], f"TTL of {key} was refreshed"
    return resp.json()


# ---------------------------------------------------------------------------
async def test_explain_is_read_only_and_serving_keeps_side_effects(rec_app, mint):
    # 1) explain на «чистом» пользователе — полный ответ, ничего не пишет.
    explained = await _explain_read_only(rec_app, mint)
    assert _mccs(explained) == RANKED
    assert explained["model_version"] == "7"
    assert explained["rationale"] and explained["feature_interpretations"]
    assert 0.0 < explained["base_value"] < 1.0
    assert rec_app.db.inserted == [] and rec_app.producer.sent == []

    # 2) боевая выдача: тот же ответ + запись, событие Kafka, отметки BRE.
    resp = await _serve(rec_app, mint)
    assert resp.status_code == 200, resp.text
    served = resp.json()
    assert served == explained
    assert len(rec_app.db.inserted) == len(RANKED)
    assert [m["mcc_code"] for _, _, m in rec_app.producer.sent] == RANKED
    assert {topic for topic, _, _ in rec_app.producer.sent} == {"recommendations.created"}
    assert any("FOR UPDATE" in q for q in rec_app.db.statements)
    assert await rec_app.redis.get(f"rec_count:{USER_ID}") == str(len(RANKED))
    for row in rec_app.db.campaigns.values():
        key = f"recent_offer:{USER_ID}:{row['campaign_id']}"
        assert await rec_app.redis.get(key) == "1"

    # 3) explain после выдачи: R3/R4 не применяются → тот же ответ, и снова
    #    ни одной записи (TTL отметок не продлеваются).
    again = await _explain_read_only(rec_app, mint)
    assert _items(again) == _items(explained)

    # 4) а боевая выдача после этого честно отсекается anti-fatigue (R3).
    resp = await _serve(rec_app, mint)
    assert resp.status_code == 200
    assert resp.json()["recommendations"] == []


async def test_explain_does_not_warm_the_feature_cache(rec_app, mint):
    """Промах по features:{user_id}: explain берёт признаки из ClickHouse и
    НЕ прогревает ими Redis (раньше — SETEX features:* на TTL 1 ч);
    прогрев остаётся за боевой выдачей."""
    from app.feature_store import FeatureStoreClient

    ch = FakeClickHouse({USER_ID: FEATURES})
    rec_app.app.state.feature_store = FeatureStoreClient(
        redis_client=rec_app.redis, ch_client=ch,
    )
    await rec_app.redis.delete(f"features:{USER_ID}")

    explained = await _explain_read_only(rec_app, mint)
    assert _mccs(explained) == RANKED
    assert len(ch.queries) == 1
    assert await rec_app.redis.exists(f"features:{USER_ID}") == 0

    resp = await _serve(rec_app, mint)
    assert resp.status_code == 200, resp.text
    assert json.loads(await rec_app.redis.get(f"features:{USER_ID}")) == FEATURES
    assert 0 < await rec_app.redis.ttl(f"features:{USER_ID}") <= 3600


async def test_explain_top_k(rec_app, mint):
    assert _mccs(await _explain_read_only(rec_app, mint, top_k=2)) == RANKED[:2]
    for bad in (0, 21):
        resp = await rec_app.client.get(
            f"/recommendations/{USER_ID}/explain", params={"top_k": bad},
            headers=_auth(mint(iss="campaign-manager")),
        )
        assert resp.status_code == 422


async def test_explain_404_for_user_without_features(rec_app, mint):
    resp = await rec_app.client.get(
        "/recommendations/22222222-2222-2222-2222-222222222222/explain",
        headers=_auth(mint(iss="campaign-manager")),
    )
    assert resp.status_code == 404
    assert "feature store" in resp.json()["detail"]
    assert rec_app.db.inserted == [] and rec_app.producer.sent == []


async def test_explain_budget_rule_is_evaluated_without_lock(rec_app, mint):
    """R6 в explain: исчерпанный бюджет отсекает кандидата — по уже
    прочитанной строке кампании, без SELECT … FOR UPDATE."""
    rec_app.db.campaigns["5411"] = campaign_row(
        "5411", 2, budget_total=1000.0, budget_spent=999.5,
    )
    assert _mccs(await _explain_read_only(rec_app, mint)) == ["5541", "5812"]
    # Боевая выдача отсекает тот же кандидат (под блокировкой).
    resp = await _serve(rec_app, mint)
    assert _mccs(resp.json()) == ["5541", "5812"]


async def test_explain_applies_ml_limits_kill_switch(rec_app, mint):
    """R7 (снапшот ML-лимитов, только чтение) в explain применяется."""
    await rec_app.redis.set("ml_limits:snapshot",
                            json.dumps({"global_enabled": False, "buckets": {}}))
    assert (await _explain_read_only(rec_app, mint))["recommendations"] == []


async def test_explain_skips_contact_policy_rules(rec_app, mint):
    """R3/R4 в explain не вычисляются: исчерпанный дневной лимит не скрывает
    объяснение (а боевую выдачу — скрывает)."""
    await rec_app.redis.set(f"rec_count:{USER_ID}", "99")
    assert _mccs(await _explain_read_only(rec_app, mint)) == RANKED
    resp = await _serve(rec_app, mint)
    assert resp.json()["recommendations"] == []
