"""Service integration: recommendation_api на настоящих Postgres, Redis и Kafka.

Postgres накатывается НАСТОЯЩИМИ миграциями db_migrations до head (stage C:
``eligible_campaigns`` из 009 — никакого самописного DDL). Сервер — из
``REC_IT_POSTGRES_DSN`` (DSN роли с CREATEDB, например
``postgresql://cashback:cashback@127.0.0.1:5432/postgres``: модуль создаёт
свою БД и удаляет её в конце) или, без переменной, testcontainers. Redis —
testcontainers, Kafka — apache/kafka (KRaft). В файловом MLflow-реестре —
крошечная LightGBM-модель; приложение работает с НАСТОЯЩИМ lifespan
(ModelWatcher грузит модель из реестра, AIOKafkaProducer с zstd,
asyncpg-движок, Redis-клиент). Миграциям нужны alembic и psycopg2 (env.py).

Проверяется:

* stage B (B19, B27): маршруты принимают только service-токены
  (SERVICE_JWT_SECRET) своего издателя: боевая выдача — mobile-api,
  explain — campaign-manager; админский access-токен и токен, подписанный
  JWT_SECRET, — 401, чужой издатель — 403; explain не меняет число строк
  ``recommendations``, офсеты топика ``recommendations.created`` и ключи
  Redis (ни одного keyspace-события записи), а боевая выдача пишет строки,
  публикует события и ставит отметки BRE;
* stage C (B21, B22, B25, B26, B42): кампанию кандидату даёт только
  ``eligible_campaigns`` — в обоих маршрутах: отозванное (последнее)
  согласие, чужой сегмент, RFM вне диапазона или неизвестный, выдача вне
  окна дат кампании, PAUSED/архив, исчерпанный бюджет или дневной лимит →
  оффера нет; минимум категории соблюдается (R2); на MCC — лучшая ставка среди
  положенных клиенту; кандидаты без кампании не тратят лимит показов R4;
  ни один маршрут не ждёт блокировку строки кампании (R6 — read-only).

Каждый сценарий stage C — на своих MCC и своём сегменте клиентов: его
кампании не видны клиентам других тестов модуля.

Marked ``integration`` — the unit gate runs with ``-m "not integration"``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio

# Один event loop на модуль: module-scoped фикстура держит движок, Redis и
# Kafka-продюсер приложения, привязанные к своему loop.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="module")]

REPO = Path(__file__).resolve().parents[4]
MIGRATIONS = REPO / "db_migrations"
KAFKA_IMAGE = "apache/kafka:3.7.2"
TOPIC = "recommendations.created"
MODEL_NAME = "it_cashback_ranker"
FEATURE_COLUMNS = ["recency_days", "frequency_total", "monetary_total", "mcc_code_int"]
FEATURES = {"recency_days": 2, "frequency_total": 30,
            "monetary_total": 50000.0, "segment_id": 5}
# MCC общих кампаний (без таргетинга): их видит каждый клиент с согласием.
# Остальные кандидаты (популярный fallback, 20 MCC) — без кампании и до BRE
# не доходят, кроме MCC сценариев stage C ниже (свои сегменты клиентов).
CAMPAIGN_MCCS = ("5411", "5812", "5541")
ALL_CHANNELS = ["ONLINE", "POS", "MOBILE"]
ADMIN_JWT_SECRET = "it-admin-jwt-secret-0123456789abcdef0123"


# ---------------------------------------------------------------------------
# Postgres at alembic head
# ---------------------------------------------------------------------------
def _plain_dsn(dsn: str) -> str:
    """``postgresql+<driver>://…`` → ``postgresql://…`` (libpq / alembic)."""
    return re.sub(r"^postgres(?:ql)?\+\w+://", "postgresql://", dsn)


@pytest.fixture(scope="module")
def pg_server_dsn():
    dsn = os.getenv("REC_IT_POSTGRES_DSN")
    if dsn:
        yield _plain_dsn(dsn)
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield _plain_dsn(pg.get_connection_url())


