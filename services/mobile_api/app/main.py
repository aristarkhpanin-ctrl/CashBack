"""FastAPI entry-point for the Mobile BFF (port 8003)."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import health, mobile
from app.clients.recommendation_client import (
    CircuitBreaker,
    RecommendationClient,
)
from app.config import Settings, get_settings
from app.scheduling import SnoozeScheduler

log = structlog.get_logger("app.main")


class RequestIDMiddleware(BaseHTTPMiddleware):
    HEADER = "X-Request-ID"

    async def dispatch(self, request, call_next):
        rid = request.headers.get(self.HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[self.HEADER] = rid
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    log.info("starting", port=settings.api_port)

    db_engine = create_async_engine(
        settings.postgres_dsn, pool_pre_ping=True,
        pool_size=10, max_overflow=20,
    )

    from redis.asyncio import Redis
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    http_client = httpx.AsyncClient(timeout=settings.recommendation_timeout_seconds)
    breaker = CircuitBreaker(
        failure_threshold=settings.circuit_breaker_failure_threshold,
        reset_seconds=settings.circuit_breaker_reset_seconds,
    )
    from app.service_token import ServiceTokenProvider
    service_tokens = ServiceTokenProvider(
        settings.jwt_secret, ttl_seconds=settings.service_token_ttl_seconds,
    )
    rec_client = RecommendationClient(
        base_url=settings.recommendation_api_url,
        http_client=http_client,
        breaker=breaker,
        max_attempts=settings.recommendation_retry_attempts,
        auth_header_provider=service_tokens.auth_header,
    )

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
    except Exception as exc:  # noqa: BLE001
        log.warning("kafka_producer_init_failed", error=str(exc))
        kafka_producer = None

    snooze = SnoozeScheduler(db_engine)
    snooze.start()

    app.state.settings = settings
    app.state.db_engine = db_engine
    app.state.redis = redis
    app.state.http_client = http_client
    app.state.recommendation_client = rec_client
    app.state.kafka_producer = kafka_producer
    app.state.snooze_scheduler = snooze

    log.info("startup_complete")
    try:
        yield
    finally:
        log.info("shutting_down")
        try:
            snooze.shutdown()
        except Exception:
            pass
        if kafka_producer is not None:
            try:
                await kafka_producer.stop()
            except Exception:
                pass
        try:
            await http_client.aclose()
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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Cashback Mobile BFF",
        version="0.1.0",
        description=("Backend-for-Frontend for the mobile client. "
                     "Wraps Recommendation API + Postgres + Redis."),
        openapi_version="3.1.0",
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

    app.include_router(health.router)
    app.include_router(mobile.router)

    @app.get("/", tags=["meta"])
    async def root(request: Request) -> dict[str, Any]:
        return {
            "service": "mobile-api",
            "version": "0.1.0",
            "request_id": request.state.request_id,
            "docs": "/docs",
            "metrics": "/metrics",
        }

    return app


app = create_app()
