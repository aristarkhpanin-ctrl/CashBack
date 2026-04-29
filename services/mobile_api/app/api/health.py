"""Liveness + readiness for the Mobile BFF."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

log = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def _ping_postgres(engine: Any) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _ping_redis(redis: Any) -> None:
    await redis.ping()


async def _ping_recommendation_api(http_client: httpx.AsyncClient,
                                   base_url: str) -> None:
    resp = await http_client.get(f"{base_url}/health/live", timeout=2.0)
    resp.raise_for_status()


async def _check(label: str, coro) -> tuple[str, bool]:
    try:
        await asyncio.wait_for(coro, timeout=2.0)
        return label, True
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness_check_failed component=%s: %s", label, exc)
        return label, False


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    state = request.app.state
    settings = state.settings

    pairs = await asyncio.gather(
        _check("postgres", _ping_postgres(state.db_engine)),
        _check("redis",    _ping_redis(state.redis)),
        _check("recommendation_api",
               _ping_recommendation_api(state.http_client,
                                        settings.recommendation_api_url)),
    )
    checks = dict(pairs)
    checks["circuit_breaker"] = state.recommendation_client.breaker.state
    if all(v for k, v in checks.items() if k != "circuit_breaker"):
        return {"status": "ready", "checks": checks}
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"status": "not_ready", "checks": checks},
    )
