"""Unit tests for reference dictionaries (фаза 21)."""
from __future__ import annotations

import pytest
from app.api import reference
from app.api.reference import _load_segments, get_mcc_categories, get_segments
from app.reference_data import MCC_CATEGORIES
from app.segments import BUCKET_ORDER, SEGMENT_BUCKETS


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    """Минимальная замена AsyncSession: execute → строки (segment_id, count)."""
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


# ── decile → bucket rollup ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_load_segments_rolls_deciles_into_buckets():
    # premium=[9,10], mass=[5,6,7,8], young=[3,4], senior=[2], business=[1]
    rows = [(10, 100), (9, 24), (8, 10), (7, 10), (6, 10), (5, 10),
            (4, 20), (3, 20), (2, 210), (1, 87)]
    segs = await _load_segments(_FakeSession(rows))
    by_id = {s.id: s for s in segs}
    assert by_id["premium"].count == 124   # 100 + 24
    assert by_id["mass"].count == 40       # 10*4
    assert by_id["young"].count == 40      # 20 + 20
    assert by_id["senior"].count == 210
    assert by_id["business"].count == 87


@pytest.mark.asyncio
async def test_load_segments_always_returns_all_buckets_in_order():
    segs = await _load_segments(_FakeSession([]))  # пустая таблица users
    assert [s.id for s in segs] == list(BUCKET_ORDER)
    assert all(s.count == 0 for s in segs)
    # раскладка децилей идёт из одного источника
    for s in segs:
        assert s.deciles == SEGMENT_BUCKETS[s.id]


@pytest.mark.asyncio
async def test_load_segments_ignores_unknown_deciles():
    segs = await _load_segments(_FakeSession([(0, 5), (11, 7), (10, 3)]))
    by_id = {s.id: s for s in segs}
    assert by_id["premium"].count == 3     # только валидный дециль 10
    assert sum(s.count for s in segs) == 3


# ── 60s process cache ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_segments_endpoint_caches_within_ttl():
    reference._reset_cache()
    first = await get_segments(_FakeSession([(10, 100)]))
    # вторая сессия отдаёт другие цифры, но кэш ещё жив → те же данные
    second = await get_segments(_FakeSession([(10, 999)]))
    assert first[0].count == second[0].count == 100
    reference._reset_cache()
    third = await get_segments(_FakeSession([(10, 999)]))
    assert third[0].count == 999


# ── MCC catalog integrity ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mcc_categories_catalog():
    cats = await get_mcc_categories()
    assert len(cats) == 12
    codes = [c.code for c in cats]
    assert len(set(codes)) == 12                     # уникальные коды
    assert all(len(c.code) == 4 for c in cats)       # MCC — 4 цифры
    # иконки заданы именами (Lucide), не emoji
    for c in cats:
        assert c.icon.isascii() and "-" in c.icon or c.icon.isalpha()
        assert c.name


def test_mcc_source_is_single_list():
    assert len({c["code"] for c in MCC_CATEGORIES}) == len(MCC_CATEGORIES)
