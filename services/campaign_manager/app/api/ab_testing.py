"""A/B testing endpoints — chapter 3.2, table 20.

CRUD for experiments + variants, deterministic user assignment via
hash bucketing, and a two-proportion z-test for results.

Этап D (B34): контроль — вариант с ``is_control`` (миграция 010), а не
первый по алфавиту; успех — назначение хотя бы с одной конверсией
(``COUNT(DISTINCT assignment_id)``), а не число событий; вердикт несёт
направление (``winner``).
"""
from __future__ import annotations

import hashlib
import math
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from scipy.stats import norm
from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session_dep
from app.models import (
    ABAssignment,
    ABEvent,
    ABExperiment,
    ABVariant,
)
from app.rbac import require_permission
from app.schemas import (
    ABAssignmentResponse,
    ABExperimentCreate,
    ABExperimentResponse,
    ABResults,
    ABVariantResponse,
    ABVariantStats,
)
from app.security import get_current_user

router = APIRouter(
    prefix="/experiments", tags=["ab-testing"],
    dependencies=[Depends(get_current_user)],
)

# B35: чтение экспериментов — право analytics, изменения (создание, статус,
# назначение пользователя) — campaigns_edit; раньше assign был доступен
# любому залогиненному.
_can_view = require_permission("analytics")
_can_mutate = require_permission("campaigns_edit")

# UNIQUE (name) из 001 — имя Postgres по умолчанию.
_NAME_UNIQUE = "ab_experiments_name_key"


def _unique_violation_of(exc: IntegrityError, constraint: str) -> bool:
    cause = getattr(exc.orig, "__cause__", None) or exc.orig
    return (getattr(cause, "sqlstate", None) == "23505"
            and getattr(cause, "constraint_name", None) == constraint)


# ---------------------------------------------------------------------------
# Hashing — deterministic, uniform bucketing of (user, experiment).
# ---------------------------------------------------------------------------
def deterministic_bucket(user_id: str, experiment_id: str) -> float:
    """Return a stable [0, 1) bucket for ``(user_id, experiment_id)``."""
    digest = hashlib.md5(f"{experiment_id}:{user_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 0x100000000


def pick_variant(bucket: float, variants: list[ABVariant]) -> ABVariant:
    """Walk the cumulative weight axis and return the matching variant."""
    cumulative = 0.0
    for v in variants:
        cumulative += float(v.traffic_weight)
        if bucket < cumulative:
            return v
    return variants[-1]


# ---------------------------------------------------------------------------
# Two-proportion z-test
# ---------------------------------------------------------------------------
def two_proportion_z_test(
    successes_a: int, n_a: int,
    successes_b: int, n_b: int,
    alpha: float = 0.05,
) -> dict[str, float | tuple[float, float]] | None:
    """z-тест двух долей; None — теста нет: пустая группа, доля вне [0, 1]
    (B34: число событий больше числа назначений → sqrt отрицательного →
    500) или нулевая дисперсия."""
    if n_a <= 0 or n_b <= 0:
        return None
    if not (0 <= successes_a <= n_a and 0 <= successes_b <= n_b):
        return None
    p_a = successes_a / n_a
    p_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)
    var_pool = p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)
    if var_pool <= 0:
        return None
    z = (p_b - p_a) / math.sqrt(var_pool)
    # sf(|z|) ∈ [0, 0.5] → p ∈ [0, 1] по построению
    p_value = 2.0 * float(norm.sf(abs(z)))
    se_diff = math.sqrt(
        p_a * (1 - p_a) / n_a + p_b * (1 - p_b) / n_b
    )
    z_alpha = norm.ppf(1 - alpha / 2)
    diff = p_b - p_a
    ci = (diff - z_alpha * se_diff, diff + z_alpha * se_diff)
    return {"z": z, "p_value": p_value, "diff": diff, "ci": ci}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.post("", response_model=ABExperimentResponse, status_code=201,
             dependencies=[Depends(_can_mutate)])
async def create_experiment(
    payload: ABExperimentCreate,
    session: AsyncSession = Depends(get_session_dep),
) -> ABExperimentResponse:
    """Контроль — вариант с ``is_control`` (по умолчанию первый в запросе;
    ровно один — иначе 422); имя эксперимента занято → 409."""
    exp = ABExperiment(
        experiment_id=uuid.uuid4(),
        name=payload.name,
        status="DRAFT",
        target_metric=payload.target_metric,
        start_date=payload.start_date,
        end_date=payload.end_date,
        variants=[v.model_dump() for v in payload.variants],
    )
    session.add(exp)
    variants_orm: list[ABVariant] = []
    for v in payload.variants:
        orm = ABVariant(
            variant_id=uuid.uuid4(),
            experiment_id=exp.experiment_id,
            name=v.name,
            traffic_weight=v.traffic_weight,
            strategy_class=v.strategy_class,
            strategy_params=v.strategy_params,
            is_control=bool(v.is_control),
        )
        session.add(orm)
        variants_orm.append(orm)
    try:
        await session.commit()
    except IntegrityError as exc:       # B34: дубль имени был 500
        await session.rollback()
        if _unique_violation_of(exc, _NAME_UNIQUE):
            raise HTTPException(status_code=409,
                                detail="experiment name already exists") from exc
        raise
    return ABExperimentResponse(
        experiment_id=exp.experiment_id, name=exp.name, status=str(exp.status),
        target_metric=exp.target_metric, start_date=exp.start_date,
        end_date=exp.end_date,
        variants=[ABVariantResponse.model_validate(v) for v in variants_orm],
    )


