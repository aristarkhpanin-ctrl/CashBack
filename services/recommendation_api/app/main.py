"""FastAPI entry-point for the Recommendation API."""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import health as health_router
from app.api import recommendations as recommendations_router
from app.bre.engine import BusinessRulesEngine
from app.candidate_gen import CandidateGenerator
from app.config import Settings, get_settings
from app.feature_store import FeatureStoreClient
from app.model_registry import ModelWatcher

log = structlog.get_logger("app.main")


# ---------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------
class RequestIDMiddleware(BaseHTTPMiddleware):
    HEADER = "X-Request-ID"

    async def dispatch(self, request, call_next):
        rid = request.headers.get(self.HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[self.HEADER] = rid
        return response


# ---------------------------------------------------------------------------
# Optional OpenTelemetry instrumentation — only wired when an endpoint is set.
# ---------------------------------------------------------------------------
def _maybe_install_otel(app: FastAPI, settings: Settings) -> None:
    if not settings.otel_endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": settings.otel_service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
        )
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app)
        log.info("otel_installed", endpoint=settings.otel_endpoint)
    except Exception as exc:  # noqa: BLE001
        log.warning("otel_setup_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Lifespan — wire all clients on startup, shut them down cleanly.
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    log.info("starting", port=settings.api_port,
             mlflow=settings.mlflow_tracking_uri)

    # ---- Redis -------------------------------------------------------
    from redis.asyncio import Redis
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    # ---- ClickHouse (sync client; called via to_thread in feature_store) ---
    import clickhouse_connect
    ch_client: Any | None = None
    try:
        ch_client = clickhouse_connect.get_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_http_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_db,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("clickhouse_init_failed", error=str(exc))

    # ---- Postgres (async) -------------------------------------------
    db_engine = create_async_engine(
        settings.postgres_dsn,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )

    # ---- Feature store + BRE + candidate generator ------------------
    feature_store = FeatureStoreClient(
        redis_client=redis, ch_client=ch_client,
        timeout_ms=settings.feature_store_timeout_ms,
    )
    bre = BusinessRulesEngine()
    candidate_gen = CandidateGenerator()

    # ---- Model watcher (background poll) ----------------------------
    model_watcher = ModelWatcher(
        mlflow_uri=settings.mlflow_tracking_uri,
        model_name=settings.model_name,
        poll_interval_seconds=settings.model_poll_interval_seconds,
        holdout_enabled=settings.holdout_enabled,
        holdout_ratio=settings.holdout_ratio,
        holdout_salt=settings.holdout_salt,
    )
    await model_watcher.start()

    # ---- Try to load SVD++ embeddings from MLflow on startup -------
    try:
        import mlflow
        from mlflow.tracking import MlflowClient
        mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
        mc = MlflowClient()
        try:
            exp = mc.get_experiment_by_name(settings.retrieval_experiment)
        except Exception:
            exp = None
        if exp is not None:
            runs = mc.search_runs(
                [exp.experiment_id],
                order_by=["attributes.start_time DESC"],
                max_results=1,
            )
            if runs:
                await candidate_gen.load_from_mlflow(mc, runs[0].info.run_id)
        if not candidate_gen.loaded:
            log.warning("candidate_gen_running_with_popular_fallback_only")
    except Exception as exc:  # noqa: BLE001
        log.warning("candidate_gen_init_failed", error=str(exc))

    # ---- Kafka producer for recommendations.created events ----------
    kafka_producer: Any | None = None
    try:
        from aiokafka import AIOKafkaProducer

        kafka_producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            compression_type="zstd",
            linger_ms=20,
            acks=1,
        )
        await kafka_producer.start()
        log.info("kafka_producer_started",
                 topic=settings.recommendations_topic)
    except Exception as exc:  # noqa: BLE001
        log.warning("kafka_producer_init_failed", error=str(exc))
        kafka_producer = None

    # ---- Stash on app.state -----------------------------------------
    app.state.settings = settings
    app.state.redis = redis
    app.state.ch_client = ch_client
    app.state.db_engine = db_engine
    app.state.feature_store = feature_store
    app.state.candidate_gen = candidate_gen
    app.state.model_watcher = model_watcher
    app.state.bre = bre
    app.state.kafka_producer = kafka_producer

    log.info("startup_complete", model_version=model_watcher.version)
    try:
        yield
    finally:
        log.info("shutting_down")
        await model_watcher.stop()
        if kafka_producer is not None:
            try:
                await kafka_producer.stop()
            except Exception:
                pass
        try:
            await redis.aclose()
        except Exception:
            pass
        try:
            await db_engine.dispose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# FastAPI factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Cashback Recommendation API",
        version="0.1.0",
        description="Two-tier (SVD++ retrieval → LightGBM ranking) "
                    "personalised cashback recommendations.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=512)
    app.add_middleware(RequestIDMiddleware)

    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
    _maybe_install_otel(app, settings)

    app.include_router(health_router.router)
    app.include_router(recommendations_router.router)

    @app.get("/", tags=["meta"])
    async def root(request: Request) -> dict[str, Any]:
        return {
            "service": "recommendation-api",
            "version": "0.1.0",
            "model_version": request.app.state.model_watcher.version,
            "request_id": request.state.request_id,
            "docs": "/docs",
            "metrics": "/metrics",
        }

    return app


app = create_app()
