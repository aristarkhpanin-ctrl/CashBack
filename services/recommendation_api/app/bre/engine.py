"""BRE orchestrator — runs the six rules in priority order with early exit."""
from __future__ import annotations

from typing import Awaitable, Callable

from app.bre.models import RuleContext, RuleOutcome, RuleResult
from app.bre.rules import (
    anti_fatigue,
    budget_reservation,
    category_exclusion,
    channel_applicability,
    frequency_gate,
    min_transaction,
)

RuleFn = Callable[[RuleContext], Awaitable[RuleResult]]


# Priority order from chapter 3.2, table 19. Cheap, in-memory checks come
# first; the budget rule (which acquires a Postgres row-lock) is last so
# every preceding REJECT spares us a DB round-trip.
DEFAULT_RULES: list[tuple[str, RuleFn]] = [
    ("R1_category_exclusion",   category_exclusion.evaluate),
    ("R2_min_transaction",      min_transaction.evaluate),
    ("R3_anti_fatigue",         anti_fatigue.evaluate),
    ("R4_frequency_gate",       frequency_gate.evaluate),
    ("R5_channel_applicability", channel_applicability.evaluate),
    ("R6_budget_reservation",   budget_reservation.evaluate),
]


class BusinessRulesEngine:
    """Apply the six rules in order; stop at the first non-PASS verdict.

    The engine returns a list of every rule that ran (so observability
    sees how far evaluation reached) plus a single ``final`` verdict.
    """

    def __init__(self, rules: list[tuple[str, RuleFn]] | None = None) -> None:
        self.rules: list[tuple[str, RuleFn]] = rules or DEFAULT_RULES

    async def evaluate(self, ctx: RuleContext) -> RuleResult:
        last: RuleResult | None = None
        for _, rule in self.rules:
            last = await rule(ctx)
            if last.outcome is RuleOutcome.REJECT:
                return last
            # SKIP rules don't fail the chain — keep going.
        # If we never received a REJECT, the candidate passes.
        return last or RuleResult.passed_("noop")

    async def evaluate_verbose(self, ctx: RuleContext) -> list[RuleResult]:
        """Run every rule (no early exit) — used by debug endpoints."""
        results: list[RuleResult] = []
        for _, rule in self.rules:
            results.append(await rule(ctx))
        return results
