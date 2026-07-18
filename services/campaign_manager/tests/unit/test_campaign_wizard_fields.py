"""Unit tests for phase-22 campaign wizard fields.

Валидация схем (rfm_min≤rfm_max, daily_limit≤budget_total) и логика
планировщика: явный daily_limit как порог + гейт auto_pause.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.scheduling import pause_overspent_campaigns
from pydantic import ValidationError

from app.schemas import CampaignCreate, CampaignUpdate  # isort: skip

_BASE = dict(
    name="Test",
    target_segment_ids=[9, 10],
    cashback_rate="5.0",
    budget_total="1000000",
    start_date="2026-06-01T00:00:00Z",
    end_date="2026-06-30T23:59:59Z",
    allowed_channels=["ONLINE"],
    mcc_codes=["5411"],
)


# ── schema validation ─────────────────────────────────────────────────────────
def test_create_accepts_wizard_fields():
    c = CampaignCreate(**_BASE, daily_limit="50000", auto_pause=False,
                       rfm_min=2, rfm_max=4,
                       min_tx_amounts={"5411": "700"})
    assert c.daily_limit == 50000
    assert c.auto_pause is False
    assert c.min_tx_amounts["5411"] == 700


def test_create_rejects_rfm_inverted():
    with pytest.raises(ValidationError, match="rfm_min must be"):
        CampaignCreate(**_BASE, rfm_min=5, rfm_max=2)


def test_create_rejects_daily_limit_over_budget():
    with pytest.raises(ValidationError, match="daily_limit must not exceed"):
        CampaignCreate(**_BASE, daily_limit="2000000")  # > budget 1M


def test_create_rejects_rfm_out_of_range():
    with pytest.raises(ValidationError):
        CampaignCreate(**_BASE, rfm_min=0)   # квантили 1..5
    with pytest.raises(ValidationError):
        CampaignCreate(**_BASE, rfm_max=6)


def test_update_rfm_validation():
    with pytest.raises(ValidationError, match="rfm_min must be"):
        CampaignUpdate(rfm_min=5, rfm_max=1)
    ok = CampaignUpdate(daily_limit="10", auto_pause=True, rfm_min=1, rfm_max=3)
    assert ok.auto_pause is True


# ── scheduler: daily_limit threshold + auto_pause gate ────────────────────────
class _FakeChResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeCh:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _FakeChResult(self._rows)


class _SelectResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _SchedConn:
    def __init__(self, rows_by_cid):
        self.rows_by_cid = rows_by_cid
        self.paused: list[str] = []

    async def execute(self, sql, params=None):
        if "UPDATE" in str(sql):
            self.paused.append(params["cid"])
            return None
        return _SelectResult(self.rows_by_cid.get(params["cid"]))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _SchedEngine:
    def __init__(self, conn):
        self._conn = conn

    def begin(self):
        return self._conn


def _camp_row(*, daily_limit, auto_pause, budget_total=90000.0, days=90):
    return SimpleNamespace(
        budget_total=budget_total,
        end_date=datetime.now(UTC) + timedelta(days=days),
        status="ACTIVE",
        daily_limit=daily_limit,
        auto_pause=auto_pause,
    )


@pytest.mark.asyncio
async def test_pauses_when_daily_limit_exceeded():
    conn = _SchedConn({"c1": _camp_row(daily_limit=1000.0, auto_pause=True)})
    engine = _SchedEngine(conn)
    ch = _FakeCh([("c1", 1500.0)])  # потрачено сегодня > лимита
    paused = await pause_overspent_campaigns(engine, ch)
    assert paused == 1
    assert conn.paused == ["c1"]


@pytest.mark.asyncio
async def test_auto_pause_false_is_never_paused():
    conn = _SchedConn({"c1": _camp_row(daily_limit=1000.0, auto_pause=False)})
    engine = _SchedEngine(conn)
    ch = _FakeCh([("c1", 999999.0)])  # сильно за лимитом, но гейт выключен
    paused = await pause_overspent_campaigns(engine, ch)
    assert paused == 0
    assert conn.paused == []


@pytest.mark.asyncio
async def test_daily_limit_takes_priority_over_budget_derived_cap():
    # daily_limit=5000 явно > выведенного cap (90000/90=1000): при spent=2000
    # выведенный cap запаузил бы, но явный лимит — нет.
    conn = _SchedConn({"c1": _camp_row(daily_limit=5000.0, auto_pause=True)})
    engine = _SchedEngine(conn)
    ch = _FakeCh([("c1", 2000.0)])
    paused = await pause_overspent_campaigns(engine, ch)
    assert paused == 0


@pytest.mark.asyncio
async def test_falls_back_to_budget_cap_when_no_daily_limit():
    conn = _SchedConn({"c1": _camp_row(daily_limit=None, auto_pause=True)})
    engine = _SchedEngine(conn)
    ch = _FakeCh([("c1", 2000.0)])  # cap ≈ 90000/90 = 1000 → превышен
    paused = await pause_overspent_campaigns(engine, ch)
    assert paused == 1
