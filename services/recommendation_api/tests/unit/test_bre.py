"""Unit tests for the Business Rules Engine — chapter 3.2, listing 3.14.

The parametrized ``BRE_CASES`` table exercises every combination listed
in the dissertation; ``test_budget_rule_no_redis_call_on_early_reject``
verifies the priority-order short-circuit (cheap REJECT in R1 must keep
us out of the Redis / Postgres dependent rules).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bre.engine import BusinessRulesEngine
from app.bre.models import RuleContext, RuleOutcome


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _campaign(**overrides) -> dict:
    base = {
        "campaign_id": "00000000-0000-0000-0000-000000000001",
        "min_transaction_amount": 100.0,
        "allowed_channels": ["ONLINE", "POS", "MOBILE"],
        "budget_total": 100_000.0,
        "budget_spent": 1000.0,
        "status": "ACTIVE",
        "cashback_rate": 3.0,
    }
    base.update(overrides)
    return base


class FakeRedis:
    """Trivial in-memory async Redis stub."""
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, key: str):
        self.calls.append(("get", key))
        return self.data.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.calls.append(("setex", key))
        self.data[key] = value
        return True


class FakeDBEngine:
    """Recording stub mimicking SQLAlchemy AsyncEngine.begin/connect."""
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
            class _Result:
                def __init__(self, row): self._row = row
                def first(self): return self._row
            return _Result(self.parent.row)


@pytest.fixture()
def settings():
    return SimpleNamespace(
        anti_fatigue_ttl_days=14,
        frequency_gate_per_24h=5,
        min_award_threshold=1.0,
    )


def _ctx(*, redis=None, db=None, settings=None, **overrides) -> RuleContext:
    base = dict(
        user_id="u-1",
        candidate_mcc="5411",
        campaign_id="00000000-0000-0000-0000-000000000001",
        user_segment_id=5,
        transaction_amount=500.0,
        channel="POS",
        excluded_mccs=frozenset(),
        campaign=_campaign(),
        redis=redis,
        db=db,
        settings=settings,
    )
    base.update(overrides)
    return RuleContext(**base)


# ---------------------------------------------------------------------------
# Listing 3.14 — parametrised BRE_CASES (every rule, both directions)
# ---------------------------------------------------------------------------
BRE_CASES = [
    # ---- R1 — category exclusion -----------------------------------
    ("R1_excluded",
     {"excluded_mccs": frozenset({"5411"})},
     RuleOutcome.REJECT, "R1_category_exclusion"),
    ("R1_not_excluded",
     {"excluded_mccs": frozenset({"7995"})},
     RuleOutcome.PASS, "R6_budget_reservation"),

    # ---- R2 — minimum transaction amount ---------------------------
    ("R2_below_min",
     {"transaction_amount": 50.0},
     RuleOutcome.REJECT, "R2_min_transaction"),
    ("R2_above_min",
     {"transaction_amount": 5000.0},
     RuleOutcome.PASS, "R6_budget_reservation"),

    # ---- R5 — channel applicability --------------------------------
    ("R5_disallowed_channel",
     {"channel": "ATM"},
     RuleOutcome.REJECT, "R5_channel_applicability"),
    ("R5_no_restriction",
     {"channel": "POS",
      "campaign": _campaign(allowed_channels=[])},
     RuleOutcome.PASS, "R6_budget_reservation"),

    # ---- R6 — budget reservation -----------------------------------
    ("R6_budget_exhausted",
     {"campaign": _campaign(budget_spent=99_999.5)},
     RuleOutcome.REJECT, "R6_budget_reservation"),
    ("R6_status_paused",
     {"campaign": _campaign(status="PAUSED")},
     RuleOutcome.REJECT, "R6_budget_reservation"),

    # ---- happy path ------------------------------------------------
    ("ALL_PASS",
     {},
     RuleOutcome.PASS, "R6_budget_reservation"),
]


@pytest.mark.parametrize(
    "case_id, overrides, expected_outcome, expected_rule",
    [(c[0], c[1], c[2], c[3]) for c in BRE_CASES],
    ids=[c[0] for c in BRE_CASES],
)
async def test_bre_cases(case_id, overrides, expected_outcome, expected_rule, settings):
    redis = FakeRedis()
    # Default DB row mimics a healthy ACTIVE campaign with budget left;
    # tests that override `campaign` cover the negative-branch paths.
    row = SimpleNamespace(
        budget_total=100_000.0, budget_spent=overrides.get(
            "campaign", _campaign())["budget_spent"],
        status=overrides.get("campaign", _campaign())["status"],
        cashback_rate=3.0,
    )
    db = FakeDBEngine(row=row)

    ctx = _ctx(redis=redis, db=db, settings=settings, **overrides)
    engine = BusinessRulesEngine()
    verdict = await engine.evaluate(ctx)

    assert verdict.outcome == expected_outcome, (
        f"{case_id}: expected {expected_outcome}, got {verdict.outcome} "
        f"(rule={verdict.rule}, reason={verdict.reason})"
    )
    assert verdict.rule == expected_rule


# ---------------------------------------------------------------------------
# R3 + R4 — Redis-bound rules
# ---------------------------------------------------------------------------
async def test_anti_fatigue_rejects_when_key_present(settings):
    redis = FakeRedis()
    redis.data["recent_offer:u-1:00000000-0000-0000-0000-000000000001"] = "1"
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))

    ctx = _ctx(redis=redis, db=db, settings=settings)
    verdict = await BusinessRulesEngine().evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert verdict.rule == "R3_anti_fatigue"


async def test_anti_fatigue_marks_key_on_pass(settings):
    redis = FakeRedis()
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))
    ctx = _ctx(redis=redis, db=db, settings=settings)
    await BusinessRulesEngine().evaluate(ctx)
    assert "recent_offer:u-1:00000000-0000-0000-0000-000000000001" in redis.data


async def test_frequency_gate_rejects_at_limit(settings):
    redis = FakeRedis()
    redis.data["rec_count:u-1"] = str(settings.frequency_gate_per_24h)
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))
    ctx = _ctx(redis=redis, db=db, settings=settings)
    verdict = await BusinessRulesEngine().evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert verdict.rule == "R4_frequency_gate"


# ---------------------------------------------------------------------------
# Priority ordering — early REJECT must short-circuit downstream IO
# ---------------------------------------------------------------------------
async def test_budget_rule_no_redis_call_on_early_reject(settings):
    """An R1 (category exclusion) reject must not touch Redis or the DB."""
    redis = FakeRedis()
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))

    ctx = _ctx(
        redis=redis, db=db, settings=settings,
        excluded_mccs=frozenset({"5411"}),  # forces R1 reject
    )
    verdict = await BusinessRulesEngine().evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert verdict.rule == "R1_category_exclusion"

    # Critical regression guard — no IO after the early reject.
    assert redis.calls == [], f"unexpected Redis calls: {redis.calls}"
    assert db.calls == [],    f"unexpected DB calls: {db.calls}"


async def test_min_transaction_short_circuits_redis_and_db(settings):
    redis = FakeRedis()
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))
    ctx = _ctx(redis=redis, db=db, settings=settings, transaction_amount=10.0)
    verdict = await BusinessRulesEngine().evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert verdict.rule == "R2_min_transaction"
    assert redis.calls == []
    assert db.calls == []


async def test_evaluate_verbose_runs_all_rules(settings):
    """Verbose mode must NOT short-circuit."""
    redis = FakeRedis()
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))
    ctx = _ctx(
        redis=redis, db=db, settings=settings,
        excluded_mccs=frozenset({"5411"}),
    )
    results = await BusinessRulesEngine().evaluate_verbose(ctx)
    assert len(results) == 6
    rule_names = [r.rule for r in results]
    assert rule_names == [
        "R1_category_exclusion",
        "R2_min_transaction",
        "R3_anti_fatigue",
        "R4_frequency_gate",
        "R5_channel_applicability",
        "R6_budget_reservation",
    ]


# ---------------------------------------------------------------------------
# DB error paths — ensure SKIP semantics are honoured
# ---------------------------------------------------------------------------
async def test_budget_rule_db_error_returns_skip(settings):
    redis = FakeRedis()
    db = FakeDBEngine(raise_exc=RuntimeError("boom"))
    ctx = _ctx(redis=redis, db=db, settings=settings)
    verdict = await BusinessRulesEngine().evaluate(ctx)
    # SKIP propagates as the final verdict (not REJECT) so the candidate
    # remains; verbose mode would show R6 as SKIP.
    assert verdict.outcome is RuleOutcome.SKIP
    assert verdict.rule == "R6_budget_reservation"


# ---------------------------------------------------------------------------
# Custom rule chains
# ---------------------------------------------------------------------------
async def test_custom_rule_chain_only_runs_supplied_rules(settings):
    async def always_pass(ctx):
        from app.bre.models import RuleResult
        return RuleResult.passed_("custom_pass")

    async def always_reject(ctx):
        from app.bre.models import RuleResult
        return RuleResult.reject("custom_reject", reason="nope")

    engine = BusinessRulesEngine([
        ("custom_pass", always_pass),
        ("custom_reject", always_reject),
    ])
    verdict = await engine.evaluate(_ctx(settings=settings))
    assert verdict.rule == "custom_reject"
    assert verdict.outcome is RuleOutcome.REJECT
