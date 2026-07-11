"""Сегментные корзины: децили 1–10 → 5 витринных групп.

ЗЕРКАЛО ``frontend/src/shared/api/adapters.ts`` (SEGMENT_BUCKETS) —
при изменении группировки менять оба файла синхронно. Единственная
точка рассинхрона фронт/бэк, зафиксирована в docs/ROADMAP.md (риски).
"""
from __future__ import annotations

SEGMENT_BUCKETS: dict[str, list[int]] = {
    "premium":  [9, 10],
    "mass":     [5, 6, 7, 8],
    "young":    [3, 4],
    "senior":   [2],
    "business": [1],
}

DECILE_TO_BUCKET: dict[int, str] = {
    decile: bucket
    for bucket, deciles in SEGMENT_BUCKETS.items()
    for decile in deciles
}

BUCKET_ORDER = ("premium", "mass", "young", "senior", "business")


def bucket_of(segment_id: int | None) -> str | None:
    return DECILE_TO_BUCKET.get(segment_id) if segment_id is not None else None
