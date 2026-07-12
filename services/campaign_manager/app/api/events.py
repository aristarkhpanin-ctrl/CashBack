"""Server-Sent Events — realtime-канал дашборда (фаза 19).

``GET /events/stream`` держит долгое соединение и шлёт агрегаты
начислений (изменения budget_spent по кампаниям). Аутентификация не
требуется на уровне зависимости роутера: EventSource в браузере не умеет
слать кастомные заголовки, поэтому канал отдаёт только неконфиденциальные
агрегаты (дельты сумм), а не сами транзакции.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.events import sse_response_stream

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    broker = request.app.state.event_broker
    return StreamingResponse(
        sse_response_stream(broker),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Явно отключаем буферизацию nginx для этого ответа.
            "X-Accel-Buffering": "no",
        },
    )
