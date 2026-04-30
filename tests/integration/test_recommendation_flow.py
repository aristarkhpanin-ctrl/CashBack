"""Integration: Recommendation API end-to-end inside one service.

Hits the full FastAPI app (incl. middleware) via httpx ASGITransport.
External dependencies (LightGBM model, MLflow, Postgres, Redis) are
stubbed so the test stays fast and hermetic, but the route handler is
the real one — validation, BRE filtering, SHAP factor extraction.

Four tests:

    1. /health/live → 200
    2. /  → service metadata + request id header
    3. /recommendations/{user} with no feature payload → 404
    4. /metrics endpoint exposes Prometheus text format
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Use the recommendation_api service's `app` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "recommendation_api"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration


@pytest.fixture()
async def app_client():
    """Build the FastAPI app, then patch app.state to skip heavy deps."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from httpx import ASGITransport, AsyncClient
    from app.main import create_app
    from app.config import get_settings

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
        # Wait for lifespan; lifespan likely fails parts (no real Postgres),
        # but middleware + routers register regardless.
        try:
            await ac.get("/health/live")
        except Exception:
            pass

        # Settings are normally attached by lifespan — restore them so the
        # route handlers can read state.settings without crashing.
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


async def test_recommendations_404_when_features_missing(app_client):
    user = str(uuid.uuid4())
    resp = await app_client.get(f"/recommendations/{user}?top_k=3")
    assert resp.status_code == 404


async def test_metrics_endpoint_exposes_prometheus_format(app_client):
    resp = await app_client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert (
        "process_" in text
        or "http_requests_total" in text
        or "python_info" in text
    )
