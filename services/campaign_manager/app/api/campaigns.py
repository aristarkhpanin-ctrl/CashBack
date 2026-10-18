"""Campaign CRUD + applicability + budget view — chapter 3.2, table 18.

Highlights
~~~~~~~~~~
* ``GET /campaigns/applicable/{user_id}`` lists the campaigns the user is
  eligible for via the SQL function ``eligible_campaigns`` (migration 009) —
  the one definition of eligibility shared with recommendation_api (offers)
  and transaction_listener (accruals), so this list cannot drift from what is
  actually paid (B26).
* ``GET /campaigns/{id}/budget`` is read-only: only the accrual transaction
  spends budget. The former ``POST …/budget/check`` "reservation" debited
  ``budget_spent`` for good — nothing ever released it (B23).
* Every status change is compare-and-set; the DB trigger
  ``trg_campaign_status_guard`` is the last line of defence (B38).
* ``DELETE`` removes only a DRAFT without history; anything else is archived
  (``POST …/archive``) — the accrual ledger is never deleted (B24).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.fsm import InvalidTransition, transition
from app.models import (
    CampaignCategory,
    CashbackAccrual,
    CashbackCampaign,
    Recommendation,
    User,
)
from app.rbac import require_permission
from app.schemas import (
    ApplicableCampaign,
    AudienceEstimateResponse,
    CampaignBudget,
    CampaignCreate,
    CampaignResponse,
    CampaignStats,
    CampaignSummary,
    CampaignUpdate,
    StatusActionResponse,
)
from app.security import get_current_user

log = structlog.get_logger("api.campaigns")

# Логин нужен всем маршрутам; конкретное право — у каждого маршрута свой
# (матрица прав, контракт этапа B §4; её фиксирует test_route_permissions).
router = APIRouter(
    prefix="/campaigns",
    tags=["campaigns"],
    dependencies=[Depends(get_current_user)],
)

# Фаза 26: кампании гейтятся правами (динамическая матрица), а не фикс-ролью;
# admin всегда имеет полный доступ. Чтение — campaigns_view (B35: раньше
# хватало логина), удаление и архив — campaigns_delete.
_can_view = require_permission("campaigns_view")
_can_create = require_permission("campaigns_create")
_can_edit = require_permission("campaigns_edit")
_can_delete = require_permission("campaigns_delete")


# Отказ триггера статусов (миграция 009): SQLSTATE 23514 с этим именем.
STATUS_GUARD = "trg_campaign_status_guard"

_HAS_HISTORY = "campaign has history — archive it instead"
_ENDED = "campaign has already ended (end_date has passed)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def status_guard_rejection(exc: DBAPIError) -> str | None:
    """Текст отказа ``trg_campaign_status_guard`` или None для любой другой
    ошибки БД (её не маскируем под 409)."""
    cause = getattr(exc.orig, "__cause__", None) or exc.orig
    if (getattr(cause, "sqlstate", None) == "23514"
            and getattr(cause, "constraint_name", None) == STATUS_GUARD):
        return getattr(cause, "message", None) or str(cause)
    return None


def _to_response(c: CashbackCampaign, mccs: list[str],
                 min_tx_amounts: dict[str, Any] | None = None) -> CampaignResponse:
    return CampaignResponse(
        campaign_id=c.campaign_id,
        name=c.name,
        target_segment_ids=list(c.target_segment_ids or []),
        cashback_rate=c.cashback_rate,
        min_transaction_amount=c.min_transaction_amount,
        budget_total=c.budget_total,
        budget_spent=c.budget_spent,
        status=str(c.status),
        start_date=c.start_date,
        end_date=c.end_date,
        allowed_channels=list(c.allowed_channels or []),
        require_existing_behavior=bool(c.require_existing_behavior),
        rate_tiers=c.rate_tiers,
        mcc_codes=mccs,
        daily_limit=c.daily_limit,
        auto_pause=bool(c.auto_pause),
        rfm_min=c.rfm_min,
        rfm_max=c.rfm_max,
        created_by=c.created_by,
        min_tx_amounts=min_tx_amounts or {},
        archived_at=c.archived_at,
    )


async def _load_with_mccs(session: AsyncSession, campaign_id: uuid.UUID
                          ) -> CampaignResponse:
    row = await session.get(CashbackCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    cat_rows = (
        await session.execute(
            select(CampaignCategory.mcc_code,
                   CampaignCategory.min_transaction_amount)
            .where(CampaignCategory.campaign_id == campaign_id)
        )
    ).all()
    mccs = [m.strip() for m, _ in cat_rows]
    min_tx = {m.strip(): amt for m, amt in cat_rows if amt is not None}
    return _to_response(row, mccs, min_tx)


# ---------------------------------------------------------------------------
# Create / read / status
# ---------------------------------------------------------------------------
@router.post("", response_model=CampaignResponse, status_code=201,
             dependencies=[Depends(_can_create)])
async def create_campaign(
    payload: CampaignCreate,
    current=Depends(get_current_user),
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
    created_by = None
    try:
        created_by = uuid.UUID(str(current.user_id))
    except (ValueError, TypeError, AttributeError):
        pass  # стаб/сервисный субъект без UUID — created_by остаётся NULL
    campaign = CashbackCampaign(
        campaign_id=uuid.uuid4(),
        name=payload.name,
        target_segment_ids=payload.target_segment_ids,
        cashback_rate=payload.cashback_rate,
        min_transaction_amount=payload.min_transaction_amount,
        budget_total=payload.budget_total,
        budget_spent=Decimal("0"),
        status="DRAFT",
        start_date=payload.start_date,
        end_date=payload.end_date,
        allowed_channels=payload.allowed_channels,
        require_existing_behavior=payload.require_existing_behavior,
        rate_tiers=payload.rate_tiers,
        daily_limit=payload.daily_limit,
        auto_pause=payload.auto_pause,
        rfm_min=payload.rfm_min,
        rfm_max=payload.rfm_max,
        created_by=created_by,
    )
    session.add(campaign)
    per_cat = payload.min_tx_amounts or {}
    for code in payload.mcc_codes:
        session.add(CampaignCategory(
            campaign_id=campaign.campaign_id,
            mcc_code=code,
            # Per-категорийная мин. сумма, иначе общий фолбэк (фаза 22).
            min_transaction_amount=per_cat.get(code, payload.min_transaction_amount),
        ))
    await session.commit()
    return _to_response(
        campaign, payload.mcc_codes,
        {c: per_cat.get(c, payload.min_transaction_amount)
         for c in payload.mcc_codes
         if per_cat.get(c, payload.min_transaction_amount) is not None},
    )


@router.get("", response_model=list[CampaignResponse],
            dependencies=[Depends(_can_view)])
async def list_campaigns(
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(DRAFT|ACTIVE|PAUSED|COMPLETED)$",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    include_archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session_dep),
) -> list[CampaignResponse]:
    """Full campaign list for the admin UI (any status, newest first);
    archived campaigns only with ``include_archived=true``."""
    stmt = (
        select(CashbackCampaign)
        .order_by(CashbackCampaign.start_date.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(CashbackCampaign.status == status_filter)
    if not include_archived:
        stmt = stmt.where(CashbackCampaign.archived_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()

    mcc_map: dict[uuid.UUID, list[str]] = {}
    ids = [c.campaign_id for c in rows]
    if ids:
        pairs = (
            await session.execute(
                select(CampaignCategory.campaign_id, CampaignCategory.mcc_code)
                .where(CampaignCategory.campaign_id.in_(ids))
            )
        ).all()
        for cid, code in pairs:
            mcc_map.setdefault(cid, []).append(code.strip())
    return [_to_response(c, mcc_map.get(c.campaign_id, [])) for c in rows]


@router.get("/active", response_model=list[CampaignSummary],
            dependencies=[Depends(_can_view)])
async def list_active_campaigns(
    include_archived: bool = Query(default=False),
    session: AsyncSession = Depends(get_session_dep),
) -> list[CampaignSummary]:
    now = datetime.now(UTC)
    stmt = select(CashbackCampaign).where(
        CashbackCampaign.status == "ACTIVE",
        CashbackCampaign.start_date <= now,
        CashbackCampaign.end_date >= now,
    ).order_by(CashbackCampaign.start_date.desc())
    if not include_archived:
        stmt = stmt.where(CashbackCampaign.archived_at.is_(None))
    rows = (await session.execute(stmt)).scalars().all()
    out: list[CampaignSummary] = []
    for c in rows:
        mccs = (
            await session.execute(
                select(CampaignCategory.mcc_code)
                .where(CampaignCategory.campaign_id == c.campaign_id)
            )
        ).scalars().all()
        out.append(CampaignSummary(
            campaign_id=c.campaign_id, name=c.name, status=str(c.status),
            budget_total=c.budget_total, budget_spent=c.budget_spent,
            cashback_rate=c.cashback_rate, start_date=c.start_date,
            end_date=c.end_date, mcc_codes=[m.strip() for m in mccs],
            archived_at=c.archived_at,
        ))
    return out


# ---------------------------------------------------------------------------
# Listing 3.10 — applicable campaigns for a user
# ---------------------------------------------------------------------------
# Кому положена кампания, решает только eligible_campaigns (миграция 009):
# статус и архив, окно дат, сегмент, ПОСЛЕДНЕЕ согласие, RFM, бюджет и
# дневной лимит. Здесь — лишь обход всех MCC кампаний (сумма покупки ещё не
# известна → NULL) и группировка строк по кампании; своих условий нет (B26).
APPLICABLE_SQL = text(
    """
    SELECT e.campaign_id::text AS campaign_id,
           e.name,
           e.cashback_rate,
           e.budget_total - e.budget_spent AS remaining_budget,
           array_agg(e.mcc_code ORDER BY e.mcc_code) AS mcc_codes
      FROM (SELECT DISTINCT mcc_code::text AS mcc_code
              FROM campaign_categories) m
     CROSS JOIN LATERAL eligible_campaigns(
                CAST(:user_id AS uuid), m.mcc_code, NULL::numeric, now()) e
     GROUP BY e.campaign_id, e.name, e.cashback_rate,
              e.budget_total, e.budget_spent, e.start_date
     ORDER BY e.cashback_rate DESC, e.start_date DESC, e.campaign_id
     LIMIT :limit
    """
)


@router.get("/applicable/{user_id}", response_model=list[ApplicableCampaign],
            dependencies=[Depends(_can_view)])
async def applicable_for_user(
    user_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session_dep),
) -> list[ApplicableCampaign]:
    rows = (
        await session.execute(APPLICABLE_SQL, {"user_id": user_id, "limit": limit})
    ).mappings().all()
    return [ApplicableCampaign(**r) for r in rows]


# ---------------------------------------------------------------------------
# Single-campaign reads / FSM
# ---------------------------------------------------------------------------
@router.get("/{campaign_id}", response_model=CampaignResponse,
            dependencies=[Depends(_can_view)])
async def get_campaign(
    campaign_id: uuid.UUID = Path(...),
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
    return await _load_with_mccs(session, campaign_id)


@router.patch("/{campaign_id}", response_model=CampaignResponse,
              dependencies=[Depends(_can_edit)])
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
    """Edit campaign fields. Only DRAFT campaigns are mutable — everything
    после активации меняется исключительно через FSM-переходы статуса,
    чтобы не ломать аудит и уже начисленный кэшбэк.

    Строка кампании читается под блокировкой (FOR UPDATE) до проверки
    статуса: параллельный activate либо ждёт конца правки, либо уже
    закоммичен — тогда здесь видно ACTIVE и ответ 409 (B38), а не правка
    ставки и категорий работающей кампании."""
    row = await session.get(CashbackCampaign, campaign_id,
                            with_for_update=True, populate_existing=True)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if str(row.status) != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail=f"only DRAFT campaigns are editable (status={row.status})",
        )

    data = payload.model_dump(exclude_unset=True)
    mcc_codes = data.pop("mcc_codes", None)
    per_cat = data.pop("min_tx_amounts", None)  # не колонка кампании
    for field, value in data.items():
        setattr(row, field, value)

    if _aware(row.end_date) <= _aware(row.start_date):
        raise HTTPException(
            status_code=422,
            detail="end_date must be strictly after start_date",
        )
    if row.daily_limit is not None and row.daily_limit > row.budget_total:
        raise HTTPException(
            status_code=422,
            detail="daily_limit must not exceed budget_total",
        )
    if mcc_codes is not None:
        per_cat = per_cat or {}
        await session.execute(
            delete(CampaignCategory)
            .where(CampaignCategory.campaign_id == campaign_id)
        )
        for code in mcc_codes:
            session.add(CampaignCategory(
                campaign_id=campaign_id,
                mcc_code=code,
                min_transaction_amount=per_cat.get(code, row.min_transaction_amount),
            ))
    await session.commit()
    return await _load_with_mccs(session, campaign_id)


@router.delete("/{campaign_id}", status_code=204, response_model=None,
               dependencies=[Depends(_can_delete)])
async def delete_campaign(
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session_dep),
):
    """Удалить кампанию (право ``campaigns_delete``) — только черновик без
    истории: ни рекомендаций, ни начислений (B24).

    Журнал начислений — долг перед клиентами, раньше он уходил вместе с
    кампанией по ON DELETE CASCADE. Кампанию с историей (и любую, кроме
    DRAFT) не удаляют, а архивируют — ``POST /campaigns/{id}/archive``.
    Статус и история проверяются под блокировкой строки кампании; FK
    ON DELETE RESTRICT (миграция 009) — страховка, если история появилась
    в обход этой проверки."""
    status_ = (
        await session.execute(
            select(CashbackCampaign.status)
            .where(CashbackCampaign.campaign_id == campaign_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if status_ is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    has_history = (
        await session.execute(select(or_(
            exists().where(Recommendation.campaign_id == campaign_id),
            exists().where(CashbackAccrual.campaign_id == campaign_id),
        )))
    ).scalar_one()
    if str(status_) != "DRAFT" or has_history:
        await session.rollback()
        raise HTTPException(status_code=409, detail=_HAS_HISTORY)
    try:
        await session.execute(
            delete(CashbackCampaign).where(
                CashbackCampaign.campaign_id == campaign_id)
        )
        await session.commit()
    except IntegrityError as exc:       # FK RESTRICT: история всё-таки есть
        await session.rollback()
        raise HTTPException(status_code=409, detail=_HAS_HISTORY) from exc
    log.info("campaign_deleted", campaign_id=str(campaign_id))
    return None


@router.post("/{campaign_id}/archive", response_model=CampaignResponse,
             dependencies=[Depends(_can_delete)])
async def archive_campaign(
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
    """Отправить в архив PAUSED или COMPLETED кампанию (B24).

    История остаётся; архивная кампания скрыта из списков (показывает её
    ``include_archived=true``), не положена никому (``eligible_campaigns``)
    и статус больше не меняет. Повторный вызов
    ничего не меняет. ACTIVE → 409 (сначала пауза или завершение), DRAFT →
    409 (черновик удаляют). Условие на статус — в самом UPDATE: гонка с
    активацией не оставит активную кампанию в архиве."""
    archived = (
        await session.execute(
            update(CashbackCampaign)
            .where(CashbackCampaign.campaign_id == campaign_id,
                   CashbackCampaign.status.in_(("PAUSED", "COMPLETED")))
            .values(archived_at=func.coalesce(CashbackCampaign.archived_at,
                                              func.now()))
            .returning(CashbackCampaign.campaign_id)
            .execution_options(synchronize_session=False)
        )
    ).first()
    if archived is None:
        await session.rollback()
        row = await session.get(CashbackCampaign, campaign_id)
        if row is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        if str(row.status) == "DRAFT":
            raise HTTPException(
                status_code=409,
                detail="a DRAFT campaign is not archived — delete it instead",
            )
        raise HTTPException(status_code=409,
                            detail="pause or complete the campaign first")
    await session.commit()
    log.info("campaign_archived", campaign_id=str(campaign_id))
    return await _load_with_mccs(session, campaign_id)


@router.patch("/{campaign_id}/status", response_model=StatusActionResponse,
              dependencies=[Depends(_can_edit)])
async def patch_status(
    campaign_id: uuid.UUID,
    action: str = Query(..., pattern="^(activate|pause|complete)$"),
    session: AsyncSession = Depends(get_session_dep),
) -> StatusActionResponse:
    """Сменить статус по FSM — compare-and-set (B38).

    UPDATE пишет, только если статус всё ещё тот, что прочитан (и кампания
    не в архиве): иначе 0 строк → 409, а не «оживление» кампании, которую
    тем временем завершил планировщик. Триггер ``trg_campaign_status_guard``
    — последняя линия обороны, его отказ — тоже 409.

    ``activate`` после ``end_date`` → 409 (B25); до ``start_date`` можно:
    платить кампания начнёт со ``start_date`` (``starts_in_future``)."""
    row = await session.get(CashbackCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    previous = str(row.status)
    try:
        new_status = transition(previous, action)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if row.archived_at is not None:
        raise HTTPException(status_code=409, detail="campaign is archived")
    if new_status == "ACTIVE" and _aware(row.end_date) <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail=_ENDED)

    stmt = (
        update(CashbackCampaign)
        .where(CashbackCampaign.campaign_id == campaign_id,
               CashbackCampaign.status == previous,
               CashbackCampaign.archived_at.is_(None))
        .values(status=new_status)
        .returning((CashbackCampaign.start_date > func.now())
                   .label("starts_in_future"))
        .execution_options(synchronize_session=False)
    )
    if new_status == "ACTIVE":
        stmt = stmt.where(CashbackCampaign.end_date > func.now())
    try:
        changed = (await session.execute(stmt)).first()
    except DBAPIError as exc:
        reason = status_guard_rejection(exc)
        if reason is None:
            raise
        await session.rollback()
        raise HTTPException(status_code=409, detail=reason) from exc
    if changed is None:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"campaign changed concurrently (expected status {previous}) "
                   "— reload and retry",
        )
    await session.commit()
    return StatusActionResponse(
        campaign_id=campaign_id,
        previous=previous, current=new_status, action=action,
        starts_in_future=new_status == "ACTIVE" and bool(changed.starts_in_future),
    )


# ---------------------------------------------------------------------------
# Budget — read-only (B23)
# ---------------------------------------------------------------------------
# Расход «за сегодня» — с полуночи UTC, ровно как spent_today в
# eligible_campaigns (миграция 009).
_BUDGET_SQL = text(
    """
    SELECT c.budget_total,
           c.budget_spent,
           c.daily_limit,
           (SELECT COALESCE(sum(a.cashback_amount), 0)
              FROM cashback_accruals a
             WHERE a.campaign_id = c.campaign_id
               AND a.accrued_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
                                   AT TIME ZONE 'UTC') AS spent_today
      FROM cashback_campaigns c
     WHERE c.campaign_id = :campaign_id
    """
)


@router.get("/{campaign_id}/budget", response_model=CampaignBudget,
            dependencies=[Depends(_can_view)])
async def campaign_budget(
    campaign_id: uuid.UUID,
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignBudget:
    """Бюджет кампании и её расход за текущие UTC-сутки — только чтение.

    Списывает бюджет только транзакция начисления (transaction_listener):
    прежний ``POST …/budget/check`` «резервировал» сумму навсегда (B23)."""
    row = (
        await session.execute(_BUDGET_SQL, {"campaign_id": campaign_id})
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    total, spent = Decimal(row.budget_total), Decimal(row.budget_spent or 0)
    spent_today = Decimal(row.spent_today or 0)
    daily = Decimal(row.daily_limit) if row.daily_limit is not None else None
    return CampaignBudget(
        campaign_id=campaign_id,
        budget_total=total, budget_spent=spent, remaining=total - spent,
        daily_limit=daily, spent_today=spent_today,
        remaining_today=(max(daily - spent_today, Decimal("0"))
                         if daily is not None else None),
    )


# ---------------------------------------------------------------------------
# Campaign stats — derived from PG + ClickHouse
# ---------------------------------------------------------------------------
@router.get("/{campaign_id}/stats", response_model=CampaignStats,
            dependencies=[Depends(_can_view)])
async def campaign_stats(
    campaign_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignStats:
    # Recommendations — impressions, clicks (= non-PENDING), accepted.
    impressions = (
        await session.execute(
            select(func.count()).select_from(Recommendation)
            .where(Recommendation.campaign_id == campaign_id)
        )
    ).scalar_one() or 0
    clicks = (
        await session.execute(
            select(func.count()).select_from(Recommendation)
            .where(Recommendation.campaign_id == campaign_id,
                   Recommendation.response_status != "PENDING")
        )
    ).scalar_one() or 0
    accepted = (
        await session.execute(
            select(func.count()).select_from(Recommendation)
            .where(Recommendation.campaign_id == campaign_id,
                   Recommendation.response_status == "ACCEPTED")
        )
    ).scalar_one() or 0

    # Accruals — transactions, cashback paid.
    accrual = (
        await session.execute(
            select(
                func.count(CashbackAccrual.accrual_id),
                func.coalesce(func.sum(CashbackAccrual.transaction_amount), 0),
                func.coalesce(func.sum(CashbackAccrual.cashback_amount), 0),
            ).where(CashbackAccrual.campaign_id == campaign_id)
        )
    ).one()
    transactions, revenue, cashback_paid = (
        int(accrual[0]), Decimal(accrual[1] or 0), Decimal(accrual[2] or 0)
    )

    ctr = float(clicks) / impressions if impressions else 0.0
    conv = float(accepted) / impressions if impressions else 0.0
    roi = float((revenue - cashback_paid) / cashback_paid) if cashback_paid > 0 else 0.0

    return CampaignStats(
        campaign_id=campaign_id,
        impressions=impressions, clicks=clicks, accepted=accepted,
        transactions=transactions, revenue=revenue,
        cashback_paid=cashback_paid,
        ctr=round(ctr, 6), conversion_rate=round(conv, 6), roi=round(roi, 4),
    )


# ---------------------------------------------------------------------------
# Audience estimate
# ---------------------------------------------------------------------------
@router.get("/audience/estimate", response_model=AudienceEstimateResponse,
            dependencies=[Depends(_can_view)])
async def audience_estimate(
    segment_ids: list[int] = Query(..., min_length=1),
    rfmR: int | None = Query(default=None, description="recency_days <= rfmR"),
    rfmF: int | None = Query(default=None, description="frequency_total >= rfmF"),
    rfmM: float | None = Query(default=None, description="monetary_total >= rfmM"),
    session: AsyncSession = Depends(get_session_dep),
) -> AudienceEstimateResponse:
    base = (
        await session.execute(
            select(func.count(User.user_id))
            .where(User.segment_id.in_(segment_ids))
        )
    ).scalar_one() or 0
    estimated = int(base)
    if rfmR is not None or rfmF is not None or rfmM is not None:
        # Heuristic shrink — assume each filter retains ~70% of users.
        keep = 1.0
        for v in (rfmR, rfmF, rfmM):
            if v is not None:
                keep *= 0.7
        estimated = int(base * keep)
    return AudienceEstimateResponse(
        estimated_users=estimated, segments=segment_ids,
        rfm_filters={"rfmR": rfmR, "rfmF": rfmF, "rfmM": rfmM},
    )