@pytest.fixture(scope="module")
def pg_dsn(pg_server_dsn):
    """asyncpg DSN свежей БД, накатанной миграциями db_migrations до head."""
    import psycopg2

    name = f"rec_it_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(pg_server_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        dsn = urlunsplit(urlsplit(pg_server_dsn)._replace(path=f"/{name}"))
        # env.py читает POSTGRES_DSN; пароли bootstrap-учёток 002/008 без
        # переменных — случайные.
        env = {k: v for k, v in os.environ.items()
               if k not in ("ADMIN_BOOTSTRAP_PASSWORD", "DEMO_USERS_PASSWORD")}
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=MIGRATIONS, env={**env, "POSTGRES_DSN": dsn},
            capture_output=True, text=True, timeout=300,
        )
        assert proc.returncode == 0, (
            f"alembic upgrade head failed:\n{proc.stdout}\n{proc.stderr}")
        yield dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


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
# Seed: строки так, как их пишут campaign_manager / сидер / мобильное приложение
# ---------------------------------------------------------------------------
async def _insert_campaign(conn, categories: dict[str, str | None], *,
                           rate: str = "5.00", min_amount: str | None = None,
                           segments: tuple[int, ...] = (),
                           rfm_min: int | None = None, rfm_max: int | None = None,
                           status: str = "ACTIVE", start: datetime | None = None,
                           end: datetime | None = None, budget_total: str = "100000",
                           budget_spent: str = "0", daily_limit: str | None = None,
                           archived: bool = False) -> str:
    """Кампания + её категории (MCC → минимум категории, None — минимум
    кампании); возвращает campaign_id."""
    from sqlalchemy import text

    now = datetime.now(UTC)
    cid = str(uuid.uuid4())
    await conn.execute(text("""
        INSERT INTO cashback_campaigns
          (campaign_id, name, target_segment_ids, cashback_rate,
           min_transaction_amount, budget_total, budget_spent, status,
           start_date, end_date, allowed_channels, daily_limit, rfm_min,
           rfm_max, archived_at)
        VALUES (:cid, :name, :segs, :rate, :min, :total, :spent,
                CAST(:status AS campaign_status), :s, :e, :channels, :daily,
                :rfm_min, :rfm_max, :archived)
    """), {"cid": cid, "name": f"it-{cid[:8]}", "segs": list(segments),
           "rate": Decimal(rate),
           "min": None if min_amount is None else Decimal(min_amount),
           "total": Decimal(budget_total), "spent": Decimal(budget_spent),
           "status": status, "s": start or now - timedelta(days=1),
           "e": end or now + timedelta(days=30), "channels": ALL_CHANNELS,
           "daily": None if daily_limit is None else Decimal(daily_limit),
           "rfm_min": rfm_min, "rfm_max": rfm_max,
           "archived": now if archived else None})
    for mcc, cat_min in categories.items():
        await conn.execute(text(
            "INSERT INTO campaign_categories (campaign_id, mcc_code, "
            "min_transaction_amount) VALUES (:cid, :mcc, :min)"),
            {"cid": cid, "mcc": mcc,
             "min": None if cat_min is None else Decimal(cat_min)})
    return cid


async def _seed_campaign(stack, categories: dict[str, str | None], **kw) -> str:
    async with stack["engine"].begin() as conn:
        return await _insert_campaign(conn, categories, **kw)


async def _seed_user(stack, *, segment_id: int | None = 5, rfm_score: int | None = None,
                     consents: tuple[str, ...] = ("GRANTED",),
                     in_postgres: bool = True) -> str:
    """Клиент: строка ``users``, история согласий ``personalised_cashback``
    (последнее в ``consents`` — самое свежее) и признаки в Redis (горячий
    путь feature store)."""
    from sqlalchemy import text

    uid = str(uuid.uuid4())
    if in_postgres:
        now = datetime.now(UTC)
        async with stack["engine"].begin() as conn:
            await conn.execute(text(
                "INSERT INTO users (user_id, external_id, segment_id, rfm_score) "
                "VALUES (:u, :ext, :seg, :rfm)"),
                {"u": uid, "ext": f"rec-it-{uid}", "seg": segment_id, "rfm": rfm_score})
            for i, consent in enumerate(consents):
                await conn.execute(text(
                    "INSERT INTO user_consents (user_id, consent_type, status, "
                    "document_version, created_at) VALUES (:u, 'personalised_cashback', "
                    "CAST(:s AS consent_status), 'v1', :at)"),
                    {"u": uid, "s": consent,
                     "at": now - timedelta(minutes=len(consents) - i)})
    await stack["redis"].set(f"features:{uid}", json.dumps(FEATURES))
    return uid


