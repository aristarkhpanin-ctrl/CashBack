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
    ml_rate_cap,
)

RuleFn = Callable[[RuleContext], Awaitable[RuleResult]]
MarkFn = Callable[[RuleContext, RuleResult], Awaitable[None]]


# Priority order from chapter 3.2, table 19 (+R7, фаза 18). Cheap,
# in-memory checks come first; the budget rule stays last. It reads only
# the campaign row from eligible_campaigns (no DB round-trip, no row
# lock — stage C).
# R7 стоит после чистых ctx-проверок (R1/R2) и перед Redis-тяжёлыми
# R3/R4: он читает лишь кэшированный в памяти процесса снапшот лимитов.
DEFAULT_RULES: list[tuple[str, RuleFn]] = [
    ("R1_category_exclusion",   category_exclusion.evaluate),
    ("R2_min_transaction",      min_transaction.evaluate),
    ("R7_ml_rate_cap",          ml_rate_cap.evaluate),
    ("R3_anti_fatigue",         anti_fatigue.evaluate),
    ("R4_frequency_gate",       frequency_gate.evaluate),
    ("R5_channel_applicability", channel_applicability.evaluate),
    ("R6_budget_reservation",   budget_reservation.evaluate),
]

# B28: R3/R4 mark a show in Redis (recent_offer:*, rec_count:*), but their
# evaluate() only checks. The engine applies the mark of each one that
# returned PASS (a SKIP marks nothing) once the whole chain has passed, so
# a candidate that R5/R6 reject is neither hidden for 14 days nor counted
# against the 24 h cap.
_MARK: dict[str, MarkFn] = {
    anti_fatigue.RULE_NAME:   anti_fatigue.mark,
    frequency_gate.RULE_NAME: frequency_gate.mark,
}


class BusinessRulesEngine:
    """Apply the six rules in order; stop at the first non-PASS verdict.

    The engine returns a list of every rule that ran (so observability
    sees how far evaluation reached) plus a single ``final`` verdict.
    """

    def __init__(self, rules: list[tuple[str, RuleFn]] | None = None) -> None:
        self.rules: list[tuple[str, RuleFn]] = rules or DEFAULT_RULES

    async def evaluate(self, ctx: RuleContext) -> RuleResult:
        last: RuleResult | None = None
        to_mark: list[RuleResult] = []
        for _, rule in self.rules:
            last = await rule(ctx)
            if last.outcome is RuleOutcome.REJECT:
                return last  # nothing marked yet — nothing to give back
            if last.outcome is RuleOutcome.PASS and last.rule in _MARK:
                to_mark.append(last)
            # SKIP rules don't fail the chain — keep going.
        # If we never received a REJECT, the candidate passes: mark it.
        for result in to_mark:
            await _MARK[result.rule](ctx, result)
        return last or RuleResult.passed_("noop")

    async def evaluate_verbose(self, ctx: RuleContext) -> list[RuleResult]:
        """Run every rule (no early exit) — used by debug endpoints.

        Writes nothing: only ``evaluate`` applies the R3/R4 marks."""
        results: list[RuleResult] = []
        for _, rule in self.rules:
            results.append(await rule(ctx))
        return results
