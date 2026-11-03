"""Unit tests for analytics (фаза 24 + этап D: B11, B33).

Полные агрегаты — на настоящем Postgres (tests/integration/
test_analytics_ab.py). Здесь — чистая логика: маппинг корзина→децили,
тренды KPI (минимальная база, ноль, потолок), отсев воронки в [0, 100],
pending-контракт воронки и KPI без работающих кампаний.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from app.api.analytics import TREND_CAP, _deciles_for, _safe_pct, _trend, funnel, kpis


# ── helpers ───────────────────────────────────────────────────────────────────
def test_deciles_for_maps_buckets():
    assert _deciles_for("premium") == [9, 10]
    assert _deciles_for("mass") == [5, 6, 7, 8]
    assert _deciles_for("business") == [1]


def test_deciles_for_none_without_filter():
    assert _deciles_for(None) is None
    assert _deciles_for("") is None


def test_trend_is_relative_change_with_enough_base():
    assert _trend(110, 100, base=20, min_base=20) == 10.0
    assert _trend(80, 100, base=500, min_base=20) == -20.0
    assert _trend(0.25, 0.2, base=40, min_base=20) == 25.0


@pytest.mark.parametrize(("prev", "base"), [
    (100, 19),      # база меньше KPI_TREND_MIN_BASE → «—», а не ▲1286% (B11)
    (5, 0),         # предыдущее окно пустое
    (0, 50),        # метрика в предыдущем окне нулевая — деления нет
])
def test_trend_is_none_without_a_meaningful_base(prev, base):
    assert _trend(5000, prev, base=base, min_base=20) is None


def test_trend_with_zero_min_base_still_needs_events():
    assert _trend(5, 1, base=1, min_base=0) == 400.0
    assert _trend(5, 1, base=0, min_base=0) is None


def test_trend_is_capped():
    assert TREND_CAP == 999.9
    assert _trend(1000, 1, base=20, min_base=20) == 999.9      # было 99900.0
    assert _trend(-1000, 1, base=20, min_base=20) == -999.9


def test_drop_off_is_clamped_to_0_100():
    assert _safe_pct(50, 200) == 75.0
    assert _safe_pct(300, 200) == 0.0           # раньше −50% (B33)
    assert _safe_pct(-1, 200) == 100.0
    assert _safe_pct(5, 0) == 0.0


# ── fakes ─────────────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, v):
        self._v = v

    def scalar_one(self):
        return self._v

    def one(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return self._v


class _QueueSession:
    """Отдаёт заранее заготовленные результаты по порядку execute()."""
    def __init__(self, results):
        self._q = list(results)
        self.executed = 0

    async def execute(self, *_a, **_k):
        self.executed += 1
        return _Result(self._q.pop(0))

    async def get(self, *_a, **_k):
        return None


# ── funnel pending contract ───────────────────────────────────────────────────
# execute во funnel: target (scalar), затем одна строка стадий
# (received, opened, accepted, transacted, cashback_paid).
@pytest.mark.asyncio
async def test_funnel_marks_post_delivery_pending_when_no_response():
    session = _QueueSession([2000, (1000, 0, 0, 0, 0)])
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
    session = _QueueSession([2000, (1000, 500, 200, 100, 90)])
    resp = await funnel(campaign_id=None, period=30, segment_id=None,
                        session=session)
    assert all(s.pending is False for s in resp.steps)
    assert [(s.name, s.count, s.drop_off_pct) for s in resp.steps] == [
        ("target_audience", 2000, 0.0), ("received", 1000, 50.0),
        ("opened", 500, 50.0), ("accepted", 200, 60.0),
        ("transacted", 100, 50.0), ("cashback_paid", 90, 10.0)]


@pytest.mark.asyncio
async def test_funnel_not_pending_when_nothing_delivered():
    # received=0 → это «нет доставки», а не «ждём отклика»; pending не ставим.
    session = _QueueSession([2000, (0, 0, 0, 0, 0)])
    resp = await funnel(campaign_id=None, period=30, segment_id=None,
                        session=session)
    assert all(s.pending is False for s in resp.steps)


@pytest.mark.asyncio
async def test_funnel_drop_off_never_negative():
    # получивших больше аудитории (клиент сменил сегмент после доставки) —
    # отсев 0, а не −25%
    session = _QueueSession([800, (1000, 500, 200, 100, 90)])
    resp = await funnel(campaign_id=None, period=30, segment_id=None,
                        session=session)
    assert resp.steps[1].drop_off_pct == 0.0
    assert all(0.0 <= s.drop_off_pct <= 100.0 for s in resp.steps)


# ── kpis ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_kpis_without_running_campaigns_are_zero_not_all_history():
    """Нет ACTIVE/PAUSED кампаний и фильтра → нули и has_data=false; по
    рекомендациям и начислениям не ходим (раньше — по всем историческим)."""
    session = _QueueSession([[]])
    resp = await kpis(campaign_id=None, period=7, segment_id=None, session=session)
    assert session.executed == 1
    assert resp.campaigns_count == 0 and resp.reach == 0
    assert resp.spent == 0 and resp.budget == 0 and resp.avg_ctr == 0.0
    assert resp.has_data is False
    assert resp.trend_window_days == 7
    assert (resp.trends.reach, resp.trends.spent, resp.trends.ctr) == (None, None, None)


def _campaign(spent="0", total="1000"):
    return SimpleNamespace(campaign_id="c1", budget_total=Decimal(total),
                           budget_spent=Decimal(spent))


@pytest.mark.asyncio
async def test_kpis_has_data_when_only_delivered():
    """Доставлено, но ещё ничего не потрачено и не принято — данные есть
    (раньше has_data = spent > 0 and ctr > 0)."""
    session = _QueueSession([
        [_campaign()],
        (40, 30, 0),            # текущее окно: доставлено, охват, принято
        (10, 10, 0),            # предыдущее окно
        (0, 0),                 # расход и число начислений: текущее
        (0, 0),                 # предыдущее
    ])
    resp = await kpis(campaign_id=None, period=30, segment_id=None, session=session)
    assert resp.has_data is True
    assert resp.reach == 30 and resp.avg_ctr == 0.0
    # 10 доставок в предыдущем окне < KPI_TREND_MIN_BASE (20) → «—»
    assert (resp.trends.reach, resp.trends.spent, resp.trends.ctr) == (None, None, None)


@pytest.mark.asyncio
async def test_kpis_trends_and_reach_are_the_same_metric():
    """trends.reach — изменение того же охвата (различные клиенты), а не
    числа строк рекомендаций."""
    session = _QueueSession([
        [_campaign(spent="120")],
        (200, 50, 40),          # 50 клиентов, CTR 0.2
        (100, 40, 10),          # 40 клиентов, CTR 0.1
        (120.0, 30),
        (60.0, 25),
    ])
    resp = await kpis(campaign_id=None, period=30, segment_id=None, session=session)
    assert resp.reach == 50
    assert resp.trends.reach == 25.0           # 50 vs 40, а не 200 vs 100 (+100%)
    assert resp.trends.ctr == 100.0
    assert resp.trends.spent == 100.0
    assert resp.trend_window_days == 30 and resp.has_data is True