@router.get("", response_model=list[ABExperimentResponse],
            dependencies=[Depends(_can_view)])
async def list_experiments(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session_dep),
) -> list[ABExperimentResponse]:
    """Список экспериментов для админ-панели (новые сверху) — фаза 16.

    Раньше эндпоинта не было: фронтовый ``abApi.list()`` получал 405."""
    exps = (
        await session.execute(
            select(ABExperiment)
            .order_by(ABExperiment.start_date.desc())
            .limit(limit).offset(offset)
        )
    ).scalars().all()
    if not exps:
        return []
    variants = (
        await session.execute(
            select(ABVariant).where(
                ABVariant.experiment_id.in_([e.experiment_id for e in exps])
            ).order_by(ABVariant.is_control.desc(), ABVariant.name)
        )
    ).scalars().all()
    by_exp: dict[uuid.UUID, list[ABVariant]] = {}
    for v in variants:
        by_exp.setdefault(v.experiment_id, []).append(v)
    return [
        ABExperimentResponse(
            experiment_id=e.experiment_id, name=e.name, status=str(e.status),
            target_metric=e.target_metric, start_date=e.start_date,
            end_date=e.end_date,
            variants=[
                ABVariantResponse.model_validate(v)
                for v in by_exp.get(e.experiment_id, [])
            ],
        )
        for e in exps
    ]


@router.get("/{experiment_id}", response_model=ABExperimentResponse,
            dependencies=[Depends(_can_view)])
