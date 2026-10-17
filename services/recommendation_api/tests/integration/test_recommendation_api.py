"""Service integration: recommendation_api на настоящих Postgres, Redis и Kafka.

Поднимает Postgres + Redis (testcontainers) и Kafka (apache/kafka, KRaft),
регистрирует крошечную LightGBM-модель в файловом MLflow-реестре и
запускает приложение с НАСТОЯЩИМ lifespan (ModelWatcher грузит модель из
реестра, AIOKafkaProducer с zstd, asyncpg-движок, Redis-клиент).

Проверяется (stage B: B19, B27):

* маршруты принимают только service-токены (SERVICE_JWT_SECRET) своего
  издателя: боевая выдача — mobile-api, explain — campaign-manager;
  админский access-токен и токен, подписанный JWT_SECRET, — 401, чужой
  издатель — 403;
* explain не меняет число строк ``recommendations``, офсеты топика
  ``recommendations.created`` и ключи Redis (ни одного keyspace-события
  записи) и не ждёт блокировку строки кампании, а боевая выдача по-прежнему
  пишет строки, публикует события, ставит отметки BRE и берёт
  ``FOR UPDATE``.

Marked ``integration`` — the unit gate runs with ``-m "not integration"``.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

# Один event loop на модуль: module-scoped фикстура держит движок, Redis и
# Kafka-продюсер приложения, привязанные к своему loop.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

KAFKA_IMAGE = "apache/kafka:3.7.2"
TOPIC = "recommendations.created"
MODEL_NAME = "it_cashback_ranker"
FEATURE_COLUMNS = ["recency_days", "frequency_total", "monetary_total", "mcc_code_int"]
# MCC с кампаниями; остальные кандидаты (популярный fallback) без кампании
# отсеиваются BRE и в выдаче, и в explain.
CAMPAIGN_MCCS = ("5411", "5812", "5541")
ADMIN_JWT_SECRET = "it-admin-jwt-secret-0123456789abcdef0123"


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def postgres_container():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg


@pytest.fixture(scope="module")
def redis_container():
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7.2-alpine") as r:
        yield r


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def kafka_bootstrap():
    """Однонодовый KRaft-брокер; внешний листенер на свободном порту хоста."""
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    port = _free_port()
    kafka = (
        DockerContainer(KAFKA_IMAGE)
        .with_bind_ports(9094, port)
        .with_envs(
            CLUSTER_ID="m0fPzfelTjSXE-sbwO0Wtw",
            KAFKA_NODE_ID="1",
            KAFKA_PROCESS_ROLES="broker,controller",
            KAFKA_CONTROLLER_QUORUM_VOTERS="1@localhost:9093",
            KAFKA_LISTENERS="INTERNAL://:9092,CONTROLLER://:9093,EXTERNAL://:9094",
            KAFKA_ADVERTISED_LISTENERS=(
                f"INTERNAL://localhost:9092,EXTERNAL://localhost:{port}"),
            KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=(
                "INTERNAL:PLAINTEXT,CONTROLLER:PLAINTEXT,EXTERNAL:PLAINTEXT"),
            KAFKA_CONTROLLER_LISTENER_NAMES="CONTROLLER",
            KAFKA_INTER_BROKER_LISTENER_NAME="INTERNAL",
            KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR="1",
            KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR="1",
            KAFKA_TRANSACTION_STATE_LOG_MIN_ISR="1",
        )
    )
    with kafka:
        wait_for_logs(kafka, "Kafka Server started", timeout=120)
        yield f"localhost:{port}"


# ---------------------------------------------------------------------------
# Model registry: файловый MLflow с одной Production-версией
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def mlflow_uri(tmp_path_factory) -> str:
    import lightgbm as lgb
    import mlflow
    import mlflow.lightgbm
    import numpy as np
    import pandas as pd
    from mlflow.tracking import MlflowClient

    uri = f"file://{tmp_path_factory.mktemp('mlruns')}"
    mlflow.set_tracking_uri(uri)
    rng = np.random.default_rng(7)
    X = pd.DataFrame(rng.integers(0, 6000, size=(300, len(FEATURE_COLUMNS))),
                     columns=FEATURE_COLUMNS).astype("float32")
    y = (X["mcc_code_int"] < 5600).astype(int)
    model = lgb.LGBMClassifier(n_estimators=10, min_child_samples=2,
                               verbose=-1).fit(X, y)
    mlflow.set_experiment("it_recommendation_api")
    with mlflow.start_run():
        mlflow.lightgbm.log_model(model, "model", registered_model_name=MODEL_NAME,
                                  pip_requirements=["lightgbm"])
        mlflow.log_dict({"feature_columns": FEATURE_COLUMNS},
                        "training_fingerprint.json")
    client = MlflowClient()
    version = client.get_latest_versions(MODEL_NAME, stages=["None"])[0]
    client.transition_model_version_stage(MODEL_NAME, version.version, "Production")
    return uri


# ---------------------------------------------------------------------------
# Schema + seed (подмножество схемы alembic, которое читает/пишет rec_api)
# ---------------------------------------------------------------------------
_DDL = [
    "CREATE TYPE campaign_status AS ENUM ('DRAFT','ACTIVE','PAUSED','COMPLETED')",
    "CREATE TYPE recommendation_response_status AS ENUM "
    "('PENDING','ACCEPTED','DECLINED','EXPIRED','SNOOZE')",
    """CREATE TABLE cashback_campaigns (
        campaign_id UUID PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        cashback_rate NUMERIC(5,2) NOT NULL,
        min_transaction_amount NUMERIC(15,2),
        budget_total NUMERIC(15,2) NOT NULL,
        budget_spent NUMERIC(15,2) NOT NULL DEFAULT 0,
        status campaign_status NOT NULL DEFAULT 'DRAFT',
        start_date TIMESTAMPTZ NOT NULL,
        end_date TIMESTAMPTZ NOT NULL,
        allowed_channels TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[]
    )""",
    """CREATE TABLE campaign_categories (
        campaign_id UUID NOT NULL REFERENCES cashback_campaigns ON DELETE CASCADE,
        mcc_code CHAR(4) NOT NULL,
        PRIMARY KEY (campaign_id, mcc_code)
    )""",
    """CREATE TABLE recommendations (
        recommendation_id UUID PRIMARY KEY,
        user_id UUID NOT NULL,
        campaign_id UUID NOT NULL REFERENCES cashback_campaigns ON DELETE CASCADE,
        mcc_code CHAR(4) NOT NULL,
        model_score REAL NOT NULL,
        generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        response_status recommendation_response_status NOT NULL DEFAULT 'PENDING',
        expires_at TIMESTAMPTZ NOT NULL,
        model_version VARCHAR(64)
    )""",
]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(postgres_container, redis_container, kafka_bootstrap, mlflow_uri):
    """Приложение с настоящим lifespan поверх контейнеров."""
    import redis.asyncio as aioredis
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    pg_dsn = postgres_container.get_connection_url().replace(
        "postgresql+psycopg2", "postgresql+asyncpg")
    redis_url = (f"redis://{redis_container.get_container_host_ip()}"
                 f":{redis_container.get_exposed_port(6379)}/0")

    engine = create_async_engine(pg_dsn)
    now = datetime.now(UTC)
    campaigns: dict[str, str] = {}
    async with engine.begin() as conn:
        for stmt in _DDL:
            await conn.execute(text(stmt))
        for mcc in CAMPAIGN_MCCS:
            cid = str(uuid.uuid4())
            campaigns[mcc] = cid
            await conn.execute(text("""
                INSERT INTO cashback_campaigns
                  (campaign_id, name, cashback_rate, budget_total, budget_spent,
                   status, start_date, end_date, allowed_channels)
                VALUES (:cid, :name, 5.0, 100000, 0, 'ACTIVE', :s, :e,
                        ARRAY['ONLINE','POS','MOBILE'])
            """), {"cid": cid, "name": f"it-{mcc}", "s": now - timedelta(days=1),
                   "e": now + timedelta(days=30)})
            await conn.execute(
                text("INSERT INTO campaign_categories VALUES (:cid, :mcc)"),
                {"cid": cid, "mcc": mcc})

    admin = AIOKafkaAdminClient(bootstrap_servers=kafka_bootstrap)
    await admin.start()
    try:
        await admin.create_topics([NewTopic(TOPIC, num_partitions=1,
                                            replication_factor=1)])
    finally:
        await admin.close()

    r = aioredis.Redis.from_url(redis_url, decode_responses=True)
    # Keyspace-события: тест видит любую запись в Redis (set/expire/incr…).
    await r.config_set("notify-keyspace-events", "KEA")

    env = {
        "POSTGRES_DSN": pg_dsn,
        "REDIS_URL": redis_url,
        "KAFKA_BOOTSTRAP_SERVERS": kafka_bootstrap,
        "RECOMMENDATIONS_TOPIC": TOPIC,
        "MLFLOW_TRACKING_URI": mlflow_uri,
        "MODEL_NAME": MODEL_NAME,
        "HOLDOUT_ENABLED": "false",
        # ClickHouse не нужен: признаки лежат в Redis (горячий путь).
        "CLICKHOUSE_HOST": "127.0.0.1",
        "CLICKHOUSE_HTTP_PORT": str(_free_port()),
        # B28 (R4 считает и кандидатов без кампании) — вне этого теста.
        "FREQUENCY_GATE_PER_24H": "100",
        # JWT_SECRET в окружении есть, но rec_api его игнорирует.
        "JWT_SECRET": ADMIN_JWT_SECRET,
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            assert app.state.model_watcher.loaded, "model not loaded from MLflow"
            assert app.state.kafka_producer is not None, "kafka producer not started"
            yield {"app": app, "engine": engine, "redis": r,
                   "kafka": kafka_bootstrap, "campaigns": campaigns}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()
        await r.aclose()
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def client(stack):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=stack["app"])
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(loop_scope="module")
async def user_id(stack) -> str:
    """Новый клиент с признаками в Redis (горячий путь feature store)."""
    uid = str(uuid.uuid4())
    await stack["redis"].set(f"features:{uid}", json.dumps({
        "recency_days": 2, "frequency_total": 30,
        "monetary_total": 50000.0, "segment_id": 5,
    }))
    return uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _token(*, iss: str = "mobile-api", aud: str = "recommendation-api",
           token_type: str = "service", secret: str | None = None,
           drop: tuple[str, ...] = ()) -> str:
    import time

    from app.config import get_settings
    from jose import jwt

    now = int(time.time())
    claims = {"iss": iss, "sub": iss, "aud": aud, "type": token_type,
              "iat": now, "exp": now + 300}
    for name in drop:
        claims.pop(name)
    key = secret if secret is not None else \
        get_settings().service_jwt_secret.get_secret_value()
    return jwt.encode(claims, key, algorithm="HS256")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


MOBILE = {"iss": "mobile-api"}
CAMPAIGN_MANAGER = {"iss": "campaign-manager"}


async def _rec_rows(stack) -> int:
    from sqlalchemy import text

    async with stack["engine"].connect() as conn:
        return (await conn.execute(text("SELECT count(*) FROM recommendations"))).scalar_one()


async def _end_offsets(stack) -> dict[int, int]:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    consumer = AIOKafkaConsumer(bootstrap_servers=stack["kafka"])
    await consumer.start()
    try:
        parts = [TopicPartition(TOPIC, p)
                 for p in sorted(consumer.partitions_for_topic(TOPIC) or {0})]
        offsets = await consumer.end_offsets(parts)
        return {tp.partition: off for tp, off in offsets.items()}
    finally:
        await consumer.stop()


async def _redis_snapshot(stack) -> dict[str, tuple[str | None, int]]:
    r = stack["redis"]
    return {key: (await r.get(key), await r.pttl(key))
            for key in sorted([k async for k in r.scan_iter()])}


class _KeyspaceWrites:
    """Подписка на keyspace-события Redis: каждая запись (set, expire, incr,
    del…) любого ключа приходит сюда сообщением."""

    def __init__(self, redis) -> None:
        self._pubsub = redis.pubsub()

    async def __aenter__(self):
        await self._pubsub.psubscribe("__keyevent@*__:*")
        await self._pubsub.get_message(timeout=1.0)  # подтверждение подписки
        return self

    async def drain(self, settle: float = 0.5) -> list[tuple[str, str]]:
        events = []
        while True:
            msg = await self._pubsub.get_message(ignore_subscribe_messages=True,
                                                 timeout=settle)
            if msg is None:
                return events
            events.append((msg["channel"].rsplit(":", 1)[-1], msg["data"]))

    async def __aexit__(self, *exc):
        await self._pubsub.punsubscribe()
        await self._pubsub.aclose()


async def _call(client, path: str, issuer: dict):
    return await client.get(path, headers=_auth(_token(**issuer)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_health_and_metrics_are_public(client):
    assert (await client.get("/health/live")).status_code == 200
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "http_requests_total" in metrics.text or "process_" in metrics.text
    root = await client.get("/")
    assert root.status_code == 200 and root.headers.get("X-Request-ID")


_ADMIN = {"iss": "cashback-campaign-manager", "aud": "cashback-admin",
          "token_type": "access", "secret": ADMIN_JWT_SECRET}
# Прежний формат (без iss/aud), JWT_SECRET: раньше rec_api его принимал.
_ADMIN_LEGACY = {"token_type": "access", "secret": ADMIN_JWT_SECRET,
                 "drop": ("iss", "aud")}
_SERVICE_JWT_SECRET_SIGNED = {"secret": ADMIN_JWT_SECRET}
_SERVICE_JWT_SECRET_LEGACY = {"secret": ADMIN_JWT_SECRET, "drop": ("iss", "aud")}


@pytest.mark.parametrize(
    ("suffix", "token_kwargs", "expected"),
    [
        pytest.param("", None, 401, id="serving-no-token"),
        pytest.param("", _ADMIN, 401, id="serving-admin-access"),
        pytest.param("", _ADMIN_LEGACY, 401, id="serving-admin-access-legacy"),
        pytest.param("", _SERVICE_JWT_SECRET_SIGNED, 401, id="serving-jwt-secret-signed"),
        pytest.param("", _SERVICE_JWT_SECRET_LEGACY, 401,
                     id="serving-jwt-secret-signed-legacy"),
        pytest.param("", {"secret": ""}, 401, id="serving-empty-key-forged"),
        pytest.param("", CAMPAIGN_MANAGER, 403, id="serving-campaign-manager"),
        pytest.param("/explain", None, 401, id="explain-no-token"),
        pytest.param("/explain", _ADMIN, 401, id="explain-admin-access"),
        pytest.param("/explain", _ADMIN_LEGACY, 401, id="explain-admin-access-legacy"),
        pytest.param("/explain", {**_SERVICE_JWT_SECRET_SIGNED, "iss": "campaign-manager"},
                     401, id="explain-jwt-secret-signed"),
        pytest.param("/explain", MOBILE, 403, id="explain-mobile-api"),
    ],
)
async def test_rejected_callers_cause_no_side_effects(stack, client, user_id, suffix,
                                                      token_kwargs, expected):
    rows, offsets = await _rec_rows(stack), await _end_offsets(stack)
    headers = {} if token_kwargs is None else _auth(_token(**token_kwargs))
    resp = await client.get(f"/recommendations/{user_id}{suffix}", headers=headers)
    assert resp.status_code == expected, resp.text
    await stack["app"].state.kafka_producer.flush()
    assert await _rec_rows(stack) == rows
    assert await _end_offsets(stack) == offsets
    assert await stack["redis"].keys(f"*{user_id}*") == [f"features:{user_id}"]


async def test_explain_is_read_only_serving_persists_and_publishes(stack, client,
                                                                   user_id):
    producer = stack["app"].state.kafka_producer
    explain_path = f"/recommendations/{user_id}/explain"

    # 1) explain: ни строк, ни событий, ни записей в Redis.
    rows, offsets = await _rec_rows(stack), await _end_offsets(stack)
    redis_before = await _redis_snapshot(stack)
    async with _KeyspaceWrites(stack["redis"]) as writes:
        resp = await _call(client, explain_path, CAMPAIGN_MANAGER)
        await producer.flush()
        events = await writes.drain()
    assert resp.status_code == 200, resp.text
    explained = resp.json()
    assert {it["mcc_code"] for it in explained["recommendations"]} == set(CAMPAIGN_MCCS)
    assert explained["model_version"] == "1"
    assert all(it["top_factors"] for it in explained["recommendations"])
    assert events == []
    assert await _rec_rows(stack) == rows
    assert await _end_offsets(stack) == offsets
    assert await _redis_snapshot(stack) == redis_before

    # 2) боевая выдача: тот же ответ; строки, события, отметки BRE.
    async with _KeyspaceWrites(stack["redis"]) as writes:
        resp = await _call(client, f"/recommendations/{user_id}", MOBILE)
        await producer.flush()
        events = await writes.drain()
    assert resp.status_code == 200, resp.text
    served = resp.json()
    assert served["recommendations"] == explained["recommendations"]
    n = len(served["recommendations"])
    assert await _rec_rows(stack) == rows + n
    new_offsets = await _end_offsets(stack)
    assert sum(new_offsets.values()) == sum(offsets.values()) + n
    written = {key for _, key in events}
    assert f"rec_count:{user_id}" in written
    assert {f"recent_offer:{user_id}:{cid}"
            for cid in stack["campaigns"].values()} <= written

    # 3) explain после выдачи — снова без единой записи (TTL не продлеваются).
    rows, offsets = await _rec_rows(stack), new_offsets
    redis_before = await _redis_snapshot(stack)
    async with _KeyspaceWrites(stack["redis"]) as writes:
        resp = await _call(client, explain_path, CAMPAIGN_MANAGER)
        await producer.flush()
        events = await writes.drain()
    assert resp.status_code == 200
    assert resp.json()["recommendations"] == explained["recommendations"]
    assert events == []
    assert await _rec_rows(stack) == rows
    assert await _end_offsets(stack) == offsets
    redis_after = await _redis_snapshot(stack)
    assert redis_after.keys() == redis_before.keys()
    for key, (value, pttl) in redis_after.items():
        assert value == redis_before[key][0]
        assert pttl <= redis_before[key][1], f"TTL of {key} was refreshed"


async def test_explain_takes_no_budget_row_lock(stack, client, user_id):
    """Пока другая транзакция держит строки кампаний (как UPDATE бюджета при
    начислении: FOR NO KEY UPDATE — FK-проверку INSERT он не блокирует),
    explain отвечает сразу (обычный SELECT), а боевая выдача ждёт
    блокировку R6 (SELECT … FOR UPDATE)."""
    from sqlalchemy import text

    async with stack["engine"].connect() as locker:
        tx = await locker.begin()
        await locker.execute(
            text("SELECT 1 FROM cashback_campaigns FOR NO KEY UPDATE"))

        resp = await asyncio.wait_for(
            _call(client, f"/recommendations/{user_id}/explain", CAMPAIGN_MANAGER),
            timeout=5.0)
        assert resp.status_code == 200
        assert resp.json()["recommendations"]

        serving = asyncio.ensure_future(
            _call(client, f"/recommendations/{user_id}", MOBILE))
        await asyncio.sleep(1.5)
        assert not serving.done(), "serving must wait for the budget row lock"
        await tx.rollback()
    resp = await asyncio.wait_for(serving, timeout=10.0)
    assert resp.status_code == 200
    assert resp.json()["recommendations"]


async def test_unknown_user_is_404_on_both_routes(client):
    unknown = str(uuid.uuid4())
    for path, issuer in ((f"/recommendations/{unknown}", MOBILE),
                         (f"/recommendations/{unknown}/explain", CAMPAIGN_MANAGER)):
        resp = await _call(client, path, issuer)
        assert resp.status_code == 404
        assert "feature store" in resp.json()["detail"]
