"""Integration: Recommendation API end-to-end inside one service.

Hits the full FastAPI app (incl. middleware) via httpx ASGITransport.
External dependencies (LightGBM model, MLflow, Postgres, Redis) are
stubbed so the test stays fast and hermetic, but the route handler is
the real one — validation, BRE filtering, SHAP factor extraction.

``/recommendations/*`` accepts only service tokens (stage B, ADR-0013):
HS256 signed with ``SERVICE_JWT_SECRET``, ``aud recommendation-api``,
``type service``; the serving path only from ``iss mobile-api``, the
read-only ``/explain`` only from ``iss campaign-manager``. The app runs
with an explicit ``SERVICE_JWT_SECRET`` and the tests mint tokens the way
mobile_api / campaign_manager do (``tests/auth_tokens.py``).

Tests:

    1. /health/live → 200
    2. /  → service metadata + request id header
    3. /recommendations/{user} without a token → 401
    4. a service token signed with another key (JWT_SECRET, MOBILE_JWT_SECRET) → 401
    5. an admin access token (aud cashback-admin, type access) → 401
    6. a campaign-manager token on the serving path / a mobile-api token on
       /explain → 403
    7. valid token, no feature payload → 404 (serving path and /explain)
    8. /metrics endpoint exposes Prometheus text format
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.auth_tokens import (
    ISSUER_CAMPAIGN_MANAGER,
    ISSUER_MOBILE_API,
    bearer,
    service_token,
)

# Use the recommendation_api service's `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "recommendation_api"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration

# Three different keys, as `make env` generates them (≥ 32 bytes each). CI
# exports a fresh random set per run, which wins (no fixed key is trusted).
SERVICE_JWT_SECRET = (os.environ.get("SERVICE_JWT_SECRET")
                      or "it-service-jwt-secret-0123456789abcdef01")
JWT_SECRET = os.environ.get("JWT_SECRET") or "it-admin-jwt-secret-0123456789abcdef0123"
MOBILE_JWT_SECRET = (os.environ.get("MOBILE_JWT_SECRET")
                     or "it-mobile-jwt-secret-0123456789abcdef012")


def _service(issuer: str = ISSUER_MOBILE_API, *, secret: str = SERVICE_JWT_SECRET,
             **claims: str) -> dict[str, str]:
    return bearer(service_token(issuer, secret=secret, **claims))


@pytest.fixture()
async def app_client(monkeypatch):
    """Build the FastAPI app, then patch app.state to skip heavy deps."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    # The service refuses to start without a strong key (no default, no
    # switch to turn the check off) — and app.main builds the app on
    # import, so the key is set before the first import.
    monkeypatch.setenv("SERVICE_JWT_SECRET", SERVICE_JWT_SECRET)
    from app.config import get_settings
    from app.main import create_app
    from httpx import ASGITransport, AsyncClient

    # Make sure config picks up something benign.
    os.environ.setdefault("MLFLOW_TRACKING_URI", "http://invalid:5000")
    os.environ.setdefault("CLICKHOUSE_HOST", "invalid")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault(
        "POSTGRES_DSN",
        "postgresql+asyncpg://nobody:nopw@localhost:5432/postgres",
    )
    get_settings.cache_clear()

    app = create_app()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # ASGITransport does not run the lifespan, so nothing connects to
        # MLflow / Postgres / Redis; attach the state lifespan would create,
        # with stubs for the external clients.
        app.state.settings = get_settings()
        app.state.feature_store = MagicMock()
        app.state.feature_store.get = AsyncMock(return_value=None)
        app.state.model_watcher = MagicMock()
        app.state.model_watcher.loaded = False
        app.state.model_watcher.version = None
        app.state.model_watcher.get = AsyncMock(return_value=None)
        app.state.candidate_gen = MagicMock()
        app.state.candidate_gen.retrieve = MagicMock(return_value=[])
        # Stub Redis + DB so the BRE rules don't crash on attribute access.
        app.state.redis = MagicMock()
        app.state.db_engine = MagicMock()
        app.state.bre = MagicMock()
        yield ac


# ---------------------------------------------------------------------------
async def test_health_live(app_client):
    resp = await app_client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_root_returns_service_metadata(app_client):
    resp = await app_client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "recommendation-api"
    assert resp.headers.get("X-Request-ID")


async def test_recommendations_require_token(app_client):
    user = str(uuid.uuid4())
    for path in (f"/recommendations/{user}?top_k=3", f"/recommendations/{user}/explain"):
        resp = await app_client.get(path)
        assert resp.status_code == 401, path
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("secret", [JWT_SECRET, MOBILE_JWT_SECRET],
                         ids=["admin-key", "mobile-key"])
async def test_recommendations_reject_token_signed_with_another_key(app_client, secret):
    """A leaked admin or customer-token key cannot mint service tokens (B19)."""
    user = str(uuid.uuid4())
    resp = await app_client.get(f"/recommendations/{user}?top_k=3",
                                headers=_service(secret=secret))
    assert resp.status_code == 401, resp.text


async def test_recommendations_reject_admin_access_token(app_client):
    """Admin tokens are not accepted anywhere in rec_api, whatever the key."""
    user = str(uuid.uuid4())
    access = _service("cashback-campaign-manager", audience="cashback-admin",
                      token_type="access")
    for path in (f"/recommendations/{user}?top_k=3", f"/recommendations/{user}/explain"):
        resp = await app_client.get(path, headers=access)
        assert resp.status_code == 401, (path, resp.text)


async def test_each_route_accepts_only_its_issuer(app_client):
    """Serving (writes recommendations, Kafka, BRE counters) — mobile-api only;
    the read-only explain — campaign-manager only (B27)."""
    user = str(uuid.uuid4())
    serving = await app_client.get(f"/recommendations/{user}?top_k=3",
                                   headers=_service(ISSUER_CAMPAIGN_MANAGER))
    explain = await app_client.get(f"/recommendations/{user}/explain",
                                   headers=_service(ISSUER_MOBILE_API))
    assert serving.status_code == 403, serving.text
    assert explain.status_code == 403, explain.text


async def test_recommendations_404_when_features_missing(app_client):
    user = str(uuid.uuid4())
    for path, issuer in ((f"/recommendations/{user}?top_k=3", ISSUER_MOBILE_API),
                         (f"/recommendations/{user}/explain", ISSUER_CAMPAIGN_MANAGER)):
        resp = await app_client.get(path, headers=_service(issuer))
        assert resp.status_code == 404, (path, resp.text)
        assert "feature store" in resp.json()["detail"]


async def test_metrics_endpoint_exposes_prometheus_format(app_client):
    resp = await app_client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert (
        "process_" in text
        or "http_requests_total" in text
        or "python_info" in text
    )
