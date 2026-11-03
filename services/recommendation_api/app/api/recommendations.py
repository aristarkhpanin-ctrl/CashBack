"""Recommendation endpoints — chapter 3.1, listing 3.9.

Three-stage pipeline:

    1. Retrieve ~20 candidates via the ALS index (CandidateGenerator).
    2. Rank them with the LGBM ranker, attaching SHAP top-5 factors.
    3. Filter through the BRE; keep only candidates that PASS.

Два маршрута над одним конвейером (B27):

* ``GET /recommendations/{user_id}`` — боевая выдача (вызывает только
  mobile_api): BRE с побочными эффектами (R3/R4 помечают показ в Redis),
  запись в ``recommendations`` и событие ``recommendations.created``
  (→ push/email клиенту). Строку оффера пишет только этот маршрут — одну
  на каждый оффер ответа (B29); её ``recommendation_id`` и ``expires_at``
  возвращаются в ответе, mobile_api их переиспользует. Оффер, строку
  которого записать не удалось, клиенту не отдаётся, и его отметки R3/R4
  снимаются.
* ``GET /recommendations/{user_id}/explain`` — тот же ответ для страницы
  «ML-объяснения» (вызывает только campaign_manager), строго read-only:
  без записи, без Kafka, BRE — ``EXPLAIN_RULES`` (см. ниже).

Кампанию кандидату оба маршрута берут одинаково — из SQL-функции
``eligible_campaigns`` (миграция 009, stage C): кандидат без положенной
клиенту кампании отбрасывается до BRE.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.bre.engine import BusinessRulesEngine, RuleFn
from app.bre.models import RuleContext, RuleOutcome
from app.bre.rules import (
    budget_reservation,
    category_exclusion,
    channel_applicability,
    min_transaction,
    ml_rate_cap,
)
from app.security import require_campaign_manager, require_mobile_api

log = structlog.get_logger("api.recommendations")

# Service-to-service auth (B27): у каждого маршрута свой список издателей
# service-токена (app/security.py); маршрута без проверки здесь нет —
# это проверяет tests/unit/test_routes_auth.py.
router = APIRouter(prefix="/recommendations", tags=["recommendations"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class RecommendationItem(BaseModel):
    mcc_code: str = Field(..., examples=["5411"])
    score: float
    campaign_id: str | None = None
    # B29: строка ``recommendations`` этого оффера — её id клиент передаёт в
    # respond (mobile_api), по ней начисление находит принятый оффер. Есть
    # только у боевой выдачи; explain ничего не пишет — там null.
    recommendation_id: str | None = None
    # Срок оффера из той же строки: LEAST(конец кампании, now() + OFFER_TTL_DAYS).
    expires_at: datetime | None = None
    top_factors: dict[str, float] = Field(default_factory=dict)
    # Фаза 18: сырые значения тех же топ-5 признаков — чтобы waterfall
    # в UI показывал не только вклад, но и «12 транзакций», «₽42 800».
    feature_values: dict[str, float] = Field(default_factory=dict)


class RecommendationResponse(BaseModel):
    user_id: str
    recommendations: list[RecommendationItem]
    model_version: str | None = None
    candidates_considered: int = 0
    # Holdout-сплит (beyond-plan): "prod" | "holdout" — какая модель
    # обслужила запрос. mobile_api это поле не проксирует (внутреннее).
    serving_group: str = "prod"
    # Фаза 25: расширенный контракт для страницы ML-объяснений — описывает
    # ТОП-рекомендацию (первую в списке). base_value — популяционная база
    # модели (sigmoid expected_value); confidence — из зазора топ-2 score;
    # alt_recs/feature_interpretations помогают строить force plot/waterfall.
    base_value: float = 0.0
    confidence: float = 0.0
    expected_roi: float | None = None
    rationale: str = ""
    alt_recs: list[str] = Field(default_factory=list)
    feature_interpretations: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_feature_matrix(
    user_features: dict,
    mcc_codes: list[str],
    feature_columns: list[str],
) -> pd.DataFrame:
    """Build one row per candidate, aligned with the model's training schema."""
    base = {k: user_features.get(k, 0) for k in feature_columns}
    rows = []
    for mcc in mcc_codes:
        row = dict(base)
        try:
            row["mcc_code_int"] = int(mcc)
        except (TypeError, ValueError):
            row["mcc_code_int"] = 0
        rows.append(row)
    df = pd.DataFrame(rows)
    if feature_columns:
        df = df.reindex(columns=feature_columns, fill_value=0)
    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.fillna(0.0).astype("float32")


