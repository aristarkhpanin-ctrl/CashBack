"""Integration: Recommendation API end-to-end inside one service.

Hits the full FastAPI app (incl. middleware) via httpx ASGITransport.
External dependencies (LightGBM model, MLflow, Postgres, Redis) are
stubbed so the test stays fast and hermetic, but the route handler is
the real one — validation, BRE filtering, SHAP factor extraction.

``/recommendations/*`` requires a service/access JWT (``auth_enabled`` is
on by default, as in production): the app is configured with an explicit
``JWT_SECRET`` and the tests mint tokens with it.

Six tests:

    1. /health/live → 200
    2. /  → service metadata + request id header
    3. /recommendations/{user} without a token → 401
    4. /recommendations/{user} with a token signed by another secret → 401
    5. /recommendations/{user} (valid service token), no feature payload → 404
    6. /metrics endpoint exposes Prometheus text format
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Use the recommendation_api service's `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "recommendation_api"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration

JWT_SECRET = "integration-tests-secret"


def _token(secret: str, token_type: str = "service") -> str:
    """HS256 token with the claims mobile_api's ServiceTokenProvider mints."""
    from app.security import ALGORITHM
    from jose import jwt

    issued = datetime.now(UTC)
    return jwt.encode(
        {"sub": "integration-tests", "type": token_type,
         "iat": int(issued.timestamp()),
         "exp": int((issued + timedelta(minutes=5)).timestamp())},
        secret, algorithm=ALGORITHM,
    )


@pytest.fixture()
async def app_client(monkeypatch):
    """Build the FastAPI app, then patch app.state to skip heavy deps."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
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
    # Auth is configured explicitly (production default: enabled).
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
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


@pytest.fixture()
def service_auth(app_client) -> dict[str, str]:
    """Bearer header signed with the secret the app under test verifies with."""
    from app.config import get_settings
    return {"Authorization": f"Bearer {_token(get_settings().jwt_secret)}"}


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
    resp = await app_client.get(f"/recommendations/{user}?top_k=3")
    assert resp.status_code == 401
    assert resp.headers.get("WWW-Authenticate") == "Bearer"


async def test_recommendations_reject_token_signed_with_other_secret(app_client):
    user = str(uuid.uuid4())
    forged = {"Authorization": f"Bearer {_token('not-the-' + JWT_SECRET)}"}
    resp = await app_client.get(f"/recommendations/{user}?top_k=3", headers=forged)
    assert resp.status_code == 401


async def test_recommendations_404_when_features_missing(app_client, service_auth):
    user = str(uuid.uuid4())
    resp = await app_client.get(f"/recommendations/{user}?top_k=3",
                                headers=service_auth)
    assert resp.status_code == 404
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
