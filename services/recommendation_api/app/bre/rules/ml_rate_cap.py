"""R7 — ML rate cap (фаза 18): бизнес-лимиты ставок на сегментную корзину.

Бизнес задаёт ограничения через админ-панель («ML-лимиты»),
campaign_manager публикует снапшот таблицы ``ml_limits`` в Redis-ключ
``ml_limits:snapshot`` (пишется при каждом PUT /ml-limits и на каждом
тике планировщика — зеркальный комментарий в
``campaign_manager/app/api/ml_limits.py``).

Правило читает ТОЛЬКО снапшот: горячий путь рекомендаций не ходит в
Postgres за конфигом. Нет снапшота (Redis пуст/недоступен) → SKIP:
отсутствие конфига не должно гасить выдачу.

Семантика:
* глобальный выключатель (`__global__.enabled = false`) → REJECT всех;
* корзина выключена → REJECT;
* ставка кампании выше ``max_rate`` корзины → REJECT (страховка от
  экономически невыгодных предложений);
* ставка ниже ``min_rate`` → REJECT (предложение неинтересно клиенту,
  не тратим на него слот выдачи).
``daily_budget`` и ``auto_approve`` — управленческие атрибуты, в горячем
пути не применяются (см. ROADMAP, фаза 18).
"""
from __future__ import annotations

import json
import time

from app.bre.models import RuleContext, RuleResult

RULE_NAME = "R7_ml_rate_cap"
SNAPSHOT_KEY = "ml_limits:snapshot"

# Децили 1–10 → 5 корзин. ЗЕРКАЛО campaign_manager/app/segments.py и
# frontend adapters.ts — менять синхронно во всех трёх местах.
_DECILE_TO_BUCKET: dict[int, str] = {
    9: "premium", 10: "premium",
    5: "mass", 6: "mass", 7: "mass", 8: "mass",
    3: "young", 4: "young",
    2: "senior",
    1: "business",
}

# Кэш снапшота в памяти процесса — один Redis GET не на каждого из
# top-K кандидатов, а раз в _CACHE_TTL секунд на воркер.
_CACHE_TTL = 30.0
_cache: dict = {"at": 0.0, "snapshot": None}


def _reset_cache() -> None:
    """Для тестов."""
    _cache["at"] = 0.0
    _cache["snapshot"] = None


async def _load_snapshot(ctx: RuleContext) -> dict | None:
    now = time.monotonic()
    if _cache["snapshot"] is not None and now - _cache["at"] < _CACHE_TTL:
        return _cache["snapshot"]
    if ctx.redis is None:
        return None
    try:
        raw = await ctx.redis.get(SNAPSHOT_KEY)
    except Exception:  # noqa: BLE001 — Redis-сбой не гасит выдачу
        return None
    if not raw:
        return None
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return None
    _cache["at"] = now
    _cache["snapshot"] = snapshot
    return snapshot


async def evaluate(ctx: RuleContext) -> RuleResult:
    snapshot = await _load_snapshot(ctx)
    if snapshot is None:
        return RuleResult.skip(RULE_NAME, reason="ml_limits snapshot unavailable")

    if not snapshot.get("global_enabled", True):
        return RuleResult.reject(RULE_NAME, reason="ml recommendations disabled globally")

    bucket = _DECILE_TO_BUCKET.get(ctx.user_segment_id or -1)
    if bucket is None or ctx.campaign is None:
        return RuleResult.skip(RULE_NAME, reason="segment bucket or campaign missing")

    limit = (snapshot.get("buckets") or {}).get(bucket)
    if limit is None:
        return RuleResult.skip(RULE_NAME, reason=f"no limit configured for {bucket}")

    if not limit.get("enabled", True):
        return RuleResult.reject(RULE_NAME, reason=f"bucket {bucket} disabled")

    rate = ctx.campaign.get("cashback_rate")
    if rate is None:
        return RuleResult.skip(RULE_NAME, reason="campaign rate missing")
    rate = float(rate)

    max_rate = float(limit.get("max_rate", 100))
    min_rate = float(limit.get("min_rate", 0))
    if rate > max_rate:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"rate {rate} above segment cap {max_rate} ({bucket})",
            bucket=bucket, max_rate=max_rate,
        )
    if rate < min_rate:
        return RuleResult.reject(
            RULE_NAME,
            reason=f"rate {rate} below segment minimum {min_rate} ({bucket})",
            bucket=bucket, min_rate=min_rate,
        )
    return RuleResult.passed_(RULE_NAME, bucket=bucket)
