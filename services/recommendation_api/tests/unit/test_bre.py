"""Unit tests for the Business Rules Engine — chapter 3.2, listing 3.14.

The parametrized ``BRE_CASES`` table exercises every combination listed
in the dissertation; ``test_budget_rule_no_redis_call_on_early_reject``
verifies the priority-order short-circuit (cheap REJECT in R1 must keep
us out of the Redis / Postgres dependent rules).

Stage C: R6 judges the campaign row read from ``eligible_campaigns``
(budget left, daily limit) and never queries Postgres itself.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.bre.engine import BusinessRulesEngine
from app.bre.models import RuleContext, RuleOutcome
from app.bre.rules import budget_reservation, ml_rate_cap


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
        "daily_limit": None,
        "spent_today": 0.0,
    }
    base.update(overrides)
    return base


class FakeRedis:
    """Trivial in-memory async Redis stub."""
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    async def get(self, key: str):
        self.calls.append(("get", key))
        return self.data.get(key)

    async def setex(self, key: str, ttl: int, value: str):
        self.calls.append(("setex", key))
        self.data[key] = value
        self.ttls[key] = ttl
        return True

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "get"]


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
        def __init__(self, parent: FakeDBEngine) -> None:
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


@pytest.fixture(autouse=True)
def _reset_r7_cache():
    """R7 кэширует снапшот лимитов в памяти процесса — изолируем тесты."""
    ml_rate_cap._reset_cache()
    yield
    ml_rate_cap._reset_cache()


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
    ("R6_daily_limit_reached",
     {"campaign": _campaign(daily_limit=300.0, spent_today=300.0)},
     RuleOutcome.REJECT, "R6_budget_reservation"),
    ("R6_daily_limit_left",
     {"campaign": _campaign(daily_limit=300.0, spent_today=299.0)},
     RuleOutcome.PASS, "R6_budget_reservation"),

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
    # Every verdict comes from the campaign row (`campaign`); a rule that
    # tried to query Postgres would hit this failing engine.
    db = FakeDBEngine(raise_exc=RuntimeError("the BRE must not query Postgres"))

    ctx = _ctx(redis=redis, db=db, settings=settings, **overrides)
    engine = BusinessRulesEngine()
    verdict = await engine.evaluate(ctx)

    assert verdict.outcome == expected_outcome, (
        f"{case_id}: expected {expected_outcome}, got {verdict.outcome} "
        f"(rule={verdict.rule}, reason={verdict.reason})"
    )
    assert verdict.rule == expected_rule
    assert db.calls == []


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
# R3/R4 marks — only for a candidate that passes the whole chain (B28)
# ---------------------------------------------------------------------------
RECENT_KEY = "recent_offer:u-1:00000000-0000-0000-0000-000000000001"
COUNT_KEY = "rec_count:u-1"


@pytest.mark.parametrize(
    ("overrides", "rejected_by"),
    [
        ({"channel": "ATM"}, "R5_channel_applicability"),
        ({"campaign": _campaign(budget_spent=99_999.5)}, "R6_budget_reservation"),
        ({"campaign": _campaign(daily_limit=300.0, spent_today=300.0)},
         "R6_budget_reservation"),
    ],
    ids=["R5_channel", "R6_budget", "R6_daily_limit"],
)
async def test_late_reject_leaves_no_marks(settings, overrides, rejected_by):
    """R3/R4 pass, then R5/R6 reject: the offer is not served, so the
    campaign is not hidden for 14 days and the 24 h cap is not spent."""
    redis = FakeRedis()
    redis.data[COUNT_KEY] = "2"
    ctx = _ctx(redis=redis, settings=settings, **overrides)

    verdict = await BusinessRulesEngine().evaluate(ctx)

    assert verdict.outcome is RuleOutcome.REJECT
    assert verdict.rule == rejected_by
    assert RECENT_KEY not in redis.data
    assert redis.data[COUNT_KEY] == "2"
    assert redis.writes() == []


async def test_channel_reject_does_not_hide_campaign_on_other_channel(settings):
    redis = FakeRedis()
    engine = BusinessRulesEngine()

    rejected = await engine.evaluate(_ctx(redis=redis, settings=settings, channel="ATM"))
    assert rejected.rule == "R5_channel_applicability"

    served = await engine.evaluate(_ctx(redis=redis, settings=settings, channel="POS"))
    assert served.outcome is RuleOutcome.PASS
    assert redis.data[COUNT_KEY] == "1"


async def test_full_pass_marks_offer_and_counts_it(settings):
    redis = FakeRedis()
    redis.data[COUNT_KEY] = "2"

    verdict = await BusinessRulesEngine().evaluate(_ctx(redis=redis, settings=settings))

    assert verdict.outcome is RuleOutcome.PASS
    assert redis.data[RECENT_KEY] == "1"
    assert redis.ttls[RECENT_KEY] == 14 * 24 * 3600
    assert redis.data[COUNT_KEY] == "3"
    assert redis.ttls[COUNT_KEY] == 24 * 3600

    again = await BusinessRulesEngine().evaluate(_ctx(redis=redis, settings=settings))
    assert again.rule == "R3_anti_fatigue"
    assert again.outcome is RuleOutcome.REJECT


async def test_skipped_rule_sets_no_mark(settings):
    """R3 SKIPs without a campaign id: no ``recent_offer:…:None`` key; R4
    still PASSes and counts the offer."""
    redis = FakeRedis()
    ctx = _ctx(redis=redis, settings=settings, campaign_id=None)

    verdict = await BusinessRulesEngine().evaluate(ctx)

    assert verdict.outcome is RuleOutcome.PASS
    assert redis.data == {COUNT_KEY: "1"}


async def test_evaluate_verbose_writes_nothing(settings):
    redis = FakeRedis()
    results = await BusinessRulesEngine().evaluate_verbose(
        _ctx(redis=redis, settings=settings))
    assert all(r.outcome is not RuleOutcome.REJECT for r in results)
    assert redis.writes() == []
    assert redis.data == {}


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
    assert len(results) == 7
    rule_names = [r.rule for r in results]
    assert rule_names == [
        "R1_category_exclusion",
        "R2_min_transaction",
        "R7_ml_rate_cap",
        "R3_anti_fatigue",
        "R4_frequency_gate",
        "R5_channel_applicability",
        "R6_budget_reservation",
    ]


# ---------------------------------------------------------------------------
# R6 — read-only budget / daily-limit check (stage C)
# ---------------------------------------------------------------------------
async def test_budget_rule_is_read_only(settings):
    """R6 no longer runs ``SELECT … FOR UPDATE`` (nor any query): an
    unreachable Postgres does not change its verdict, which comes from the
    campaign row of ``eligible_campaigns``."""
    db = FakeDBEngine(raise_exc=RuntimeError("boom"))
    verdict = await budget_reservation.evaluate(_ctx(db=db, settings=settings))
    assert verdict.outcome is RuleOutcome.PASS
    assert verdict.metadata["remaining"] == 99_000.0
    assert db.calls == []

    exhausted = _campaign(budget_total=1000.0, budget_spent=999.5)
    verdict = await budget_reservation.evaluate(
        _ctx(db=db, settings=settings, campaign=exhausted))
    assert verdict.outcome is RuleOutcome.REJECT
    assert "budget exhausted" in verdict.reason
    assert db.calls == []


async def test_budget_rule_skips_without_campaign(settings):
    verdict = await budget_reservation.evaluate(_ctx(campaign=None, settings=settings))
    assert verdict.outcome is RuleOutcome.SKIP


@pytest.mark.parametrize(
    ("daily_limit", "spent_today", "expected"),
    [
        (300.0, 300.0, RuleOutcome.REJECT),    # used up exactly
        (300.0, 300.01, RuleOutcome.REJECT),   # limit lowered after payouts
        (300.0, 299.99, RuleOutcome.PASS),     # the accrual decides the rest
        (None, 1_000_000.0, RuleOutcome.PASS),  # no daily limit
    ],
)
async def test_budget_rule_daily_limit(settings, daily_limit, spent_today, expected):
    """B22: the campaign's daily limit (``spent_today`` — today's accruals,
    UTC day) already used up → no offer."""
    campaign = _campaign(daily_limit=daily_limit, spent_today=spent_today)
    verdict = await budget_reservation.evaluate(_ctx(settings=settings, campaign=campaign))
    assert verdict.outcome is expected, verdict.reason
    if expected is RuleOutcome.REJECT:
        assert "daily limit reached" in verdict.reason
        assert verdict.metadata == {"spent_today": spent_today, "daily_limit": daily_limit}


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


# ---------------------------------------------------------------------------
# R7 — ML rate cap (фаза 18)
# ---------------------------------------------------------------------------
def _snapshot(global_enabled=True, **buckets) -> str:
    import json
    default = {
        "mass": {"min_rate": 1.0, "max_rate": 7.0, "enabled": True},
        "premium": {"min_rate": 3.0, "max_rate": 15.0, "enabled": True},
    }
    default.update(buckets)
    return json.dumps({"global_enabled": global_enabled, "buckets": default})


async def test_r7_skips_without_snapshot(settings):
    """Redis пуст → SKIP: отсутствие конфига не гасит выдачу."""
    redis = FakeRedis()
    ctx = _ctx(redis=redis, settings=settings)
    verdict = await ml_rate_cap.evaluate(ctx)
    assert verdict.outcome is RuleOutcome.SKIP
    assert ("get", "ml_limits:snapshot") in redis.calls


async def test_r7_global_kill_switch_rejects_everything(settings):
    redis = FakeRedis()
    redis.data["ml_limits:snapshot"] = _snapshot(global_enabled=False)
    ctx = _ctx(redis=redis, settings=settings)
    verdict = await ml_rate_cap.evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert "disabled globally" in verdict.reason


async def test_r7_rejects_rate_above_segment_cap(settings):
    redis = FakeRedis()
    redis.data["ml_limits:snapshot"] = _snapshot()
    # дециль 5 → корзина mass (cap 7%); кампания 9% — выше потолка
    ctx = _ctx(redis=redis, settings=settings,
               user_segment_id=5, campaign=_campaign(cashback_rate=9.0))
    verdict = await ml_rate_cap.evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert "above segment cap" in verdict.reason
    assert verdict.metadata["bucket"] == "mass"


async def test_r7_rejects_rate_below_segment_min(settings):
    redis = FakeRedis()
    redis.data["ml_limits:snapshot"] = _snapshot()
    # дециль 9 → premium (min 3%); кампания 2% — ниже интересного порога
    ctx = _ctx(redis=redis, settings=settings,
               user_segment_id=9, campaign=_campaign(cashback_rate=2.0))
    verdict = await ml_rate_cap.evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert "below segment minimum" in verdict.reason


async def test_r7_passes_rate_within_limits_and_caches_snapshot(settings):
    redis = FakeRedis()
    redis.data["ml_limits:snapshot"] = _snapshot()
    ctx = _ctx(redis=redis, settings=settings,
               user_segment_id=5, campaign=_campaign(cashback_rate=3.0))

    first = await ml_rate_cap.evaluate(ctx)
    assert first.outcome is RuleOutcome.PASS
    assert first.metadata["bucket"] == "mass"

    # Второй вызов берёт снапшот из кэша процесса — Redis не трогается.
    n_calls = len(redis.calls)
    second = await ml_rate_cap.evaluate(ctx)
    assert second.outcome is RuleOutcome.PASS
    assert len(redis.calls) == n_calls


async def test_r7_skips_unknown_bucket(settings):
    redis = FakeRedis()
    redis.data["ml_limits:snapshot"] = _snapshot()
    ctx = _ctx(redis=redis, settings=settings, user_segment_id=None)
    verdict = await ml_rate_cap.evaluate(ctx)
    assert verdict.outcome is RuleOutcome.SKIP


async def test_r7_in_chain_blocks_before_redis_heavy_rules(settings):
    """Полная цепочка: REJECT на R7 не доходит до R3/R6 (анти-fatigue/бюджет)."""
    redis = FakeRedis()
    redis.data["ml_limits:snapshot"] = _snapshot(global_enabled=False)
    db = FakeDBEngine(row=SimpleNamespace(
        budget_total=100_000.0, budget_spent=0.0,
        status="ACTIVE", cashback_rate=3.0))
    ctx = _ctx(redis=redis, db=db, settings=settings)
    verdict = await BusinessRulesEngine().evaluate(ctx)
    assert verdict.outcome is RuleOutcome.REJECT
    assert verdict.rule == "R7_ml_rate_cap"
    assert db.calls == []  # до бюджетного правила не дошли
