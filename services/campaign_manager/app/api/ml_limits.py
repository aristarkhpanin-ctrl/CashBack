"""ML-лимиты (фаза 18): бизнес управляет ограничениями ML через UI.

``GET /ml-limits``  — текущие лимиты по корзинам + глобальный выключатель
(право ``analytics``).
``PUT /ml-limits``  — upsert (только роль ADMIN: kill switch ML не
делегируется через матрицу прав); после коммита в Postgres в Redis
публикуется свежий снапшот, чтобы BRE-правило R7 в recommendation_api
подхватило изменения сразу (горячий путь рекомендаций читает только
снапшот и никогда не ходит в Postgres за конфигом). Не удалось
опубликовать → 503 (B39: раньше — 200, и выключенный kill switch молча не
действовал): изменение уже в БД, его переиздаст отдельная задача
планировщика ``republish-ml-limits`` (каждые 60 с) — она же
самовосстановление после рестарта Redis.

Ключ снапшота ``ml_limits:snapshot`` — общий контракт с
``recommendation_api/app/bre/rules/ml_rate_cap.py`` (зеркальный комментарий там).
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import MlLimit
from app.rbac import require_permission
from app.schemas import MlLimitItem, MlLimitsResponse, MlLimitsUpdate
from app.security import AuthUser, get_current_user, require_role

log = structlog.get_logger("api.ml_limits")

router = APIRouter(
    prefix="/ml-limits", tags=["ml-limits"],
    dependencies=[Depends(get_current_user)],
)

GLOBAL_ROW = "__global__"
SNAPSHOT_KEY = "ml_limits:snapshot"
SNAPSHOT_TTL = 24 * 3600  # страховочный TTL; планировщик переиздаёт чаще


NOT_PUBLISHED = "saved but not published to the serving path; it will be retried"


def build_snapshot(rows: Iterable[MlLimit]) -> str:
    """JSON-снапшот для R7 из строк ``ml_limits``: только то, что нужно
    горячему пути, в том числе ``enabled`` каждой корзины (выключенная
    корзина → R7 отклоняет её офферы; раньше в снапшот всегда шло true)."""
    global_enabled, buckets = True, {}
    for row in rows:
        if row.segment_bucket == GLOBAL_ROW:
            global_enabled = bool(row.enabled)
        else:
            buckets[row.segment_bucket] = {
                "min_rate": float(row.min_rate),
                "max_rate": float(row.max_rate),
                "enabled": bool(row.enabled),
            }
    return json.dumps({"global_enabled": global_enabled,
                       "buckets": dict(sorted(buckets.items()))},
                      separators=(",", ":"))


async def publish_snapshot(redis, session: AsyncSession) -> None:
    """Собрать актуальный снапшот из БД и записать в Redis."""
    rows = (await session.execute(select(MlLimit))).scalars().all()
    await redis.setex(SNAPSHOT_KEY, SNAPSHOT_TTL, build_snapshot(rows))


async def _load(session: AsyncSession) -> MlLimitsResponse:
    rows = (await session.execute(select(MlLimit))).scalars().all()
    global_enabled = True
    latest_by, latest_at = None, None
    limits: list[MlLimitItem] = []
    for row in rows:
        if row.updated_at and (latest_at is None or row.updated_at > latest_at):
            latest_at, latest_by = row.updated_at, row.updated_by
        if row.segment_bucket == GLOBAL_ROW:
            global_enabled = bool(row.enabled)
        else:
            limits.append(MlLimitItem.model_validate(row))
    limits.sort(key=lambda x: x.segment_bucket)
    return MlLimitsResponse(
        global_enabled=global_enabled, limits=limits,
        updated_by=latest_by, updated_at=latest_at,
    )


@router.get("", response_model=MlLimitsResponse,
            dependencies=[Depends(require_permission("analytics"))])
async def get_ml_limits(
    session: AsyncSession = Depends(get_session_dep),
) -> MlLimitsResponse:
    return await _load(session)


@router.put("", response_model=MlLimitsResponse,
            dependencies=[Depends(require_role("ADMIN"))])
async def put_ml_limits(
    request: Request,
    payload: MlLimitsUpdate,
    current: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> MlLimitsResponse:
    now = datetime.now(UTC)

    if payload.limits:
        for item in payload.limits:
            row = await session.get(MlLimit, item.segment_bucket)
            if row is None:
                row = MlLimit(segment_bucket=item.segment_bucket)
                session.add(row)
            row.min_rate = item.min_rate
            row.max_rate = item.max_rate
            row.daily_budget = item.daily_budget
            row.auto_approve = item.auto_approve
            row.risk_level = item.risk_level
            row.updated_by = current.email
            row.updated_at = now

    if payload.global_enabled is not None:
        g = await session.get(MlLimit, GLOBAL_ROW)
        if g is None:
            g = MlLimit(segment_bucket=GLOBAL_ROW, min_rate=0, max_rate=100,
                        daily_budget=0)
            session.add(g)
        g.enabled = payload.global_enabled
        g.updated_by = current.email
        g.updated_at = now

    await session.commit()
    log.info("ml_limits_updated", by=current.email,
             global_enabled=payload.global_enabled,
             buckets=[x.segment_bucket for x in payload.limits or []])

    # Публикация свежего снапшота: R7 подхватит изменения сразу. Сбой — не
    # «успех»: клиент узнаёт, что лимиты ещё не действуют (B39); изменение
    # остаётся в БД, задача republish-ml-limits доставит его позже.
    try:
        await publish_snapshot(request.app.state.redis, session)
    except Exception as exc:  # noqa: BLE001 — Redis недоступен или ответил ошибкой
        log.warning("ml_limits_snapshot_publish_failed", error=str(exc))
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=NOT_PUBLISHED) from exc
    return await _load(session)
