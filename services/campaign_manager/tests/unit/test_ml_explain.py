"""GET /ml/explain/{user_id} — прокси в read-only explain recommendation_api (B27).

recommendation_api подменяется ``httpx.MockTransport`` в клиенте, который в
проде создаёт lifespan (``app.state.rec_api_client``): видно, куда и с каким
токеном campaign_manager ходит, и как он отображает ответы rec_api.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
from app.config import get_settings
from app.models import RolePermission
from jose import jwt

USER = uuid.UUID("3f1c8a52-1111-4222-8333-944455556666")
EXPLANATION = {
    "user_id": str(USER), "model_version": "v7", "candidates_considered": 20,
    "serving_group": "prod", "base_value": 0.18, "confidence": 0.7,
    "expected_roi": None, "rationale": "", "alt_recs": ["5812"],
    "feature_interpretations": {},
    "recommendations": [{"mcc_code": "5411", "score": 0.81, "campaign_id": None,
                         "top_factors": {"txn_count_30d": 0.21},
                         "feature_values": {"txn_count_30d": 12.0}}],
}


class RecApi:
    """Фейковый recommendation_api: ``reply`` — ответ (или исключение)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.reply: httpx.Response | Exception = httpx.Response(200, json=EXPLANATION)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def rec_api(app) -> RecApi:
    fake = RecApi()
    app.state.rec_api_client = httpx.AsyncClient(
        base_url="http://recommendation-api:8001", transport=httpx.MockTransport(fake))
    return fake


def test_proxies_to_read_only_explain_with_service_token(client, auth, rec_api):
    r = client.get(f"/ml/explain/{USER}", params={"top_k": 3}, headers=auth("ANALYST"))
    assert r.status_code == 200, r.text
    assert r.json() == EXPLANATION                    # ответ rec_api как есть

    (req,) = rec_api.requests
    assert req.method == "GET"
    # read-only explain, а не боевой /recommendations/{id} (B27)
    assert req.url == f"http://recommendation-api:8001/recommendations/{USER}/explain?top_k=3"
    scheme, token = req.headers["authorization"].split()
    assert scheme == "Bearer"
    claims = jwt.decode(token, get_settings().service_jwt_secret.get_secret_value(),
                        algorithms=["HS256"], audience="recommendation-api")
    assert claims["iss"] == "campaign-manager" and claims["type"] == "service"
    assert claims["exp"] - claims["iat"] == 300
    # admin-ключом сервисный токен не подписан
    with pytest.raises(jwt.JWTError):
        jwt.decode(token, get_settings().jwt_secret.get_secret_value(),
                   algorithms=["HS256"], audience="recommendation-api")


def test_default_top_k_is_5(client, auth, rec_api):
    assert client.get(f"/ml/explain/{USER}", headers=auth("ADMIN")).status_code == 200
    assert rec_api.requests[0].url.params["top_k"] == "5"


def test_upstream_404_passes_through(client, auth, rec_api):
    detail = f"user '{USER}' not found in the feature store"
    rec_api.reply = httpx.Response(404, json={"detail": detail})
    r = client.get(f"/ml/explain/{USER}", headers=auth("ANALYST"))
    assert r.status_code == 404
    assert r.json() == {"detail": detail}


@pytest.mark.parametrize(("status", "body"), [
    (500, {"detail": "boom"}), (503, {"detail": "ranking model not loaded yet"}),
    (401, {"detail": "invalid or expired token"}), (403, {"detail": "nope"}),
    (422, {"detail": []}), (302, {}),
])
def test_upstream_errors_become_502(client, auth, rec_api, status, body):
    rec_api.reply = httpx.Response(status, json=body)
    r = client.get(f"/ml/explain/{USER}", headers=auth("ANALYST"))
    assert r.status_code == 502
    assert r.json() == {"detail": f"recommendation service returned {status}"}


@pytest.mark.parametrize(("error", "detail"), [
    (httpx.ReadTimeout("slow"), "recommendation service timed out"),
    (httpx.ConnectTimeout("slow"), "recommendation service timed out"),
    (httpx.ConnectError("refused"), "recommendation service unavailable"),
    (httpx.RemoteProtocolError("reset"), "recommendation service unavailable"),
])
def test_unreachable_upstream_is_502(client, auth, rec_api, error, detail):
    rec_api.reply = error
    r = client.get(f"/ml/explain/{USER}", headers=auth("ANALYST"))
    assert r.status_code == 502
    assert r.json() == {"detail": detail}


@pytest.mark.parametrize("reply", [
    httpx.Response(200, content=b"<html>proxy error</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
])
def test_non_json_upstream_body_is_502(client, auth, rec_api, reply):
    rec_api.reply = reply
    r = client.get(f"/ml/explain/{USER}", headers=auth("ANALYST"))
    assert r.status_code == 502
    assert r.json() == {"detail": "invalid response from recommendation service"}


def test_upstream_404_without_json_detail(client, auth, rec_api):
    rec_api.reply = httpx.Response(404, content=b"not found")
    r = client.get(f"/ml/explain/{USER}", headers=auth("ANALYST"))
    assert r.status_code == 404 and r.json() == {"detail": "user not found"}


def test_needs_analytics_permission(client, db, auth, rec_api):
    assert client.get(f"/ml/explain/{USER}").status_code == 401
    headers = auth("MARKETER")                       # analytics=false по умолчанию
    r = client.get(f"/ml/explain/{USER}", headers=headers)
    assert r.status_code == 403
    db.put(RolePermission(role="MARKETER", permissions={"analytics": True}))
    assert client.get(f"/ml/explain/{USER}", headers=headers).status_code == 200
    assert len(rec_api.requests) == 1                # отказы до rec_api не доходят


@pytest.mark.parametrize("path", ["/ml/explain/not-a-uuid", "/ml/explain/..",
                                  f"/ml/explain/{USER}?top_k=0",
                                  f"/ml/explain/{USER}?top_k=21"])
def test_rejects_bad_input_without_calling_upstream(client, auth, rec_api, path):
    assert client.get(path, headers=auth("ADMIN")).status_code in (404, 422)
    assert rec_api.requests == []


def test_upstream_body_is_not_reparsed_into_a_schema(client, auth, rec_api):
    # новое поле rec_api доходит до фронта без правки campaign_manager
    rec_api.reply = httpx.Response(200, content=json.dumps(
        {**EXPLANATION, "new_field": {"x": 1}}).encode(),
        headers={"content-type": "application/json"})
    r = client.get(f"/ml/explain/{USER}", headers=auth("ANALYST"))
    assert r.json()["new_field"] == {"x": 1}
