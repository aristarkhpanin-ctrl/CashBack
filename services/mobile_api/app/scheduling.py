"""SNOOZE — когда напомнить клиенту об отложенном оффере.

Ответ ``SNOOZE`` остаётся в строке рекомендации: это ответ клиента
(«открыто», ADR 0015), и отложенный оффер принимается или отклоняется до
своего ``expires_at`` — respond принимает PENDING и SNOOZE.

Раньше через :data:`Settings.snooze_days` задача APScheduler (в памяти
процесса) возвращала строке ``PENDING`` — «чтобы следующая выдача отдала
оффер снова». После B29 каждую выдачу пишет recommendation_api новой
строкой, старые строки никто заново не выдаёт, и сброс только стирал ответ:
оффер задним числом выпадал из «открыто» в воронке, каналах и
``ml_online_ctr`` (этап D). Теперь ``snooze_until`` в ответе respond — лишь
момент, когда приложению напомнить об оффере; вернуть оффер в выдачу
надёжно — задача этапа E (B52).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta


class SnoozeScheduler:
    """Срок напоминания об отложенном оффере; строку рекомендации не меняет."""

    def schedule(self, recommendation_id: str, days: int) -> datetime:
        """``snooze_until`` оффера ``recommendation_id``: сейчас + ``days``."""
        return datetime.now(UTC) + timedelta(days=days)