async def get_experiment(
    experiment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session_dep),
) -> ABExperimentResponse:
    exp = await session.get(ABExperiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    variants = (
        await session.execute(
            select(ABVariant).where(ABVariant.experiment_id == experiment_id)
            .order_by(ABVariant.is_control.desc(), ABVariant.name)
        )
    ).scalars().all()
    return ABExperimentResponse(
        experiment_id=exp.experiment_id, name=exp.name, status=str(exp.status),
        target_metric=exp.target_metric, start_date=exp.start_date,
        end_date=exp.end_date,
        variants=[ABVariantResponse.model_validate(v) for v in variants],
    )


_FSM_AB = {
    ("DRAFT", "start"):    "ACTIVE",
    ("ACTIVE", "stop"):    "STOPPED",
}


@router.patch("/{experiment_id}/status", response_model=ABExperimentResponse,
              dependencies=[Depends(_can_mutate)])
async def patch_experiment_status(
    experiment_id: uuid.UUID,
    action: str = Query(..., pattern="^(start|stop)$"),
    session: AsyncSession = Depends(get_session_dep),
) -> ABExperimentResponse:
    exp = await session.get(ABExperiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    key = (str(exp.status), action)
    if key not in _FSM_AB:
        raise HTTPException(
            status_code=409,
            detail=f"cannot {action} experiment in state {exp.status!r}",
        )
    exp.status = _FSM_AB[key]
    await session.commit()
    return await get_experiment(experiment_id, session)


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
@router.post("/{experiment_id}/assign/{user_id}",
             response_model=ABAssignmentResponse,
             dependencies=[Depends(_can_mutate)])
async def assign_user(
    experiment_id: uuid.UUID,
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session_dep),
) -> ABAssignmentResponse:
    """Назначение только в ACTIVE-эксперимент (иначе 409) и идемпотентно:
    ``INSERT … ON CONFLICT DO NOTHING`` и чтение того, что в таблице, —
    повтор и гонка двух запросов возвращают одно назначение, а не 500 (B34)."""
    exp = await session.get(ABExperiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    if str(exp.status) != "ACTIVE":
        raise HTTPException(
            status_code=409,
            detail=f"experiment is not active (status {exp.status!r})",
        )

    variants = (
        await session.execute(
            select(ABVariant)
            .where(ABVariant.experiment_id == experiment_id)
            .order_by(ABVariant.name)
        )
    ).scalars().all()
    if not variants:
        raise HTTPException(status_code=409, detail="experiment has no variants")

    bucket = deterministic_bucket(str(user_id), str(experiment_id))
    chosen = pick_variant(bucket, list(variants))

    await session.execute(
        pg_insert(ABAssignment)
        .values(assignment_id=uuid.uuid4(), user_id=user_id,
                experiment_id=experiment_id, variant_id=chosen.variant_id,
                assigned_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["user_id", "experiment_id"])
    )
    assignment, variant_name = (
        await session.execute(
            select(ABAssignment, ABVariant.name)
            .join(ABVariant, ABVariant.variant_id == ABAssignment.variant_id)
            .where(ABAssignment.user_id == user_id,
                   ABAssignment.experiment_id == experiment_id)
        )
    ).one()
    await session.commit()
    return ABAssignmentResponse(
        user_id=assignment.user_id, experiment_id=assignment.experiment_id,
        variant_id=assignment.variant_id, variant_name=variant_name,
        assigned_at=assignment.assigned_at,
    )


# ---------------------------------------------------------------------------
# Results — z-test + significance badge
# ---------------------------------------------------------------------------
def _significance_badge(
    p_value: float | None, n_total: int,
    *, alpha_sig: float, alpha_trend: float, min_n: int,
) -> str:
    if p_value is None or n_total < min_n:
        return "no_data"
    if p_value < alpha_sig:
        return "significant"
    if p_value < alpha_trend:
        return "trending"
    return "no_data"


def _winner(significance: str, diff: float) -> str | None:
    """Чей rate выше — treatment или control; None без вердикта или без
    разницы. ``significant`` + ``control`` — тест значимо хуже (B34)."""
    if significance == "no_data" or diff == 0:
        return None
    return "treatment" if diff > 0 else "control"


async def _variant_stats(session: AsyncSession,
                         variants: list[ABVariant]) -> dict[uuid.UUID, tuple[int, int]]:
    """variant_id → (назначений, назначений с конверсией) — одним запросом,
    без загрузки строк; дубль CONVERSION успех не удваивает."""
    rows = (
        await session.execute(
            select(ABAssignment.variant_id,
                   func.count(func.distinct(ABAssignment.assignment_id)),
                   func.count(func.distinct(ABEvent.assignment_id)))
            .select_from(ABAssignment)
            .outerjoin(ABEvent, and_(ABEvent.assignment_id == ABAssignment.assignment_id,
                                     ABEvent.event_type == "CONVERSION"))
            .where(ABAssignment.variant_id.in_([v.variant_id for v in variants]))
            .group_by(ABAssignment.variant_id)
        )
    ).all()
    return {vid: (int(n), int(s)) for vid, n, s in rows}


@router.get("/{experiment_id}/results", response_model=ABResults,
            dependencies=[Depends(_can_view)])
async def experiment_results(
    experiment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session_dep),
) -> ABResults:
    cfg = get_settings()
    exp = await session.get(ABExperiment, experiment_id)
    if exp is None:
        raise HTTPException(status_code=404, detail="experiment not found")

    variants = (
        await session.execute(
            select(ABVariant)
            .where(ABVariant.experiment_id == experiment_id)
            .order_by(ABVariant.is_control.desc(), ABVariant.name)
        )
    ).scalars().all()
    if len(variants) < 2:
        raise HTTPException(status_code=409, detail="need ≥2 variants for results")
    if not variants[0].is_control:
        raise HTTPException(status_code=409, detail="experiment has no control variant")

    # Контроль — is_control; тест — первый из остальных по имени (ответ
    # сравнивает одну пару).
    control, treatment = variants[0], variants[1]
    counts = await _variant_stats(session, [control, treatment])
    stats: dict[uuid.UUID, ABVariantStats] = {}
    total_n = 0
    for v in (control, treatment):
        n_count, success_count = counts.get(v.variant_id, (0, 0))
        rate = (success_count / n_count) if n_count else 0.0
        stats[v.variant_id] = ABVariantStats(
            variant_id=v.variant_id, name=v.name,
            n=n_count, successes=success_count, rate=round(rate, 6),
        )
        total_n += n_count

    z_res = two_proportion_z_test(
        stats[control.variant_id].successes,   stats[control.variant_id].n,
        stats[treatment.variant_id].successes, stats[treatment.variant_id].n,
        alpha=cfg.ab_significance_alpha,
    )
    if z_res is None:
        diff = stats[treatment.variant_id].rate - stats[control.variant_id].rate
        z = p = ci = None
    else:
        diff = float(z_res["diff"])
        z = float(z_res["z"])
        p = float(z_res["p_value"])
        ci = (round(z_res["ci"][0], 6), round(z_res["ci"][1], 6))

    badge = _significance_badge(
        p, total_n,
        alpha_sig=cfg.ab_significance_alpha,
        alpha_trend=cfg.ab_trending_alpha,
        min_n=cfg.ab_min_observations,
    )

    return ABResults(
        experiment_id=experiment_id, target_metric=exp.target_metric,
        control=stats[control.variant_id], treatment=stats[treatment.variant_id],
        diff=round(diff, 6),
        z=z, p_value=p, confidence_interval=ci, significance=badge,
        winner=_winner(badge, diff),
    )
