"""HTTP-фикстура recommendation_api с подменёнными внешними клиентами.

``create_app()`` без lifespan: app.state собирается из подделок
(tests/unit/fakes.py), маршруты и правила BRE — настоящие.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from tests.unit.fakes import (
    CANDIDATES,
    FEATURE_COLUMNS,
    FEATURES,
    USER_ID,
    FakeEngine,
    FakeExplainer,
    FakeModel,
    FakeProducer,
    RecApp,
    RecordingRedis,
    campaign_row,
)


@pytest.fixture()
def campaigns() -> dict[str, dict]:
    return {mcc: campaign_row(mcc, n) for n, mcc in enumerate(CANDIDATES, start=1)}


@pytest_asyncio.fixture()
async def rec_app(campaigns):
    """Приложение без lifespan: app.state собирается из подделок."""
    from app.bre.engine import BusinessRulesEngine
    from app.bre.rules import ml_rate_cap
    from app.config import get_settings
    from app.feature_store import FeatureStoreClient
    from app.main import create_app
    from app.model_registry import LoadedModel
    from httpx import ASGITransport, AsyncClient

    ml_rate_cap._reset_cache()  # снапшот R7 кэшируется на процесс
    app = create_app()
    redis = RecordingRedis(decode_responses=True)
    await redis.set(f"features:{USER_ID}", json.dumps(FEATURES))
    db = FakeEngine(campaigns)
    producer = FakeProducer()
    model = LoadedModel(model=FakeModel(), explainer=FakeExplainer(),
                        version="7", feature_columns=FEATURE_COLUMNS)

    st = app.state
    st.settings = get_settings()
    st.redis = redis
    st.db_engine = db
    st.feature_store = FeatureStoreClient(redis_client=redis, ch_client=None)
    st.candidate_gen = SimpleNamespace(retrieve=lambda user_id, k: list(CANDIDATES))
    st.model_watcher = SimpleNamespace(
        version="7", get_for_user=AsyncMock(return_value=(model, "prod")),
    )
    st.bre = BusinessRulesEngine()
    st.kafka_producer = producer

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield RecApp(client=client, app=app, redis=redis, db=db, producer=producer)
    await redis.aclose()
    ml_rate_cap._reset_cache()
