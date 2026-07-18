"""Unit tests for phase-24 analytics (kpis / segment_id / pending).

Полные агрегаты требуют БД (integration). Здесь — чистая логика: маппинг
корзина→децили, дельта-тренд и pending-контракт воронки (доставлено, но нет
откликов → пост-доставочные стадии «ждут данных»).
"""
from __future__ import annotations

import pytest
from app.api.analytics import _deciles_for, _delta_pct, funnel


# ── helpers ───────────────────────────────────────────────────────────────────
def test_deciles_for_maps_buckets():
    assert _deciles_for("premium") == [9, 10]
    assert _deciles_for("mass") == [5, 6, 7, 8]
    assert _deciles_for("business") == [1]


def test_deciles_for_none_without_filter():
    assert _deciles_for(None) is None
    assert _deciles_for("") is None


def test_delta_pct():
    assert _delta_pct(110, 100) == 10.0
    assert _delta_pct(80, 100) == -20.0
    assert _delta_pct(5, 0) == 0.0      # нет базы → 0


# ── funnel pending contract ───────────────────────────────────────────────────
class _ScalarResult:
    def __init__(self, v):
        self._v = v

    def scalar_one(self):
        return self._v


class _QueueSession:
    """Отдаёт заранее заготовленные scalar-значения по порядку execute()."""
    def __init__(self, scalars):
        self._q = list(scalars)

    async def execute(self, *_a, **_k):
        return _ScalarResult(self._q.pop(0))

    async def get(self, *_a, **_k):
        return None


# порядок execute во funnel: target, received, opened, accepted,
# transacted, cashback_paid
@pytest.mark.asyncio
async def test_funnel_marks_post_delivery_pending_when_no_response():
    session = _QueueSession([2000, 1000, 0, 0, 0, 0])
    resp = await funnel(campaign_id=None, period=30, segment_id=None,
                        session=session)
    pending = {s.name: s.pending for s in resp.steps}
    assert pending["target_audience"] is False
    assert pending["received"] is False
    # доставлено (received=1000), откликов нет (opened=0) → стадии 3–6 pending
    assert pending["opened"] is True
    assert pending["accepted"] is True
    assert pending["transacted"] is True
    assert pending["cashback_paid"] is True


@pytest.mark.asyncio
async def test_funnel_not_pending_when_responses_exist():
    session = _QueueSession([2000, 1000, 500, 200, 100, 90])
    resp = await funnel(campaign_id=None, period=30, segment_id=None,
                        session=session)
    assert all(s.pending is False for s in resp.steps)


@pytest.mark.asyncio
async def test_funnel_not_pending_when_nothing_delivered():
    # received=0 → это «нет доставки», а не «ждём отклика»; pending не ставим.
    session = _QueueSession([2000, 0, 0, 0, 0, 0])
    resp = await funnel(campaign_id=None, period=30, segment_id=None,
                        session=session)
    assert all(s.pending is False for s in resp.steps)
