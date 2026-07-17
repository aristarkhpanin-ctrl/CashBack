"""Справочники сегментов и MCC-категорий (фаза 21).

``GET /reference/segments``        — 5 витринных корзин с ЖИВЫМ размером
аудитории (агрегат ``users`` по децилям, свёрнутый в корзины) и децильной
раскладкой. ``GET /reference/mcc-categories`` — статический справочник из
``reference_data.py``.

Единый источник вместо тройного дубля (mockData.ts / segments.py /
ml_rate_cap.py). Оба эндпоинта read-only, доступны любой аутентифицированной
роли. Размер аудитории кэшируется в процессе на 60 с — агрегат по всей
таблице ``users`` не нужно считать на каждый заход в визард.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.models import User
from app.reference_data import MCC_CATEGORIES, SEGMENT_NAMES
from app.schemas import MccCategoryRef, SegmentRef
from app.security import get_current_user
from app.segments import BUCKET_ORDER, SEGMENT_BUCKETS, bucket_of

router = APIRouter(
    prefix="/reference", tags=["reference"],
    dependencies=[Depends(get_current_user)],
)

_CACHE_TTL = 60.0  # секунд
_segments_cache: tuple[float, list[SegmentRef]] | None = None


def _reset_cache() -> None:
    """Сброс кэша (для тестов)."""
    global _segments_cache
    _segments_cache = None


async def _load_segments(session: AsyncSession) -> list[SegmentRef]:
    """Свернуть децильные размеры аудитории в 5 витринных корзин."""
    rows = (await session.execute(
        select(User.segment_id, func.count())
        .where(User.segment_id.is_not(None))
        .group_by(User.segment_id)
    )).all()

    counts: dict[str, int] = {b: 0 for b in BUCKET_ORDER}
    for segment_id, n in rows:
        bucket = bucket_of(int(segment_id))
        if bucket is not None:
            counts[bucket] += int(n)

    return [
        SegmentRef(
            id=bucket,
            name=SEGMENT_NAMES[bucket],
            count=counts[bucket],
            deciles=SEGMENT_BUCKETS[bucket],
        )
        for bucket in BUCKET_ORDER
    ]


@router.get("/segments", response_model=list[SegmentRef])
async def get_segments(
    session: AsyncSession = Depends(get_session_dep),
) -> list[SegmentRef]:
    global _segments_cache
    now = time.monotonic()
    if _segments_cache is not None and now - _segments_cache[0] < _CACHE_TTL:
        return _segments_cache[1]
    segments = await _load_segments(session)
    _segments_cache = (now, segments)
    return segments


@router.get("/mcc-categories", response_model=list[MccCategoryRef])
async def get_mcc_categories() -> list[MccCategoryRef]:
    return [MccCategoryRef(**c) for c in MCC_CATEGORIES]