async def _seed_accrual(stack, campaign_id: str, user_id: str, mcc: str,
                        cashback: str, accrued_at: datetime) -> None:
    from sqlalchemy import text

    async with stack["engine"].begin() as conn:
        await conn.execute(text("""
            INSERT INTO cashback_accruals
              (user_id, campaign_id, transaction_id, mcc_code,
               transaction_amount, cashback_amount, accrued_at)
            VALUES (:u, :c, :tx, :mcc, :amount, :cb, :at)
        """), {"u": user_id, "c": campaign_id, "tx": f"rec-it-{uuid.uuid4().hex}",
               "mcc": mcc, "amount": Decimal(cashback) * 20, "cb": Decimal(cashback),
               "at": accrued_at})


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(pg_dsn, redis_container, kafka_bootstrap, mlflow_uri):
    """Приложение с настоящим lifespan поверх контейнеров."""
    import redis.asyncio as aioredis
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from sqlalchemy.ext.asyncio import create_async_engine

    redis_url = (f"redis://{redis_container.get_container_host_ip()}"
                 f":{redis_container.get_exposed_port(6379)}/0")

    engine = create_async_engine(pg_dsn)
    campaigns: dict[str, str] = {}
    async with engine.begin() as conn:
        for mcc in CAMPAIGN_MCCS:
            campaigns[mcc] = await _insert_campaign(conn, {mcc: None})

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
        # Сценарии stage C запрашивают до 20 офферов на клиента; лимит
        # показов R4 здесь не проверяется (кроме счётчика — см. ниже).
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
    """Новый клиент сегмента 5 с согласием и признаками в Redis."""
    return await _seed_user(stack)


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


async def _offers(client, uid: str, *, explain: bool = False, **params) -> dict[str, str]:
    """MCC → campaign_id ответа маршрута (top_k=20 — все кандидаты, прошедшие
    BRE)."""
    path = f"/recommendations/{uid}" + ("/explain" if explain else "")
    resp = await client.get(path, params={"top_k": 20, **params},
                            headers=_auth(_token(**(CAMPAIGN_MANAGER if explain
                                                    else MOBILE))))
    assert resp.status_code == 200, resp.text
    return {it["mcc_code"]: it["campaign_id"] for it in resp.json()["recommendations"]}


async def _offers_both(client, uid: str) -> dict[str, str]:
    """Explain, затем боевая выдача: одна и та же выборка кампаний."""
    explained = await _offers(client, uid, explain=True)
    served = await _offers(client, uid)
    assert served == explained
    return served


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
    # Stage C: 17 кандидатов без положенной кампании отброшены до BRE —
    # лимит показов R4 считает только реальные офферы (раньше — все 20).
    assert await stack["redis"].get(f"rec_count:{user_id}") == str(n)

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


async def test_no_route_waits_for_campaign_row_locks(stack, client, user_id):
    """R6 — read-only (stage C): выдача не ждёт блокировок строк кампаний.

    * Начисление держит строку кампании ``FOR UPDATE`` (AccrualEngine, §2):
      explain (только SELECT из eligible_campaigns) отвечает сразу.
    * UPDATE бюджета держит ``FOR NO KEY UPDATE`` (FK-проверку INSERT в
      recommendations он не блокирует): боевая выдача отвечает сразу —
      раньше она ждала SELECT … FOR UPDATE своего R6."""
    from sqlalchemy import text

    for lock, path, issuer in (
        ("FOR UPDATE", f"/recommendations/{user_id}/explain", CAMPAIGN_MANAGER),
        ("FOR NO KEY UPDATE", f"/recommendations/{user_id}", MOBILE),
    ):
        async with stack["engine"].connect() as locker:
            tx = await locker.begin()
            await locker.execute(text(f"SELECT 1 FROM cashback_campaigns {lock}"))
            try:
                resp = await asyncio.wait_for(_call(client, path, issuer), timeout=5.0)
            finally:
                await tx.rollback()
        assert resp.status_code == 200, resp.text
        assert {it["mcc_code"] for it in resp.json()["recommendations"]} \
            == set(CAMPAIGN_MCCS), lock


