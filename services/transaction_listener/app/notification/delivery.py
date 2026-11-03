"""Канал доставки оффера → ``recommendations.channel`` (stage D, B29).

``channel`` — канал, которым оффер реально ДОСТАВЛЕН клиенту: адаптер
Notification Pipeline, первым успешно отправивший событие
``recommendations.created`` (push / in-app / email / sms), либо показ в
приложении (mobile_api ставит IN_APP). Записывается один раз — первым
доставившим каналом (``channel IS NULL``): повторная доставка того же
события и показ в приложении его не меняют, отклик клиента — тоже.
По нему канальная аналитика campaign_manager считает «отправлено» и
open rate по каналам.

Строку оффера пишет recommendation_api до публикации события, поэтому к
моменту доставки она уже есть. Запись канала — best-effort: уведомление
уже ушло, сбой Postgres не повод доставлять его снова (consumer —
auto-commit), он попадает в лог и метрику.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

import structlog
from prometheus_client import Counter
from sqlalchemy import text

log = structlog.get_logger("notification.delivery")

# Имя адаптера NotificationPipeline → значение enum delivery_channel
# (миграция 003: PUSH / SMS / EMAIL / IN_APP).
CHANNEL_BY_ADAPTER: dict[str, str] = {
    "push": "PUSH",
    "in_app": "IN_APP",
    "email": "EMAIL",
    "sms": "SMS",
}

CHANNEL_RECORDED = Counter(
    "notification_channel_recorded_total",
    "Delivery channel written to recommendations.channel after a dispatch",
    ["outcome"],
)

_RECORD_CHANNEL_SQL = text(
    """
    UPDATE recommendations
       SET channel = CAST(:channel AS delivery_channel)
     WHERE recommendation_id = CAST(:rid AS uuid)
       AND channel IS NULL
    """
)


class DeliveryChannelRecorder:
    """Пишет канал успешной доставки в строку оффера (``recommendation_id``)."""

    def __init__(self, db_engine: Any) -> None:
        self._db = db_engine

    async def record(self, recommendation_id: Optional[str], adapter: str) -> bool:
        """``True`` — канал записан. ``False`` — записывать нечего (событие
        без ``recommendation_id``, неизвестный адаптер), канал уже записан
        (или строки нет) либо сбой Postgres; причина — в метрике."""
        channel = CHANNEL_BY_ADAPTER.get(adapter)
        if channel is None:
            return self._outcome("unknown_channel", adapter=adapter)
        try:
            rid = uuid.UUID(str(recommendation_id)) if recommendation_id else None
        except ValueError:
            rid = None
        if rid is None:
            # Событие без строки оффера (издатель до B29) — канал некуда писать.
            return self._outcome("no_recommendation_id",
                                 recommendation_id=recommendation_id)
        try:
            async with self._db.begin() as conn:
                result = await conn.execute(
                    _RECORD_CHANNEL_SQL, {"channel": channel, "rid": str(rid)},
                )
        except Exception as exc:  # noqa: BLE001 — уведомление уже доставлено
            log.warning("delivery_channel_record_failed", recommendation_id=str(rid),
                        channel=channel, error=f"{type(exc).__name__}: {exc}")
            CHANNEL_RECORDED.labels(outcome="error").inc()
            return False
        if result.rowcount == 0:
            # Первым доставил другой канал (или показ в приложении) — его и
            # оставляем.
            return self._outcome("already_recorded", recommendation_id=str(rid))
        CHANNEL_RECORDED.labels(outcome="recorded").inc()
        log.info("delivery_channel_recorded", recommendation_id=str(rid), channel=channel)
        return True

    @staticmethod
    def _outcome(outcome: str, **fields: Any) -> bool:
        CHANNEL_RECORDED.labels(outcome=outcome).inc()
        log.info("delivery_channel_not_recorded", outcome=outcome, **fields)
        return False
