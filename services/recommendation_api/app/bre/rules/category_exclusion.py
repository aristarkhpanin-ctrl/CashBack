"""R1 — category exclusion (priority 1).

Rejects candidates whose MCC is in the user-specific exclusion list
(e.g. customer opted out of gambling/alcohol categories).
"""
from __future__ import annotations

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R1_category_exclusion"


async def evaluate(ctx: RuleContext) -> RuleResult:
    if not ctx.excluded_mccs:
        return RuleResult.passed_(RULE_NAME)
    if ctx.candidate_mcc in ctx.excluded_mccs:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"mcc {ctx.candidate_mcc} excluded by user preferences",
            excluded_mcc=ctx.candidate_mcc,
        )
    return RuleResult.passed_(RULE_NAME)