# ---------------------------------------------------------------------------
# Stage C: кому положена кампания — eligible_campaigns (миграция 009)
# ---------------------------------------------------------------------------
async def test_latest_consent_decides(stack, client):
    """B26: согласие — по ПОСЛЕДНЕЙ записи personalised_cashback (раньше
    согласие не проверялось вовсе). Отозвано, не дано, ещё не подтверждено
    или клиента нет в Postgres → ни одного оффера, включая общие кампании."""
    mcc = "5912"
    cid = await _seed_campaign(stack, {mcc: None}, segments=(21,))
    for consents in (("GRANTED",), ("REVOKED", "GRANTED")):
        uid = await _seed_user(stack, segment_id=21, consents=consents)
        offers = await _offers_both(client, uid)
        assert offers[mcc] == cid, consents
        assert set(offers) == {mcc, *CAMPAIGN_MCCS}, consents
    for consents in (("GRANTED", "REVOKED"), (), ("PENDING",)):
        uid = await _seed_user(stack, segment_id=21, consents=consents)
        assert await _offers_both(client, uid) == {}, consents
    unknown = await _seed_user(stack, segment_id=21, in_postgres=False)
    assert await _offers_both(client, unknown) == {}


async def test_target_segments(stack, client):
    """B26: кампания с target_segment_ids — только клиентам этих сегментов;
    без сегмента (NULL) — не положена."""
    mcc = "5732"
    cid = await _seed_campaign(stack, {mcc: None}, segments=(22, 23))
    member = await _seed_user(stack, segment_id=23)
    assert (await _offers_both(client, member))[mcc] == cid
    for segment in (24, None):
        uid = await _seed_user(stack, segment_id=segment)
        offers = await _offers_both(client, uid)
        assert mcc not in offers, segment
        assert set(offers) == set(CAMPAIGN_MCCS), segment


async def test_rfm_range_is_fail_closed(stack, client):
    """B26: rfm_min/rfm_max — только клиентам с RFM в диапазоне; RFM
    неизвестен (NULL) — не положена."""
    mcc = "5311"
    cid = await _seed_campaign(stack, {mcc: None}, segments=(25,), rfm_min=4, rfm_max=5)
    for score in (4, 5):
        uid = await _seed_user(stack, segment_id=25, rfm_score=score)
        assert (await _offers_both(client, uid))[mcc] == cid, score
    for score in (3, 1, None):
        uid = await _seed_user(stack, segment_id=25, rfm_score=score)
        assert mcc not in await _offers_both(client, uid), score


async def test_date_window(stack, client):
    """B25: ACTIVE кампания предлагается только внутри [start_date, end_date):
    ещё не началась или уже закончилась (планировщик её ещё не закрыл) — нет
    оффера."""
    now = datetime.now(UTC)
    future = await _seed_campaign(stack, {"5651": None}, segments=(26,),
                                  start=now + timedelta(days=1))
    ended = await _seed_campaign(stack, {"5921": None}, segments=(26,),
                                 start=now - timedelta(days=30),
                                 end=now - timedelta(hours=1))
    current = await _seed_campaign(stack, {"5942": None}, segments=(26,))
    uid = await _seed_user(stack, segment_id=26)
    offers = await _offers_both(client, uid)
    assert offers["5942"] == current
    assert "5651" not in offers and "5921" not in offers, (future, ended)


async def test_paused_and_archived_campaigns_are_not_offered(stack, client):
    paused = await _seed_campaign(stack, {"4111": None}, segments=(27,), status="PAUSED")
    archived = await _seed_campaign(stack, {"4121": None}, segments=(27,), archived=True)
    active = await _seed_campaign(stack, {"5462": None}, segments=(27,))
    uid = await _seed_user(stack, segment_id=27)
    offers = await _offers_both(client, uid)
    assert offers["5462"] == active
    assert "4111" not in offers and "4121" not in offers, (paused, archived)


