"""Mobile BFF endpoints — chapter 3.2, table 22.

The router exposes four endpoints under ``/v1/mobile``:

    * GET    /v1/mobile/recommendations/{user_id}
    * POST   /v1/mobile/recommendations/{rec_id}/respond
    * GET    /v1/mobile/cashback/history/{user_id}
    * GET    /v1/mobile/cashback/balance/{user_id}

These call the upstream Recommendation API and Postgres directly; the
response is *trimmed* of model-internals (``model_score``, ``top_factors``)
and *enriched* with mobile-friendly fields (Cyrillic category name,
icon URL, deeplink, terms summary).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from sqlalchemy import text

from app.clients.recommendation_client import CircuitBreakerOpen
from app.config import get_settings
from app.mcc_registry import icon_url, lookup
from app.schemas import (
    CashbackBalance,
    CashbackHistoryItem,
    CashbackHistoryResponse,
    MobileRecommendation,
    MobileRecommendationsResponse,
    RespondAction,
    RespondRequest,
    RespondResponse,
)

log = structlog.get_logger("api.mobile")

router = APIRouter(prefix="/v1/mobile", tags=["mobile"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _fetch_campaign_context(
    db_engine: Any,
    campaign_ids: list[uuid.UUID],
) -> dict[str, dict]:
    """Pull campaign details (name, cashback_rate, end_date) for enrichment."""
    if not campaign_ids:
        return {}
    sql = text(
        """
        SELECT campaign_id::text  AS campaign_id,
               name,
               cashback_rate,
               end_date
          FROM cashback_campaigns
         WHERE campaign_id = ANY(:cids)
        """
    )
    out: dict[str, dict] = {}
    try:
        async with db_engine.connect() as conn:
            rows = (await conn.execute(
                sql, {"cids": [str(c) for c in campaign_ids]},
            )).mappings().all()
            for r in rows:
                out[r["campaign_id"]] = dict(r)
    except Exception as exc:  # noqa: BLE001
        log.warning("campaign_context_failed", error=str(exc))
    return out


def _format_rate(value: Any) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v.is_integer():
        return f"{int(v)}%"
    return f"{v:g}%"


def _trim(text_value: str, max_len: int = 140) -> str:
    text_value = text_value or ""
    if len(text_value) <= max_len:
        return text_value
    return text_value[: max_len - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# 1) GET /v1/mobile/recommendations/{user_id}
# ---------------------------------------------------------------------------
@router.get(
    "/recommendations/{user_id}",
    response_model=MobileRecommendationsResponse,
)
async def get_mobile_recommendations(
    request: Request,
    user_id: uuid.UUID = Path(...),
    top_k: int | None = Query(default=None, ge=1, le=20),
    transaction_amount: float | None = Query(default=None, ge=0),
    channel: str | None = Query(default=None,
                                pattern="^(ONLINE|POS|ATM|MOBILE)$"),
) -> MobileRecommendationsResponse:
    state = request.app.state
    settings = state.settings
    top_k = top_k or settings.default_top_k

    # ---- 1) call upstream Recommendation API ------------------------
    try:
        upstream = await state.recommendation_client.get_recommendations(
            str(user_id),
            top_k=top_k,
            transaction_amount=transaction_amount,
            channel=channel,
        )
    except CircuitBreakerOpen as exc:
        log.warning("recommendation_api_breaker_open", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="recommendation service unavailable",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("recommendation_api_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream recommendation api error: {exc}",
        )

    raw_items: list[dict] = list(upstream.get("recommendations", []))

    # ---- 2) join with campaign details (for cashback_rate, name, end_date) ----
    campaign_ids = [
        uuid.UUID(item["campaign_id"]) for item in raw_items
        if item.get("campaign_id")
    ]
    campaigns = await _fetch_campaign_context(state.db_engine, campaign_ids)

    # ---- 3) persist a recommendation row per item so /respond later
    #         can resolve it deterministically ------------------------
    persisted_ids: dict[int, uuid.UUID] = {}
    if raw_items:
        try:
            insert_sql = text(
                """
                INSERT INTO recommendations (
                    recommendation_id, user_id, campaign_id, mcc_code,
                    model_score, generated_at, response_status, expires_at
                ) VALUES (
                    :rid, :uid, :cid, :mcc, :score, :gen_at, 'PENDING', :exp_at
                )
                ON CONFLICT DO NOTHING
                """
            )
            now = datetime.now(UTC)
            async with state.db_engine.begin() as conn:
                for idx, item in enumerate(raw_items):
                    cid_raw = item.get("campaign_id")
                    if not cid_raw:
                        continue
                    rid = uuid.uuid4()
                    persisted_ids[idx] = rid
                    expires_at = (campaigns.get(str(cid_raw)) or {}).get(
                        "end_date", now + timedelta(days=14)
                    )
                    await conn.execute(insert_sql, {
                        "rid": rid,
                        "uid": user_id,
                        "cid": uuid.UUID(cid_raw),
                        "mcc": item.get("mcc_code", ""),
                        "score": float(item.get("score", 0)),
                        "gen_at": now,
                        "exp_at": expires_at,
                    })
        except Exception as exc:  # noqa: BLE001
            log.warning("persist_recommendations_failed", error=str(exc))

    # ---- 4) build mobile-shaped payload (drop model_score/top_factors) ----
    mobile_items: list[MobileRecommendation] = []
    for idx, item in enumerate(raw_items):
        mcc = str(item.get("mcc_code", "")).strip()
        info = lookup(mcc)
        cid = item.get("campaign_id")
        ctx = campaigns.get(str(cid)) if cid else None
        rate = ctx.get("cashback_rate") if ctx else None
        rec_id = persisted_ids.get(idx)
        mobile_items.append(MobileRecommendation(
            recommendation_id=rec_id,
            mcc_code=mcc or "0000",
            category_name=info.name,
            category_icon_url=icon_url(settings.cdn_base_url, mcc),
            cashback_rate=_format_rate(rate),
            expires_at=ctx.get("end_date") if ctx else None,
            terms_summary=_trim(info.terms_summary, 140),
            deeplink=settings.deeplink_template.format(
                recommendation_id=str(rec_id) if rec_id else "unknown"
            ),
            campaign_id=uuid.UUID(cid) if cid else None,
            campaign_name=(ctx or {}).get("name"),
        ))

    return MobileRecommendationsResponse(
        user_id=user_id,
        recommendations=mobile_items,
        model_version=upstream.get("model_version"),
    )


# ---------------------------------------------------------------------------
# 2) POST /v1/mobile/recommendations/{rec_id}/respond
# ---------------------------------------------------------------------------
# Отклик — один условный UPDATE (B13): ответить можно только на оффер в статусе
# PENDING или SNOOZE, срок которого не истёк; ACCEPTED, DECLINED и EXPIRED
# терминальны. Конкурирующий запрос ждёт row-lock, после COMMIT первого
# перепроверяет WHERE и получает 0 строк. Поэтому повторное «принятие» (в т.ч.
# уже оплаченного оффера — ключ accepted_offers создался бы заново),
# DECLINED → ACCEPTED и отклик на просроченный оффер невозможны.
# SNOOZE не терминален: «отложенный» оффер можно принять или отклонить и до
# реактивации. Задача реактивации живёт в памяти процесса (scheduling.py) и
# теряется при рестарте — без этого оффер навсегда застревал бы в SNOOZE с 409.
# Выплаты это не повторяет: SNOOZE → ACCEPTED случается один раз, дальше
# ACCEPTED терминален, а реактивация меняет только строку в статусе SNOOZE.
# expires_at в схеме NOT NULL (миграция 001); если NULL всё же встретится,
# сравнение не пройдёт — оффер без срока не активируется (fail-closed).
_RESPONDABLE_STATUSES = ("PENDING", "SNOOZE")

_RESPOND_TRANSITION_SQL = text(
    """
    UPDATE recommendations
       SET response_status = :status,
           responded_at    = now(),
           channel         = :channel
     WHERE recommendation_id = :rid
       AND response_status IN ('PENDING', 'SNOOZE')
       AND expires_at > now()
    RETURNING user_id::text     AS user_id,
              campaign_id::text AS campaign_id,
              mcc_code,
              expires_at,
              responded_at
    """
)

# Почему переход не состоялся — для выбора кода ответа. Выполняется в той же
# транзакции, что и UPDATE, поэтому now() здесь тот же самый.
_RESPOND_STATE_SQL = text(
    """
    SELECT response_status::text                       AS response_status,
           (expires_at IS NULL OR expires_at <= now()) AS expired
      FROM recommendations
     WHERE recommendation_id = :rid
    """
)


def _respond_rejection(current: Any) -> HTTPException:
    """404 / 410 / 409 для отклика, который не прошёл условный UPDATE."""
    if current is None:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                             detail="recommendation not found")
    open_offer = current.response_status in _RESPONDABLE_STATUSES
    if current.response_status == "EXPIRED" or (open_offer and current.expired):
        return HTTPException(status_code=status.HTTP_410_GONE,
                             detail="recommendation expired")
    if open_offer:
        # UPDATE не прошёл, а оффер открыт и не истёк — строку изменили
        # между UPDATE и этим SELECT; повтор запроса получит итоговый ответ.
        return HTTPException(status_code=status.HTTP_409_CONFLICT,
                             detail="recommendation changed concurrently, retry")
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"recommendation already responded (status={current.response_status})",
    )


# ---- активация оффера: горячий ключ Redis для transaction_listener --------
# Ключ пишется до COMMIT: если Redis недоступен, переход откатывается и клиент
# может повторить (503), а не застревает в ACCEPTED без ключа (повтор после
# COMMIT получил бы 409). Обратная сторона — ключ может пережить откат: Redis
# применил запись, но ответ потерян (тайм-аут), COMMIT не прошёл, запрос
# отменён. Такой ключ оплатил бы покупку по офферу, который в БД не принят, а
# повтор после 503 создал бы новый ключ — две выплаты по одному офферу (B13).
# Поэтому:
#   * у каждой попытки свой activation_id (он же в значении ключа); запись —
#     Lua-скрипт, который ничего не пишет, если попытка уже аннулирована;
#   * при любом выходе без COMMIT попытка аннулируется: ставится её
#     fence-ключ, а ключ оффера удаляется, только если в нём всё ещё значение
#     этой попытки (compare-and-delete — ключ другого оффера на тот же
#     user×MCC не трогаем). Запись, которая дойдёт до Redis позже
#     аннулирования (застряла в сети или в очереди Redis), упрётся в fence;
#   * обе операции ограничены по времени: запись идёт внутри открытой
#     транзакции под row-lock, и «тормозящий» Redis не должен держать
#     соединение пула дольше _REDIS_OP_TIMEOUT_S (у redis-py 5.x тайм-аута
#     сокета нет вовсе, у 8.x — 5 с на попытку плюс ретраи).
_REDIS_OP_TIMEOUT_S = 1.0
_ACTIVATION_VOID_TTL_S = 24 * 3600
_ACTIVATION_VOID_KEY_FMT = "accepted_offers_void:{activation_id}"

# KEYS[1] — ключ оффера, KEYS[2] — fence попытки; ARGV[1] — значение, ARGV[2] — TTL.
_ACTIVATE_OFFER_LUA = """
if redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""

