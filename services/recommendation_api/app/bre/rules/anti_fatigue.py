"""R3 — anti-fatigue (priority 3).

Suppresses an offer that has already been shown to this user for the
same campaign in the last ``ANTI_FATIGUE_TTL_DAYS`` (default 14).

Implementation: Redis key ``recent_offer:{user_id}:{campaign_id}``
with a TTL.  The rule both *checks* the key and *sets* it on PASS, so
that the very next inference run for the same pair returns REJECT.
"""
from __future__ import annotations

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R3_anti_fatigue"
DEFAULT_TTL_SECONDS = 14 * 24 * 3600


def _key(user_id: str, campaign_id: str) -> str:
    return f"recent_offer:{user_id}:{campaign_id}"


async def evaluate(ctx: RuleContext) -> RuleResult:
    if ctx.redis is None or ctx.campaign_id is None:
        return RuleResult.skip(RULE_NAME, reason="redis or campaign missing")

    ttl = DEFAULT_TTL_SECONDS
    if ctx.settings is not None:
        ttl = int(getattr(ctx.settings, "anti_fatigue_ttl_days", 14)) * 24 * 3600

    key = _key(ctx.user_id, ctx.campaign_id)
    seen = await ctx.redis.get(key)
    if seen:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"already shown within last {ttl // 86400} days",
            key=key,
        )

    # Mark as shown so subsequent calls within the TTL window are filtered.
    await ctx.redis.setex(key, ttl, "1")
    return RuleResult.passed_(RULE_NAME, ttl=ttl)