async def test_best_eligible_rate_wins_per_mcc(stack, client):
    """B42: на MCC — лучшая ставка среди ПОЛОЖЕННЫХ клиенту кампаний (раньше
    DISTINCT ON брал самую свежую по start_date, не глядя на таргетинг)."""
    now = datetime.now(UTC)
    await _seed_campaign(stack, {"5499": None}, segments=(28,), rate="3.00",
                         start=now - timedelta(hours=1))
    best = await _seed_campaign(stack, {"5499": None}, segments=(28,), rate="7.00",
                                start=now - timedelta(days=10))
    eligible = await _seed_campaign(stack, {"7011": None}, segments=(28,), rate="4.00",
                                    start=now - timedelta(days=10))
    # Ставка выше и старт свежее, но клиенту сегмента 28 не положена.
    await _seed_campaign(stack, {"7011": None}, segments=(29,), rate="9.00",
                         start=now - timedelta(hours=1))
    uid = await _seed_user(stack, segment_id=28)
    offers = await _offers_both(client, uid)
    assert offers["5499"] == best
    assert offers["7011"] == eligible


async def test_budget_and_daily_limit(stack, client):
    """B22: дневной лимит — по начислениям текущих суток UTC (вчерашние не
    в счёт); исчерпанный бюджет и остаток меньше минимальной выплаты (R6) —
    нет оффера."""
    now = datetime.now(UTC)
    payer = await _seed_user(stack, segment_id=30)
    used_up = await _seed_campaign(stack, {"7832": None}, segments=(30,),
                                   daily_limit="300", budget_spent="300")
    await _seed_accrual(stack, used_up, payer, "7832", "300", now)
    fresh_day = await _seed_campaign(stack, {"5611": None}, segments=(30,),
                                     daily_limit="300", budget_spent="300")
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await _seed_accrual(stack, fresh_day, payer, "5611", "300",
                        midnight - timedelta(minutes=1))
    await _seed_campaign(stack, {"5641": None}, segments=(30,),
                         budget_total="1000", budget_spent="1000")
    await _seed_campaign(stack, {"5722": None}, segments=(30,),
                         budget_total="1000", budget_spent="999.50")

    uid = await _seed_user(stack, segment_id=30)
    offers = await _offers_both(client, uid)
    assert offers["5611"] == fresh_day
    assert not {"7832", "5641", "5722"} & set(offers), offers


async def test_per_category_minimum(stack, client):
    """B21: R2 сравнивает сумму покупки с минимумом КАТЕГОРИИ, а без него —
    с минимумом кампании (раньше — всегда с минимумом кампании). explain
    суммы не знает: минимум проверит начисление.

    Две кампании, а не две категории одной: R3 (anti-fatigue) ключуется
    по (клиент, кампания) и за одну выдачу пропускает кампанию лишь на
    одном MCC."""
    category_min = await _seed_campaign(stack, {"5712": "1000"},
                                        segments=(31,), min_amount="100")
    campaign_min = await _seed_campaign(stack, {"5814": None},
                                        segments=(31,), min_amount="100")
    below = await _seed_user(stack, segment_id=31)
    offers = await _offers(client, below, transaction_amount=500)
    assert offers["5814"] == campaign_min
    assert "5712" not in offers
    for amount in (1000, 99.99):
        uid = await _seed_user(stack, segment_id=31)
        offers = await _offers(client, uid, transaction_amount=amount)
        assert ("5712" in offers) is (amount >= 1000), amount
        assert ("5814" in offers) is (amount >= 100), amount
    explained = await _offers(client, below, explain=True)
    assert explained["5712"] == category_min
    assert explained["5814"] == campaign_min


async def test_unknown_user_is_404_on_both_routes(client):
    unknown = str(uuid.uuid4())
    for path, issuer in ((f"/recommendations/{unknown}", MOBILE),
                         (f"/recommendations/{unknown}/explain", CAMPAIGN_MANAGER)):
        resp = await _call(client, path, issuer)
        assert resp.status_code == 404
        assert "feature store" in resp.json()["detail"]
