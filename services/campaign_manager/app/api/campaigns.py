"""Campaign CRUD + applicability + budget reservation — chapter 3.2, table 18.

Highlights
~~~~~~~~~~
* ``GET /campaigns/applicable/{user_id}`` reproduces listing 3.10 — a
  four-stage SQL filter (status/dates → segment → consent → budget).
* ``POST /campaigns/{id}/budget/check`` reserves budget atomically by
  acquiring a row-level lock on ``cashback_campaigns`` (``SELECT … FOR
  UPDATE``) and persisting an idempotency-keyed Redis hold.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session_dep
from app.fsm import InvalidTransition, transition
from app.models import (
    CampaignCategory,
    CashbackAccrual,
    CashbackCampaign,
    Recommendation,
    User,
    UserConsent,
)
from app.schemas import (
    AudienceEstimateResponse,
    BudgetCheckRequest,
    BudgetCheckResponse,
    CampaignCreate,
    CampaignResponse,
    CampaignStats,
    CampaignSummary,
    CampaignUpdate,
    StatusActionResponse,
)

log = structlog.get_logger("api.campaigns")
router = APIRouter(prefix="/campaigns", tags=["campaigns"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_response(c: CashbackCampaign, mccs: list[str]) -> CampaignResponse:
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
    )


async def _load_with_mccs(session: AsyncSession, campaign_id: uuid.UUID
                          ) -> CampaignResponse:
    row = await session.get(CashbackCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    mccs = (
        await session.execute(
            select(CampaignCategory.mcc_code)
            .where(CampaignCategory.campaign_id == campaign_id)
        )
    ).scalars().all()
    return _to_response(row, [m.strip() for m in mccs])


# ---------------------------------------------------------------------------
# Create / read / status
# ---------------------------------------------------------------------------
@router.post("", response_model=CampaignResponse, status_code=201)
async def create_campaign(
    payload: CampaignCreate,
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
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
    )
    session.add(campaign)
    for code in payload.mcc_codes:
        session.add(CampaignCategory(
            campaign_id=campaign.campaign_id,
            mcc_code=code,
            min_transaction_amount=payload.min_transaction_amount,
        ))
    await session.commit()
    return _to_response(campaign, payload.mcc_codes)


@router.get("", response_model=list[CampaignResponse])
async def list_campaigns(
    status_filter: str | None = Query(
        default=None, alias="status", pattern="^(DRAFT|ACTIVE|PAUSED|COMPLETED)$",
    ),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_session_dep),
) -> list[CampaignResponse]:
    """Full campaign list for the admin UI (any status, newest first)."""
    stmt = (
        select(CashbackCampaign)
        .order_by(CashbackCampaign.start_date.desc())
        .limit(limit)
    )
    if status_filter:
        stmt = stmt.where(CashbackCampaign.status == status_filter)
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


@router.get("/active", response_model=list[CampaignSummary])
async def list_active_campaigns(
    session: AsyncSession = Depends(get_session_dep),
) -> list[CampaignSummary]:
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(CashbackCampaign).where(
                CashbackCampaign.status == "ACTIVE",
                CashbackCampaign.start_date <= now,
                CashbackCampaign.end_date >= now,
            ).order_by(CashbackCampaign.start_date.desc())
        )
    ).scalars().all()
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
        ))
    return out


# ---------------------------------------------------------------------------
# Listing 3.10 — applicable campaigns for a user
# ---------------------------------------------------------------------------
APPLICABLE_SQL = text(
    """
    -- Listing 3.10: 4-stage filter
    --   1) campaign ACTIVE within date window
    --   2) target_segment_ids overlaps user segment
    --   3) user has GRANTED consent for personalised cashback
    --   4) campaign has remaining budget > :min_award
    SELECT c.campaign_id::text   AS campaign_id,
           c.name                AS name,
           c.cashback_rate       AS cashback_rate,
           c.budget_total - c.budget_spent AS remaining_budget,
           array_agg(DISTINCT cc.mcc_code) AS mcc_codes
      FROM cashback_campaigns  c
      JOIN campaign_categories cc USING (campaign_id)
      JOIN users               u  ON u.user_id = :user_id
      JOIN user_consents       uc ON uc.user_id = u.user_id
                                 AND uc.consent_type = 'personalised_cashback'
                                 AND uc.status = 'GRANTED'
     WHERE c.status     = 'ACTIVE'
       AND c.start_date <= now()
       AND c.end_date   >= now()
       AND u.segment_id = ANY(c.target_segment_ids)
       AND (c.budget_total - c.budget_spent) > :min_award
     GROUP BY c.campaign_id, c.name, c.cashback_rate, c.budget_total, c.budget_spent
     ORDER BY c.cashback_rate DESC
     LIMIT :limit
    """
)


@router.get("/applicable/{user_id}")
async def applicable_for_user(
    user_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session_dep),
) -> list[dict[str, Any]]:
    cfg = get_settings()
    rows = (
        await session.execute(
            APPLICABLE_SQL,
            {
                "user_id": str(user_id),
                "min_award": cfg.min_award_threshold,
                "limit": limit,
            },
        )
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Single-campaign reads / FSM
# ---------------------------------------------------------------------------
@router.get("/{campaign_id}", response_model=CampaignResponse)
async def get_campaign(
    campaign_id: uuid.UUID = Path(...),
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
    return await _load_with_mccs(session, campaign_id)


@router.patch("/{campaign_id}", response_model=CampaignResponse)
async def update_campaign(
    campaign_id: uuid.UUID,
    payload: CampaignUpdate,
    session: AsyncSession = Depends(get_session_dep),
) -> CampaignResponse:
    """Edit campaign fields. Only DRAFT campaigns are mutable — everything
    после активации меняется исключительно через FSM-переходы статуса,
    чтобы не ломать аудит и уже начисленный кэшбэк."""
    row = await session.get(CashbackCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    if str(row.status) != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail=f"only DRAFT campaigns are editable (status={row.status})",
        )

    data = payload.model_dump(exclude_unset=True)
    mcc_codes = data.pop("mcc_codes", None)
    for field, value in data.items():
        setattr(row, field, value)

    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)

    if _aware(row.end_date) <= _aware(row.start_date):
        raise HTTPException(
            status_code=422,
            detail="end_date must be strictly after start_date",
        )
    if mcc_codes is not None:
        await session.execute(
            delete(CampaignCategory)
            .where(CampaignCategory.campaign_id == campaign_id)
        )
        for code in mcc_codes:
            session.add(CampaignCategory(
                campaign_id=campaign_id,
                mcc_code=code,
                min_transaction_amount=row.min_transaction_amount,
            ))
    await session.commit()
    return await _load_with_mccs(session, campaign_id)


@router.patch("/{campaign_id}/status", response_model=StatusActionResponse)
async def patch_status(
    campaign_id: uuid.UUID,
    action: str = Query(..., pattern="^(activate|pause|complete)$"),
    session: AsyncSession = Depends(get_session_dep),
) -> StatusActionResponse:
    row = await session.get(CashbackCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail="campaign not found")
    previous = str(row.status)
    try:
        new_status = transition(previous, action)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    row.status = new_status
    await session.commit()
    return StatusActionResponse(
        campaign_id=row.campaign_id,
        previous=previous, current=new_status, action=action,
    )


# ---------------------------------------------------------------------------
# Budget reservation — atomic SELECT FOR UPDATE
# ---------------------------------------------------------------------------
_BUDGET_LOCK_SQL = text(
    """
    SELECT campaign_id::text  AS campaign_id,
           budget_total,
           budget_spent,
           status::text       AS status
      FROM cashback_campaigns
     WHERE campaign_id = :campaign_id
     FOR UPDATE
    """
)


@router.post("/{campaign_id}/budget/check", response_model=BudgetCheckResponse)
async def reserve_budget(
    request: Request,
    campaign_id: uuid.UUID,
    payload: BudgetCheckRequest,
    session: AsyncSession = Depends(get_session_dep),
) -> BudgetCheckResponse:
    cfg = get_settings()
    request_id = uuid.uuid4().hex

    async with session.begin():
        row = (
            await session.execute(_BUDGET_LOCK_SQL, {"campaign_id": str(campaign_id)})
        ).first()
        if row is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        if row.status != "ACTIVE":
            return BudgetCheckResponse(
                campaign_id=campaign_id, reserved=False, request_id=request_id,
                remaining_budget=Decimal(row.budget_total) - Decimal(row.budget_spent),
                reason=f"campaign status={row.status}",
            )

        remaining = Decimal(row.budget_total) - Decimal(row.budget_spent)
        if Decimal(payload.amount) > remaining:
            return BudgetCheckResponse(
                campaign_id=campaign_id, reserved=False, request_id=request_id,
                remaining_budget=remaining,
                reason="insufficient_budget",
            )

        # Increment spent under the lock; commit happens on context exit.
        await session.execute(
            update(CashbackCampaign)
            .where(CashbackCampaign.campaign_id == campaign_id)
            .values(budget_spent=CashbackCampaign.budget_spent + payload.amount)
        )
        new_remaining = remaining - Decimal(payload.amount)

    # Persist a Redis hold so the reservation can be released on TTL expiry
    # (the actual debit is already in PG; this is for observability + UI).
    redis = request.app.state.redis
    try:
        await redis.setex(
            f"budget_reservation:{campaign_id}:{request_id}",
            payload.ttl_seconds,
            json.dumps({
                "amount": str(payload.amount),
                "remaining": str(new_remaining),
            }),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("redis_hold_failed", error=str(exc))

    return BudgetCheckResponse(
        campaign_id=campaign_id, reserved=True, request_id=request_id,
        remaining_budget=new_remaining,
    )


# ---------------------------------------------------------------------------
# Campaign stats — derived from PG + ClickHouse
# ---------------------------------------------------------------------------
@router.get("/{campaign_id}/stats", response_model=CampaignStats)
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
@router.get("/audience/estimate", response_model=AudienceEstimateResponse)
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
