"""Liveness + readiness probes for the Campaign Manager."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import text

log = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def _check(label: str, coro) -> tuple[str, bool]:
    try:
        await asyncio.wait_for(coro, timeout=2.0)
        return label, True
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness_check_failed component=%s: %s", label, exc)
        return label, False


async def _ping_postgres(engine: Any) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _ping_redis(redis: Any) -> None:
    await redis.ping()


async def _ping_clickhouse(ch: Any) -> None:
    if ch is None:
        raise RuntimeError("clickhouse not initialised")
    await asyncio.to_thread(ch.command, "SELECT 1")


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    state = request.app.state
    pairs = await asyncio.gather(
        _check("postgres",   _ping_postgres(state.db_engine)),
        _check("redis",      _ping_redis(state.redis)),
        _check("clickhouse", _ping_clickhouse(state.ch_client)),
    )
    checks = dict(pairs)
    if all(checks.values()):
        return {"status": "ready", "checks": checks}
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"status": "not_ready", "checks": checks},
    )
