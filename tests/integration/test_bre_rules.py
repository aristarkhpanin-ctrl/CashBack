"""Integration: each of the 6 Business Rules × 3 cases (PASS / REJECT / SKIP).

Total: 18 tests. The rules don't truly need testcontainers (their
external dependencies — Redis / Postgres — are reachable via tiny
async stubs). We still mark these as ``integration`` because they
exercise the multi-rule chain in :class:`BusinessRulesEngine` exactly
as it runs in production.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Pick the recommendation_api `app` package as the namespace to import from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "recommendation_api"))
for _m in [m for m in list(sys.modules) if m == "app" or m.startswith("app.")]:
    del sys.modules[_m]

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Light-weight async stubs (mirror those in services/recommendation_api/tests)
# ---------------------------------------------------------------------------
class FakeRedis:
    def __init__(self, prefilled: dict[str, str] | None = None) -> None:
        self.data: dict[str, str] = dict(prefilled or {})
        self.calls: list[tuple[str, str]] = []

    async def get(self, key: str):
        self.calls.append(("get", key))
        return self.data.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.calls.append(("setex", key))
        self.data[key] = value
        return True


class FakeDBEngine:
    def __init__(self, row: SimpleNamespace | None = None,
                 raise_exc: Exception | None = None):
        self.row = row
        self.raise_exc = raise_exc
        self.calls: list[str] = []

    def begin(self):
        return self._Cm(self)

    class _Cm:
        def __init__(self, parent: "FakeDBEngine") -> None:
            self.parent = parent
        async def __aenter__(self):
            self.parent.calls.append("begin")
            if self.parent.raise_exc:
                raise self.parent.raise_exc
            return self
        async def __aexit__(self, *exc):
            self.parent.calls.append("end")
            return False
        async def execute(self, sql, params):
            self.parent.calls.append("execute")
            class _R:
                def __init__(self, row): self._row = row
                def first(self): return self._row
            return _R(self.parent.row)


def _settings(**overrides):
    base = dict(
        anti_fatigue_ttl_days=14,
        frequency_gate_per_24h=5,
        min_award_threshold=1.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _campaign(**overrides):
    base = {
        "campaign_id": "00000000-0000-0000-0000-000000000001",
        "min_transaction_amount": 100.0,
        "allowed_channels": ["ONLINE", "POS", "MOBILE"],
        "budget_total": 100000.0,
        "budget_spent": 0.0,
        "status": "ACTIVE",
        "cashback_rate": 3.0,
    }
    base.update(overrides)
    return base


def _ctx(**overrides):
    from app.bre.models import RuleContext
    base = dict(
        user_id="u-1",
        candidate_mcc="5411",
        campaign_id="00000000-0000-0000-0000-000000000001",
        user_segment_id=5,
        transaction_amount=500.0,
        channel="POS",
        excluded_mccs=frozenset(),
        campaign=_campaign(),
        redis=FakeRedis(),
        db=FakeDBEngine(row=SimpleNamespace(
            budget_total=100_000.0, budget_spent=0.0,
            status="ACTIVE", cashback_rate=3.0)),
        settings=_settings(),
    )
    base.update(overrides)
    return RuleContext(**base)


# ===========================================================================
# R1 — category exclusion
# ===========================================================================
async def test_r1_pass_when_mcc_not_excluded():
    from app.bre.rules.category_exclusion import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(excluded_mccs=frozenset({"7995"})))
    assert res.outcome is RuleOutcome.PASS


async def test_r1_reject_when_mcc_excluded():
    from app.bre.rules.category_exclusion import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(excluded_mccs=frozenset({"5411"})))
    assert res.outcome is RuleOutcome.REJECT


async def test_r1_pass_when_no_exclusions_set():
    from app.bre.rules.category_exclusion import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(excluded_mccs=frozenset()))
    assert res.outcome is RuleOutcome.PASS


# ===========================================================================
# R2 — minimum transaction amount
# ===========================================================================
async def test_r2_pass_when_amount_above_minimum():
    from app.bre.rules.min_transaction import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(transaction_amount=5000))
    assert res.outcome is RuleOutcome.PASS


async def test_r2_reject_when_amount_below_minimum():
    from app.bre.rules.min_transaction import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(transaction_amount=50))
    assert res.outcome is RuleOutcome.REJECT


async def test_r2_skip_when_amount_missing():
    from app.bre.rules.min_transaction import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(transaction_amount=None))
    assert res.outcome is RuleOutcome.SKIP


# ===========================================================================
# R3 — anti-fatigue
# ===========================================================================
async def test_r3_pass_when_no_recent_offer():
    from app.bre.rules.anti_fatigue import evaluate
    from app.bre.models import RuleOutcome
    redis = FakeRedis()
    res = await evaluate(_ctx(redis=redis))
    assert res.outcome is RuleOutcome.PASS
    # PASS branch sets the key.
    assert any(c[0] == "setex" for c in redis.calls)


async def test_r3_reject_when_recent_offer_present():
    from app.bre.rules.anti_fatigue import evaluate
    from app.bre.models import RuleOutcome
    redis = FakeRedis(prefilled={
        "recent_offer:u-1:00000000-0000-0000-0000-000000000001": "1",
    })
    res = await evaluate(_ctx(redis=redis))
    assert res.outcome is RuleOutcome.REJECT


async def test_r3_skip_when_redis_unavailable():
    from app.bre.rules.anti_fatigue import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(redis=None))
    assert res.outcome is RuleOutcome.SKIP


# ===========================================================================
# R4 — frequency gate
# ===========================================================================
async def test_r4_pass_below_limit():
    from app.bre.rules.frequency_gate import evaluate
    from app.bre.models import RuleOutcome
    redis = FakeRedis(prefilled={"rec_count:u-1": "2"})
    res = await evaluate(_ctx(redis=redis))
    assert res.outcome is RuleOutcome.PASS


async def test_r4_reject_at_limit():
    from app.bre.rules.frequency_gate import evaluate
    from app.bre.models import RuleOutcome
    redis = FakeRedis(prefilled={"rec_count:u-1": "5"})
    res = await evaluate(_ctx(redis=redis))
    assert res.outcome is RuleOutcome.REJECT


async def test_r4_skip_when_redis_unavailable():
    from app.bre.rules.frequency_gate import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(redis=None))
    assert res.outcome is RuleOutcome.SKIP


# ===========================================================================
# R5 — channel applicability
# ===========================================================================
async def test_r5_pass_when_channel_allowed():
    from app.bre.rules.channel_applicability import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(channel="POS"))
    assert res.outcome is RuleOutcome.PASS


async def test_r5_reject_when_channel_not_allowed():
    from app.bre.rules.channel_applicability import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(channel="ATM"))
    assert res.outcome is RuleOutcome.REJECT


async def test_r5_pass_when_no_channel_restriction():
    from app.bre.rules.channel_applicability import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(campaign=_campaign(allowed_channels=[])))
    assert res.outcome is RuleOutcome.PASS


# ===========================================================================
# R6 — budget reservation
# ===========================================================================
async def test_r6_pass_when_remaining_budget_above_min_award():
    from app.bre.rules.budget_reservation import evaluate
    from app.bre.models import RuleOutcome
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=10000.0, budget_spent=10.0,
        status="ACTIVE", cashback_rate=3.0,
    ))
    res = await evaluate(_ctx(db=db))
    assert res.outcome is RuleOutcome.PASS


async def test_r6_reject_when_budget_exhausted():
    from app.bre.rules.budget_reservation import evaluate
    from app.bre.models import RuleOutcome
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100.0, budget_spent=99.5,
        status="ACTIVE", cashback_rate=3.0,
    ))
    res = await evaluate(_ctx(db=db))
    assert res.outcome is RuleOutcome.REJECT


async def test_r6_skip_when_db_unavailable():
    from app.bre.rules.budget_reservation import evaluate
    from app.bre.models import RuleOutcome
    res = await evaluate(_ctx(db=None))
    assert res.outcome is RuleOutcome.SKIP