# KEYS[1] — ключ оффера, KEYS[2] — fence попытки; ARGV[1] — значение попытки,
# ARGV[2] — TTL fence-ключа.
_VOID_ACTIVATION_LUA = """
redis.call('SET', KEYS[2], '1', 'EX', ARGV[2])
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# Аннулирования, которые доводятся до конца после отмены запроса.
_background_voids: set[asyncio.Task] = set()


def _describe(exc: BaseException) -> str:
    # repr() исключений redis-py 8.x — «network:ConnectionError», без причины.
    return f"{type(exc).__name__}: {exc}"


@dataclass(frozen=True)
class _Activation:
    key: str       # accepted_offers:{user_id}:{mcc_code}
    void_key: str  # fence этой попытки
    body: str      # значение ключа, уникальное для попытки (activation_id)


async def _activate_offer(redis: Any, activation: _Activation, ttl: int) -> None:
    """Пишет ключ оффера; любое исключение — «записан ли ключ, неизвестно»."""
    written = await asyncio.wait_for(
        redis.eval(_ACTIVATE_OFFER_LUA, 2, activation.key, activation.void_key,
                   activation.body, ttl),
        timeout=_REDIS_OP_TIMEOUT_S,
    )
    if int(written) != 1:
        raise RuntimeError("offer activation was voided before it was written")


async def _void_activation(redis: Any, activation: _Activation) -> None:
    """Best-effort: аннулировать попытку и снять её ключ, если он ещё её."""
    try:
        removed = await asyncio.wait_for(
            redis.eval(_VOID_ACTIVATION_LUA, 2, activation.key, activation.void_key,
                       activation.body, _ACTIVATION_VOID_TTL_S),
            timeout=_REDIS_OP_TIMEOUT_S,
        )
        log.info("offer_activation_voided", key=activation.key, removed=int(removed))
    except Exception as exc:  # noqa: BLE001
        # Ключ непринятого оффера может дожить до TTL: нужна ручная проверка
        # (и проверка статуса рекомендации в transaction_listener, B45).
        log.error("offer_activation_void_failed", key=activation.key,
                  void_key=activation.void_key, error=_describe(exc))


async def _void_activation_shielded(redis: Any, activation: _Activation) -> None:
    """Аннулирование доводится до конца, даже если сам запрос отменён
    (обрыв соединения клиента, остановка воркера)."""
    task = asyncio.ensure_future(_void_activation(redis, activation))
    _background_voids.add(task)
    task.add_done_callback(_background_voids.discard)
    await asyncio.shield(task)


@router.post(
    "/recommendations/{recommendation_id}/respond",
    response_model=RespondResponse,
)
async def respond_to_recommendation(
    request: Request,
    recommendation_id: uuid.UUID,
    payload: RespondRequest,
) -> RespondResponse:
    state = request.app.state
    settings = state.settings

    activation: _Activation | None = None
    committed = False
    snooze_until: datetime | None = None

    try:
        async with state.db_engine.begin() as conn:
            row = (await conn.execute(_RESPOND_TRANSITION_SQL, {
                "status": payload.action.value,
                "channel": payload.channel,
                "rid": recommendation_id,
            })).first()
            if row is None:
                current = (await conn.execute(
                    _RESPOND_STATE_SQL, {"rid": recommendation_id},
                )).first()
                raise _respond_rejection(current)

            if payload.action is RespondAction.ACCEPTED:
                # TTL = остаток жизни оффера по часам БД: responded_at — это
                # now() транзакции, а guard гарантирует expires_at > now().
                accepted_at = row.responded_at
                ttl = math.ceil((row.expires_at - accepted_at).total_seconds())
                activation_id = uuid.uuid4().hex
                activation = _Activation(
                    key=settings.accepted_offer_key_fmt.format(
                        user_id=row.user_id, mcc_code=row.mcc_code.strip(),
                    ),
                    void_key=_ACTIVATION_VOID_KEY_FMT.format(activation_id=activation_id),
                    body=json.dumps({
                        "campaign_id": row.campaign_id,
                        "recommendation_id": str(recommendation_id),
                        "accepted_at": accepted_at.isoformat(),
                        "activation_id": activation_id,
                    }),
                )
                try:
                    await _activate_offer(state.redis, activation, ttl)
                except Exception as exc:  # noqa: BLE001
                    log.warning("offer_activation_failed", key=activation.key,
                                error=_describe(exc))
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="offer activation is temporarily unavailable, retry",
                    ) from exc
        committed = True
    finally:
        if activation is not None and not committed:
            # Переход откатился (503, сбой COMMIT, отмена запроса), а ключ мог
            # быть записан — снимаем его, пока он не оплатил непринятый оффер.
            await _void_activation_shielded(state.redis, activation)

    # ---- переход зафиксирован: только теперь побочные эффекты ----------
    if payload.action is RespondAction.ACCEPTED:
        producer = getattr(state, "kafka_producer", None)
        if producer is not None:
            event = {
                "recommendation_id": str(recommendation_id),
                "user_id": row.user_id,
                "campaign_id": row.campaign_id,
                "mcc_code": row.mcc_code.strip(),
                "accepted_at": accepted_at.isoformat(),
                "ttl_seconds": ttl,
            }
            try:
                await producer.send(
                    settings.offer_accepted_topic,
                    key=row.user_id.encode("utf-8"),
                    value=json.dumps(event).encode("utf-8"),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("kafka_publish_failed", error=str(exc))

    elif payload.action is RespondAction.SNOOZE:
        snooze_until = state.snooze_scheduler.schedule(
            str(recommendation_id), days=settings.snooze_days,
        )

    return RespondResponse(
        recommendation_id=recommendation_id,
        action=payload.action,
        accepted_offer_key=activation.key if activation is not None else None,
        snooze_until=snooze_until,
    )


# ---------------------------------------------------------------------------
# 3) GET /v1/mobile/cashback/history/{user_id}
# ---------------------------------------------------------------------------
@router.get(
    "/cashback/history/{user_id}",
    response_model=CashbackHistoryResponse,
)
async def cashback_history(
    request: Request,
    user_id: uuid.UUID,
    period_from: datetime | None = Query(default=None),
    period_to: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> CashbackHistoryResponse:
    state = request.app.state
    now = datetime.now(UTC)
    period_to = period_to or now
    period_from = period_from or (period_to - timedelta(days=30))

    sql = text(
        """
        SELECT accrual_id::text       AS accrual_id,
               transaction_id,
               mcc_code,
               transaction_amount,
               cashback_amount,
               status::text           AS status,
               accrued_at
          FROM cashback_accruals
         WHERE user_id = :uid
           AND accrued_at BETWEEN :pfrom AND :pto
         ORDER BY accrued_at DESC
         LIMIT :limit
        """
    )
    async with state.db_engine.connect() as conn:
        rows = (await conn.execute(sql, {
            "uid": user_id,
            "pfrom": period_from,
            "pto": period_to,
            "limit": limit,
        })).mappings().all()

    items: list[CashbackHistoryItem] = []
    for r in rows:
        mcc = str(r["mcc_code"]).strip()
        items.append(CashbackHistoryItem(
            accrual_id=uuid.UUID(r["accrual_id"]),
            transaction_id=r["transaction_id"],
            mcc_code=mcc,
            category_name=lookup(mcc).name,
            transaction_amount=Decimal(r["transaction_amount"]),
            cashback_amount=Decimal(r["cashback_amount"]),
            status=r["status"],
            accrued_at=r["accrued_at"],
        ))

    return CashbackHistoryResponse(
        user_id=user_id,
        period_from=period_from,
        period_to=period_to,
        items=items,
    )


# ---------------------------------------------------------------------------
# 4) GET /v1/mobile/cashback/balance/{user_id}
# ---------------------------------------------------------------------------
@router.get(
    "/cashback/balance/{user_id}",
    response_model=CashbackBalance,
)
async def cashback_balance(
    request: Request,
    user_id: uuid.UUID,
) -> CashbackBalance:
    state = request.app.state
    sql = text(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'PENDING' THEN cashback_amount END), 0)
                AS pending_amount,
            COALESCE(SUM(CASE WHEN status = 'PAID'    THEN cashback_amount END), 0)
                AS paid_amount
          FROM cashback_accruals
         WHERE user_id = :uid
        """
    )
    async with state.db_engine.connect() as conn:
        row = (await conn.execute(sql, {"uid": user_id})).one()

    pending = Decimal(row.pending_amount or 0)
    paid = Decimal(row.paid_amount or 0)
    return CashbackBalance(
        user_id=user_id,
        pending_amount=pending,
        paid_amount=paid,
        available_amount=paid,
    )