def _shap_top5(shap_row: np.ndarray, columns: list[str]) -> dict[str, float]:
    pairs = sorted(
        zip(columns, shap_row.tolist()),
        key=lambda kv: abs(kv[1]),
        reverse=True,
    )
    return {k: round(float(v), 6) for k, v in pairs[:5]}


def _feature_values_for(factors: dict[str, float], row) -> dict[str, float]:
    """Сырые значения признаков из строки матрицы X для топ-факторов."""
    out: dict[str, float] = {}
    for name in factors:
        try:
            out[name] = round(float(row[name]), 4)
        except (KeyError, TypeError, ValueError):
            continue
    return out


# --- Фаза 25: расширенный контракт ML-объяснений --------------------------
def _to_prob(x: float) -> float:
    """Число из explainer → вероятность [0.01, 0.99].

    TreeExplainer.expected_value для классификатора обычно в лог-оддсах →
    sigmoid; если значение уже похоже на вероятность (0..1) — берём как есть."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.18
    if not (0.0 <= v <= 1.0):
        v = 1.0 / (1.0 + np.exp(-v))
    return max(0.01, min(0.99, v))


def _base_value(explainer) -> float:
    ev = getattr(explainer, "expected_value", 0.18)
    if isinstance(ev, list | tuple | np.ndarray):
        ev = ev[-1]  # класс 1 (принятие)
    return _to_prob(ev)


def _confidence(items: list[RecommendationItem]) -> float:
    """Уверенность из зазора между топ-1 и топ-2 score."""
    margin = (items[0].score - items[1].score) if len(items) > 1 else 0.2
    return round(max(0.5, min(0.99, 0.5 + margin * 2.0)), 4)


def _expected_roi(score: float, campaign: dict | None) -> float:
    """Прокси ROI: выше вероятность принятия → выше ROI; дороже ставка → ниже."""
    rate = float((campaign or {}).get("cashback_rate") or 5.0)
    base = 1.5 + score * 3.5           # 1.5..5.0 по score
    return round(base * (5.0 / max(rate, 5.0)), 1)


def _interpretation(shap_val: float) -> str:
    mag = abs(shap_val)
    strength = "сильно " if mag >= 0.1 else ("умеренно " if mag >= 0.03 else "слегка ")
    direction = "повышает вероятность" if shap_val >= 0 else "снижает вероятность"
    return strength + direction


def _rationale(top: RecommendationItem) -> str:
    factors = top.top_factors or {}
    pos = sum(1 for v in factors.values() if v > 0)
    neg = len(factors) - pos
    pct = round(top.score * 100)
    lead = ("преобладают усиливающие факторы" if pos >= neg
            else "есть сдерживающие факторы, но score остаётся высоким")
    return (f"Ранжирующая модель оценила вероятность принятия в {pct}%: "
            f"{lead} (топ-{len(factors)} по |SHAP|).")


# B21/B26: «кому положена кампания» решает только eligible_campaigns
# (статус и архив, окно дат, категория, таргет-сегменты, последнее согласие,
# RFM, остаток бюджета и дневной лимит). Сумма покупки при выдаче неизвестна
# (NULL): минимум проверяет R2 по min_transaction_amount — эффективному
# минимуму категории. На MCC — первая строка функции в её же порядке:
# лучшая ставка, затем более свежая кампания.
_ELIGIBLE_CAMPAIGNS_SQL = text(
    """
    SELECT m.mcc_code,
           e.campaign_id::text AS campaign_id,
           e.name,
           e.cashback_rate,
           e.min_transaction_amount,
           e.allowed_channels,
           e.budget_total,
           e.budget_spent,
           e.daily_limit,
           e.spent_today
      FROM unnest(CAST(:mccs AS text[])) AS m(mcc_code)
     CROSS JOIN LATERAL (
           SELECT ec.*
             FROM eligible_campaigns(CAST(:user_id AS uuid), m.mcc_code,
                                     NULL, CAST(:at AS timestamptz)) AS ec
            ORDER BY ec.cashback_rate DESC, ec.start_date DESC, ec.campaign_id
            LIMIT 1
     ) AS e
    """
)


async def _fetch_campaigns_for_mccs(
    db_engine: Any, user_id: str, mccs: list[str], at: datetime | None = None,
) -> dict[str, dict]:
    """Лучшая положенная клиенту кампания на каждый MCC-кандидат.

    MCC без положенной кампании в ответе нет. Обычный SELECT — без
    блокировок: деньги решает начисление (AccrualEngine). Ошибка БД →
    пустой ответ (ни одного оффера), а не оффер без проверки."""
    if not mccs:
        return {}
    params = {"user_id": user_id, "mccs": mccs, "at": at or datetime.now(UTC)}
    out: dict[str, dict] = {}
    try:
        async with db_engine.connect() as conn:
            rows = (await conn.execute(_ELIGIBLE_CAMPAIGNS_SQL, params)).mappings().all()
            for r in rows:
                out[str(r["mcc_code"]).strip()] = dict(r)
    except Exception as exc:  # noqa: BLE001
        log.warning("campaign_lookup_failed", error=str(exc))
    return out


# ---------------------------------------------------------------------------
# B27: BRE для /explain — без побочных эффектов.
#
# * R1, R2, R5, R6 — чистые проверки контекста (R6 — по строке кампании из
#   eligible_campaigns, без FOR UPDATE — так же и в боевой выдаче); R7 читает
#   снапшот ML-лимитов (Redis GET + кэш в памяти процесса) — берутся как есть.
# * R3 (anti-fatigue) и R4 (частота за 24 ч) НЕ вычисляются: в боевой
#   цепочке они пишут в Redis (SETEX recent_offer:* / rec_count:*) и задают
#   политику контактов с клиентом, а не решение модели. Объяснение
#   показывает, что модель выбрала бы при прочих допустимых условиях, и не
#   пустеет на сутки после реальной выдачи.
# Вне BRE explain тоже ничего не пишет: при промахе по features:{user_id}
# признаки читаются из ClickHouse без прогрева кэша Redis (warm_cache=False);
# прогревает его только боевая выдача.
# ---------------------------------------------------------------------------
EXPLAIN_RULES: list[tuple[str, RuleFn]] = [
    ("R1_category_exclusion",    category_exclusion.evaluate),
    ("R2_min_transaction",       min_transaction.evaluate),
    ("R7_ml_rate_cap",           ml_rate_cap.evaluate),
    ("R5_channel_applicability", channel_applicability.evaluate),
    ("R6_budget_reservation",    budget_reservation.evaluate),
]
_explain_bre = BusinessRulesEngine(EXPLAIN_RULES)


# ---------------------------------------------------------------------------
async def _recommend(
    request: Request,
    user_id: str,
    *,
    top_k: int,
    transaction_amount: float | None,
    channel: str | None,
    bre: BusinessRulesEngine,
    warm_cache: bool,
) -> tuple[RecommendationResponse, dict[str, dict]]:
    """Шаги 0–3 + расширенный контракт; сам ничего не пишет.

    Побочные эффекты — только внутри переданного ``bre`` (и прогрев кэша
    признаков при промахе Redis, если ``warm_cache``). Возвращает ответ и
    кампании по MCC (для события Kafka боевой выдачи)."""
    state = request.app.state
    settings = state.settings

    # ---- 0. Feature lookup ------------------------------------------
    features = await state.feature_store.get(user_id, warm_cache=warm_cache)
    if features is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"user '{user_id}' not found in the feature store",
        )

    # ---- 1. Retrieval ------------------------------------------------
    candidates = state.candidate_gen.retrieve(user_id, k=settings.candidate_top_k)
    log.info("retrieve_done", user_id=user_id, n_candidates=len(candidates))
    if not candidates:
        return RecommendationResponse(
            user_id=user_id, recommendations=[],
            model_version=state.model_watcher.version,
        ), {}

    # ---- 2. Ranking + SHAP ------------------------------------------
    # Holdout-сплит (beyond-plan): ~5% пользователей детерминированно
    # обслуживаются предыдущей моделью; model_version в ответе отражает
    # реально применённую модель, поэтому онлайн-CTR сравнивает версии.
    loaded, serving_group = await state.model_watcher.get_for_user(user_id)
    if loaded is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ranking model not loaded yet",
        )

    cols = loaded.feature_columns or list(features.keys()) + ["mcc_code_int"]
    X = _build_feature_matrix(features, candidates, cols)

    proba = loaded.model.predict_proba(X)[:, 1]
    shap_values = loaded.explainer.shap_values(X)
    if isinstance(shap_values, list):
        shap_values = shap_values[1]
    shap_values = np.asarray(shap_values)

    order = np.argsort(-proba)  # high→low score

    # ---- 3. Campaign join + BRE filtering ---------------------------
    campaigns = await _fetch_campaigns_for_mccs(state.db_engine, user_id, candidates)

    accepted: list[RecommendationItem] = []
    considered = 0
    for idx in order:
        considered += 1
        mcc = candidates[idx]
        campaign = campaigns.get(mcc)
        if campaign is None:
            # Нет положенной клиенту кампании — предлагать нечего: в BRE не
            # идёт и не тратит лимит показов R4.
            log.info("candidate_filtered", user_id=user_id, mcc=mcc,
                     rule="eligible_campaigns", reason="no eligible campaign")
            continue

        ctx = RuleContext(
            user_id=user_id,
            candidate_mcc=mcc,
            campaign_id=campaign["campaign_id"],
            user_segment_id=int(features.get("segment_id"))
                            if features.get("segment_id") is not None else None,
            transaction_amount=transaction_amount,
            channel=channel,
            campaign=campaign,
            redis=state.redis,
            db=state.db_engine,
            settings=settings,
        )
        verdict = await bre.evaluate(ctx)
        if verdict.outcome is not RuleOutcome.PASS:
            log.info(
                "candidate_filtered",
                user_id=user_id, mcc=mcc, rule=verdict.rule, reason=verdict.reason,
            )
            continue

        factors = _shap_top5(shap_values[idx], list(X.columns))
        accepted.append(
            RecommendationItem(
                mcc_code=str(mcc),
                score=float(proba[idx]),
                campaign_id=campaign["campaign_id"],
                top_factors=factors,
                feature_values=_feature_values_for(factors, X.iloc[idx]),
            )
        )
        if len(accepted) >= top_k:
            break

    # ---- Фаза 25: расширенный контракт для страницы ML-объяснений ----
    base_value = confidence = 0.0
    expected_roi: float | None = None
    rationale = ""
    alt_recs: list[str] = []
    interpretations: dict[str, str] = {}
    if accepted:
        top = accepted[0]
        base_value = _base_value(loaded.explainer)
        confidence = _confidence(accepted)
        expected_roi = _expected_roi(top.score, campaigns.get(top.mcc_code))
        rationale = _rationale(top)
        interpretations = {
            name: _interpretation(val) for name, val in top.top_factors.items()
        }
        alt_recs = [
            f"MCC {it.mcc_code} — score {round(it.score * 100)}%"
            for it in accepted[1:4]
        ]

    response = RecommendationResponse(
        user_id=user_id,
        recommendations=accepted,
        model_version=loaded.version,
        candidates_considered=considered,
        serving_group=serving_group,
        base_value=base_value,
        confidence=confidence,
        expected_roi=expected_roi,
        rationale=rationale,
        alt_recs=alt_recs,
        feature_interpretations=interpretations,
    )
    return response, campaigns


@router.get("/{user_id}", response_model=RecommendationResponse,
            dependencies=[Depends(require_mobile_api)])
async def get_recommendations(
    request: Request,
    user_id: str,
    top_k: int = Query(default=5, ge=1, le=20),
    transaction_amount: float | None = Query(default=None, ge=0),
    channel: str | None = Query(default=None,
                                pattern="^(ONLINE|POS|ATM|MOBILE)$"),
) -> RecommendationResponse:
    """Боевая выдача клиенту (mobile_api)."""
    response, campaigns = await _recommend(
        request, user_id, top_k=top_k,
        transaction_amount=transaction_amount, channel=channel,
        bre=request.app.state.bre, warm_cache=True,
    )

    # ---- Side-effects: persist + emit Kafka events --------------------
    # В ответе — только офферы с записанной строкой (B29): у каждого свой
    # recommendation_id и expires_at. R3/R4 отметили показ ещё в BRE —
    # с оффера без строки (сбой записи → 503, кампании уже нет) отметки
    # снимаются: клиент его не получил.
    offered = response.recommendations
    try:
        response.recommendations, generated_at = await _persist_offers(
            request, user_id, offered, response.model_version,
        )
    except HTTPException:
        await _release_unserved(request, user_id, offered)
        raise
    served = {(it.mcc_code, it.campaign_id) for it in response.recommendations}
    await _release_unserved(request, user_id, [
        it for it in offered if (it.mcc_code, it.campaign_id) not in served])
    await _emit_created(
        request, user_id, response.recommendations, campaigns,
        response.model_version, generated_at,
    )
    return response


@router.get("/{user_id}/explain", response_model=RecommendationResponse,
            dependencies=[Depends(require_campaign_manager)])
async def explain_recommendations(
    request: Request,
    user_id: str,
    top_k: int = Query(default=5, ge=1, le=20),
) -> RecommendationResponse:
    """Объяснение для страницы «ML-объяснения» (campaign_manager, B27).

    Тот же ответ, что у боевой выдачи, но без побочных эффектов: нет
    INSERT в ``recommendations``, нет события Kafka, BRE — ``EXPLAIN_RULES``
    (без записей anti-fatigue/частоты в Redis), признаки — без прогрева
    кэша Redis. Кампании — тем же read-only запросом к
    ``eligible_campaigns``, что и в боевой выдаче."""
    response, _ = await _recommend(
        request, user_id, top_k=top_k,
        transaction_amount=None, channel=None, bre=_explain_bre,
        warm_cache=False,
    )
    return response


# ---------------------------------------------------------------------------
# B29: строку ``recommendations`` на каждый оффер ответа пишет только боевая
# выдача (раньше вторую, без model_version, писал mobile_api — отклик
# обновлял её, и онлайн-CTR по версии модели был пуст). По её
# recommendation_id клиент откликается, начисление находит принятый оффер,
# а model_version атрибутирует онлайн-CTR (ml_online_ctr в campaign_manager).
# Срок — по часам БД, как и проверка отклика (expires_at > now()): не позже
# конца кампании и не дольше OFFER_TTL_DAYS. INSERT … SELECT: для кампании,
# которой уже нет, строка не создаётся (и оффер не отдаётся) — без ошибки
# FK на всю выдачу.
_PERSIST_OFFER_SQL = text(
    """
    INSERT INTO recommendations (
        recommendation_id, user_id, campaign_id, mcc_code, model_score,
        generated_at, response_status, expires_at, model_version
    )
    SELECT CAST(:rid AS uuid), CAST(:uid AS uuid), c.campaign_id, :mcc,
           CAST(:score AS real), now(), 'PENDING',
           LEAST(c.end_date, now() + make_interval(days => CAST(:ttl_days AS int))),
           :model_version
      FROM cashback_campaigns c
     WHERE c.campaign_id = CAST(:cid AS uuid)
    RETURNING expires_at, generated_at
    """
)


async def _persist_offers(
    request: Request,
    user_id: str,
    items: list[RecommendationItem],
    model_version: str | None,
) -> tuple[list[RecommendationItem], datetime | None]:
    """Одна строка ``recommendations`` на оффер — в одной транзакции.

    Возвращает офферы с ``recommendation_id``/``expires_at`` записанных
    строк и ``generated_at`` выдачи. Оффер без строки клиенту не отдаётся:
    принять его нельзя (respond — 404), а в аналитике его нет. Поэтому сбой
    записи — 503, а не ответ с офферами, которых не существует."""
    if not items:
        return [], None

    state = request.app.state
    ttl_days = state.settings.offer_ttl_days
    persisted: list[RecommendationItem] = []
    generated_at: datetime | None = None
    try:
        async with state.db_engine.begin() as conn:
            for item in items:
                rid = str(uuid.uuid4())
                row = (await conn.execute(_PERSIST_OFFER_SQL, {
                    "rid": rid,
                    "uid": str(user_id),
                    "cid": item.campaign_id,
                    "mcc": item.mcc_code,
                    "score": float(item.score),
                    "ttl_days": ttl_days,
                    # Миграция 005: версия модели, реально обслужившей запрос
                    # (holdout — предыдущая), — для онлайн-CTR по версиям.
                    "model_version": model_version,
                })).first()
                if row is None:
                    log.warning("offer_campaign_missing", user_id=user_id,
                                mcc=item.mcc_code, campaign_id=item.campaign_id)
                    continue
                generated_at = row.generated_at
                persisted.append(item.model_copy(update={
                    "recommendation_id": rid, "expires_at": row.expires_at,
                }))
    except Exception as exc:  # noqa: BLE001
        log.error("persist_recommendations_failed", user_id=user_id,
                  error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="recommendations could not be stored, retry",
        ) from exc
    return persisted, generated_at


async def _release_unserved(
    request: Request, user_id: str, items: list[RecommendationItem],
) -> None:
    """Снять BRE-отметки показа (R3 anti-fatigue, R4 частота) с офферов,
    которых клиент не получил (этап D).

    Отметки ставятся при оценке, до записи строк. Без снятия оффер, строку
    которого записать не удалось, считался бы показанным: кампания скрыта
    от клиента на ANTI_FATIGUE_TTL_DAYS, лимит R4 съеден без показа.
    Best-effort: сбой Redis здесь ответа не меняет."""
    state = request.app.state
    for item in items:
        ctx = RuleContext(user_id=user_id, candidate_mcc=item.mcc_code,
                          campaign_id=item.campaign_id, redis=state.redis,
                          settings=state.settings)
        try:
            await state.bre.release(ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("bre_release_failed", user_id=user_id, mcc=item.mcc_code,
                        campaign_id=item.campaign_id,
                        error=f"{type(exc).__name__}: {exc}")


async def _emit_created(
    request: Request,
    user_id: str,
    items: list[RecommendationItem],
    campaigns_by_mcc: dict[str, dict],
    model_version: str | None,
    generated_at: datetime | None,
) -> None:
    """Событие ``recommendations.created`` на каждый записанный оффер — для
    Notification Pipeline (transaction_listener): доставка клиенту и запись
    канала доставки в строку ``recommendation_id`` (B29)."""
    state = request.app.state
    producer = getattr(state, "kafka_producer", None)
    if producer is None or not items:
        return
    for item in items:
        # Кампания оффера — из того же eligible_campaigns, что выбрал её (B42):
        # настоящее имя, а не «Cashback offer».
        campaign = campaigns_by_mcc[item.mcc_code]
        payload = {
            "recommendation_id": item.recommendation_id,
            "user_id": str(user_id),
            "mcc_code": item.mcc_code,
            "campaign_id": item.campaign_id,
            "score": float(item.score),
            "cashback_rate": float(campaign.get("cashback_rate") or 0),
            "campaign_name": campaign["name"],
            "model_version": model_version,
            "generated_at": generated_at.isoformat() if generated_at else None,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "preferences": {
                # Default channel mix — production would look this up by user.
                "allowed_channels": ["push", "in_app", "email"],
            },
        }
        try:
            await producer.send(
                state.settings.recommendations_topic,
                key=str(user_id).encode("utf-8"),
                value=json.dumps(payload).encode("utf-8"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("recommendations_publish_failed", error=str(exc))
