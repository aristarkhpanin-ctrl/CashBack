"""R5 — channel applicability (priority 5).

Rejects recommendations whose request channel is not in the campaign's
``allowed_channels`` list.
"""
from __future__ import annotations

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R5_channel_applicability"


async def evaluate(ctx: RuleContext) -> RuleResult:
    if ctx.channel is None or ctx.campaign is None:
        return RuleResult.skip(RULE_NAME, reason="channel or campaign missing")

    allowed = ctx.campaign.get("allowed_channels") or []
    if not allowed:
        # Empty list means "all channels".
        return RuleResult.passed_(RULE_NAME)

    if ctx.channel not in allowed:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"channel {ctx.channel!r} not in {list(allowed)}",
            allowed=list(allowed),
        )
    return RuleResult.passed_(RULE_NAME)
