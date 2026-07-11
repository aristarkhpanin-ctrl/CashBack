"""Unit tests for the budget-utilization Prometheus export (phase 17).

The gauge feeds the ``CampaignBudgetNearlyExhausted`` alert rule, so the
contract matters: ratio = spent/total per ACTIVE campaign, stale labels
cleared on every refresh.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.scheduling import BUDGET_UTILIZATION, export_budget_metrics


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_args, **_kw):
        return _FakeResult(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _FakeConn(self._rows)


def _row(cid, name, total, spent):
    return SimpleNamespace(
        campaign_id=cid, name=name, budget_total=total, budget_spent=spent,
    )


def _gauge_value(cid, name):
    return BUDGET_UTILIZATION.labels(campaign_id=cid, name=name)._value.get()


@pytest.mark.asyncio
async def test_export_sets_ratio_per_campaign():
    engine = _FakeEngine([
        _row("c1", "Groceries", 1000, 950),
        _row("c2", "Pharmacy", 500, 0),
    ])
    count = await export_budget_metrics(engine)

    assert count == 2
    assert _gauge_value("c1", "Groceries") == pytest.approx(0.95)
    assert _gauge_value("c2", "Pharmacy") == 0.0


@pytest.mark.asyncio
async def test_zero_total_budget_does_not_divide_by_zero():
    engine = _FakeEngine([_row("c3", "Broken", 0, 100)])
    await export_budget_metrics(engine)
    assert _gauge_value("c3", "Broken") == 0.0


@pytest.mark.asyncio
async def test_stale_labels_cleared_on_refresh():
    await export_budget_metrics(_FakeEngine([_row("old", "Finished", 100, 100)]))
    await export_budget_metrics(_FakeEngine([_row("new", "Running", 100, 10)]))

    # Only the fresh campaign remains in the exposition.
    samples = [
        s for metric in BUDGET_UTILIZATION.collect() for s in metric.samples
    ]
    label_ids = {s.labels["campaign_id"] for s in samples}
    assert label_ids == {"new"}
