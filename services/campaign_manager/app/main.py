"""FastAPI entry-point for the Campaign Manager."""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import (
    ab_testing,
    analytics,
    auth,
    campaigns,
    events,
    health,
    ml_explain,
    ml_limits,
    reference,
    roles,
)
from app.config import Settings, get_settings
from app.db import make_engine, make_sessionmaker
from app.scheduling import CampaignScheduler

log = structlog.get_logger("app.main")


class RequestIDMiddleware(BaseHTTPMiddleware):
    HEADER = "X-Request-ID"

    async def dispatch(self, request, call_next):
        rid = request.headers.get(self.HEADER) or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[self.HEADER] = rid
        return response


class SSENoGzipMiddleware:
    """Снимает Accept-Encoding для /events/, чтобы GZipMiddleware не
    буферизовал SSE-поток (фаза 19). Чистый ASGI — добавляется ПОСЛЕ
    GZip, поэтому правит запрос до того, как gzip его увидит."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/events/"):
            scope = dict(scope)
            scope["headers"] = [
                (k, v) for (k, v) in scope["headers"]
                if k.lower() != b"accept-encoding"
            ]
        await self.app(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    log.info("starting", port=settings.api_port)

    db_engine = make_engine(settings.postgres_dsn)
    sessionmaker = make_sessionmaker(db_engine)

    from redis.asyncio import Redis
    redis = Redis.from_url(settings.redis_url, decode_responses=True)

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

    scheduler = CampaignScheduler(
        db_engine, ch_client,
        redis_client=redis,
        interval_minutes=settings.scheduling_interval_minutes,
        threshold_ratio=settings.daily_budget_pause_threshold,
    )
    try:
        scheduler.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler_start_failed", error=str(exc))

    # SSE realtime (фаза 19): брокер + мост из топика cashback.accrued.
    from app.events import AccrualStreamBridge, EventBroker
    event_broker = EventBroker()
    sse_bridge = AccrualStreamBridge(
        event_broker, bootstrap=settings.kafka_bootstrap_servers,
    )
    try:
        await sse_bridge.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("sse_bridge_start_failed", error=str(exc))

    app.state.settings = settings
    app.state.db_engine = db_engine
    app.state.sessionmaker = sessionmaker
    app.state.redis = redis
    app.state.ch_client = ch_client
    app.state.scheduler = scheduler
    app.state.event_broker = event_broker
    app.state.sse_bridge = sse_bridge

    log.info("startup_complete")
    try:
        yield
    finally:
        log.info("shutting_down")
        try:
            await sse_bridge.stop()
        except Exception:
            pass
        try:
            scheduler.shutdown()
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
        title="Cashback Campaign Manager",
        version="0.1.0",
        description=("Campaign CRUD, applicability filtering, A/B testing "
                     "and analytics for the CashBack stack."),
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
    app.add_middleware(SSENoGzipMiddleware)  # раньше GZip на пути запроса
    app.add_middleware(RequestIDMiddleware)

    Instrumentator().instrument(app).expose(app, endpoint="/metrics")

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(campaigns.router)
    app.include_router(analytics.router)
    app.include_router(ab_testing.router)
    app.include_router(ml_limits.router)
    app.include_router(reference.router)
    app.include_router(ml_explain.router)
    app.include_router(roles.router)
    app.include_router(events.router)

    @app.get("/", tags=["meta"])
    async def root(request: Request) -> dict[str, Any]:
        return {
            "service": "campaign-manager",
            "version": "0.1.0",
            "request_id": request.state.request_id,
            "docs": "/docs",
            "metrics": "/metrics",
        }

    return app


app = create_app()
