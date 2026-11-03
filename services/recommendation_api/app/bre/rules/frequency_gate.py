"""R4 — frequency gate (priority 4).

Caps the number of recommendations shown to a user in the last 24h.
Counter lives in Redis under ``rec_count:{user_id}`` with a 24h TTL.
:func:`release` takes back the count of an offer that passed the BRE but
was not served.
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
    new_count = count + 1
    await ctx.redis.setex(key, COUNTER_TTL, str(new_count))
    return RuleResult.passed_(RULE_NAME, count=new_count, limit=limit)


async def release(ctx: RuleContext) -> None:
    """Вернуть показ в лимит: оффер прошёл BRE, но клиенту не отдан
    (этап D). Счётчик — как в :func:`evaluate` (GET + SETEX на 24 ч); нет
    ключа — нечего возвращать, ноль показов — ключа нет."""
    if ctx.redis is None:
        return
    key = _key(ctx.user_id)
    raw = await ctx.redis.get(key)
    if raw is None:
        return
    left = int(raw) - 1
    if left > 0:
        await ctx.redis.setex(key, COUNTER_TTL, str(left))
    else:
        await ctx.redis.delete(key)
