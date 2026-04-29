"""R2 — minimum transaction amount (priority 2)."""
from __future__ import annotations

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R2_min_transaction"


async def evaluate(ctx: RuleContext) -> RuleResult:
    if ctx.transaction_amount is None or ctx.campaign is None:
        return RuleResult.skip(RULE_NAME, reason="amount or campaign missing")

    threshold = ctx.campaign.get("min_transaction_amount")
    if threshold is None:
        return RuleResult.passed_(RULE_NAME)

    if float(ctx.transaction_amount) < float(threshold):
        return RuleResult.reject(
            RULE_NAME,
            reason=(f"amount {ctx.transaction_amount} below "
                    f"campaign minimum {threshold}"),
            min_required=float(threshold),
        )
    return RuleResult.passed_(RULE_NAME)
