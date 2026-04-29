"""R6 — budget reservation (priority 6, last because it is the most
expensive — it acquires a row-level lock in PostgreSQL).

Atomically checks ``cashback_campaigns.budget_total - budget_spent``
under ``SELECT … FOR UPDATE`` and rejects the candidate if the
remaining budget can't fund the minimum award.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.bre.models import RuleContext, RuleResult

log = logging.getLogger(__name__)

RULE_NAME = "R6_budget_reservation"
DEFAULT_MIN_AWARD = 1.0  # roubles


_BUDGET_QUERY = text(
    """
    SELECT budget_total, budget_spent, status, cashback_rate
      FROM cashback_campaigns
     WHERE campaign_id = :campaign_id
     FOR UPDATE
    """
)


async def evaluate(ctx: RuleContext) -> RuleResult:
    if ctx.db is None or ctx.campaign_id is None:
        return RuleResult.skip(RULE_NAME, reason="db or campaign_id missing")

    min_award = DEFAULT_MIN_AWARD
    if ctx.settings is not None:
        min_award = float(getattr(ctx.settings, "min_award_threshold", min_award))

    try:
        async with ctx.db.begin() as conn:  # type: ignore[attr-defined]
            row = (
                await conn.execute(_BUDGET_QUERY, {"campaign_id": ctx.campaign_id})
            ).first()
    except Exception as exc:  # noqa: BLE001
        log.warning("budget_query_failed: %s", exc)
        return RuleResult.skip(RULE_NAME, reason=f"db error: {exc}")

    if row is None:
        return RuleResult.reject(
            RULE_NAME, reason=f"campaign {ctx.campaign_id} not found"
        )

    total = float(row.budget_total)
    spent = float(row.budget_spent or 0.0)
    remaining = total - spent
    if str(getattr(row, "status", "ACTIVE")) != "ACTIVE":
        return RuleResult.reject(
            RULE_NAME, reason=f"campaign status={row.status} is not ACTIVE",
        )
    if remaining <= min_award:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"budget exhausted: remaining={remaining:.2f} ≤ {min_award}",
            remaining=remaining,
        )
    return RuleResult.passed_(
        RULE_NAME, remaining=remaining, total=total, spent=spent,
    )
