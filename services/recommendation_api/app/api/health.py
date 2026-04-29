"""Liveness + readiness probes."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

log = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, str]:
    """Trivial liveness — process is up."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
async def _check(label: str, coro) -> tuple[str, bool]:
    try:
        await asyncio.wait_for(coro, timeout=2.0)
        return label, True
    except Exception as exc:  # noqa: BLE001
        log.warning("readiness_check_failed component=%s: %s", label, exc)
        return label, False


async def _check_postgres(engine: Any) -> None:
    from sqlalchemy import text
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(redis_client: Any) -> None:
    await redis_client.ping()


async def _check_clickhouse(ch_client: Any) -> None:
    if ch_client is None:
        raise RuntimeError("clickhouse client not initialised")
    await asyncio.to_thread(ch_client.command, "SELECT 1")


async def _check_mlflow(mlflow_uri: str) -> None:
    import httpx
    async with httpx.AsyncClient(timeout=2.0) as client:
        resp = await client.get(f"{mlflow_uri.rstrip('/')}/health")
        resp.raise_for_status()


# ---------------------------------------------------------------------------
@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    """Composite readiness — checks all hard dependencies."""
    state = request.app.state
    settings = state.settings

    pairs = await asyncio.gather(
        _check("postgres",   _check_postgres(state.db_engine)),
        _check("redis",      _check_redis(state.redis)),
        _check("clickhouse", _check_clickhouse(state.ch_client)),
        _check("mlflow",     _check_mlflow(settings.mlflow_tracking_uri)),
    )
    checks = {name: ok for name, ok in pairs}
    checks["model_loaded"] = bool(state.model_watcher.loaded)

    if all(checks.values()):
        return {"status": "ready", "checks": checks,
                "model_version": state.model_watcher.version}

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"status": "not_ready", "checks": checks,
                "model_version": state.model_watcher.version},
    )
