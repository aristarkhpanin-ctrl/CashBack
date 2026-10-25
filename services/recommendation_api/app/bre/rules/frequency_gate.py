"""R4 — frequency gate (priority 4).

Caps the number of recommendations shown to a user in the last 24h.
Counter lives in Redis under ``rec_count:{user_id}`` with a 24h TTL.
``evaluate`` only reads it; ``mark`` stores the incremented value, and the
engine calls it once the whole chain has passed (B28) — a candidate that
R5/R6 reject does not use up the cap.
"""
from __future__ import annotations

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R4_frequency_gate"
DEFAULT_LIMIT = 5
COUNTER_TTL = 24 * 3600


def _key(user_id: str) -> str:
    return f"rec_count:{user_id}"


async def evaluate(ctx: RuleContext) -> RuleResult:
    if ctx.redis is None:
        return RuleResult.skip(RULE_NAME, reason="redis missing")

    limit = DEFAULT_LIMIT
    if ctx.settings is not None:
        limit = int(getattr(ctx.settings, "frequency_gate_per_24h", DEFAULT_LIMIT))

    key = _key(ctx.user_id)
    raw = await ctx.redis.get(key)
    count = int(raw) if raw is not None else 0
    if count >= limit:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"reached {limit} recommendations in last 24h",
            count=count,
            limit=limit,
        )
    return RuleResult.passed_(RULE_NAME, count=count + 1, limit=limit)


async def mark(ctx: RuleContext, result: RuleResult) -> None:
    """Count the served offer: store the value ``evaluate`` computed."""
    await ctx.redis.setex(_key(ctx.user_id), COUNTER_TTL, str(result.metadata["count"]))
