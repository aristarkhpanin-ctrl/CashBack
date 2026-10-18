"""R6 — budget (priority 6, last).

Read-only (stage C): judges the campaign row ``_fetch_campaigns_for_mccs``
already read from ``eligible_campaigns`` (migration 009) — no query of its
own and no ``SELECT … FOR UPDATE``. The old row lock lived in its own short
transaction, so it protected nothing (B42): an offer is not money yet.
Money is spent only by the accrual: ``AccrualEngine`` re-checks the budget
and the daily limit under the campaign row lock, and its decision is the
authoritative one.

Rejects the candidate when the campaign is not ACTIVE (rows of
``eligible_campaigns`` always are; a row that carries another status is
rejected), when the remaining budget can't fund the minimum award, or when
the campaign's daily limit is already used up (``spent_today >=
daily_limit``, B22).
"""
from __future__ import annotations

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R6_budget_reservation"
DEFAULT_MIN_AWARD = 1.0  # roubles


async def evaluate(ctx: RuleContext) -> RuleResult:
    campaign = ctx.campaign
    if campaign is None:
        return RuleResult.skip(RULE_NAME, reason="campaign missing")

    min_award = DEFAULT_MIN_AWARD
    if ctx.settings is not None:
        min_award = float(getattr(ctx.settings, "min_award_threshold", min_award))

    status = str(campaign.get("status", "ACTIVE"))
    if status != "ACTIVE":
        return RuleResult.reject(
            RULE_NAME, reason=f"campaign status={status} is not ACTIVE",
        )

    total = float(campaign["budget_total"])
    spent = float(campaign.get("budget_spent") or 0.0)
    remaining = total - spent
    if remaining <= min_award:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"budget exhausted: remaining={remaining:.2f} ≤ {min_award}",
            remaining=remaining,
        )

    daily_limit = campaign.get("daily_limit")
    if daily_limit is not None:
        spent_today = float(campaign.get("spent_today") or 0.0)
        if spent_today >= float(daily_limit):
            return RuleResult.reject(
                RULE_NAME,
                reason=(f"daily limit reached: spent_today={spent_today:.2f} "
                        f"≥ {float(daily_limit):.2f}"),
                spent_today=spent_today, daily_limit=float(daily_limit),
            )
    return RuleResult.passed_(
        RULE_NAME, remaining=remaining, total=total, spent=spent,
    )
