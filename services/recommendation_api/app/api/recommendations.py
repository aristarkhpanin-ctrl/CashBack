"""Recommendation endpoint — chapter 3.1, listing 3.9.

Three-stage pipeline:

    1. Retrieve ~20 candidates via the ALS index (CandidateGenerator).
    2. Rank them with the LGBM ranker, attaching SHAP top-5 factors.
    3. Filter through the BRE; keep only candidates that PASS.
"""
from __future__ import annotations

import asyncio
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

from app.bre.engine import BusinessRulesEngine
from app.bre.models import RuleContext, RuleOutcome
from app.security import require_caller

log = structlog.get_logger("api.recommendations")

# Service-to-service auth (beyond-plan): валидный service|access токен.
router = APIRouter(
    prefix="/recommendations", tags=["recommendations"],
    dependencies=[Depends(require_caller)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class RecommendationItem(BaseModel):
    mcc_code: str = Field(..., examples=["5411"])
    score: float
    campaign_id: str | None = None
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
    if isinstance(ev, (list, tuple, np.ndarray)):
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


async def _fetch_campaigns_for_mccs(
    db_engine: Any, mccs: list[str],
) -> dict[str, dict]:
    """Return one ACTIVE campaign per MCC (or empty dict if none)."""
    if not mccs:
        return {}
    sql = text(
        """
        SELECT DISTINCT ON (cc.mcc_code)
               cc.mcc_code,
               c.campaign_id::text   AS campaign_id,
               c.min_transaction_amount,
               c.allowed_channels,
               c.budget_total,
               c.budget_spent,
               c.status::text        AS status,
               c.cashback_rate
          FROM campaign_categories cc
          JOIN cashback_campaigns c USING (campaign_id)
         WHERE cc.mcc_code = ANY(:mccs)
           AND c.status = 'ACTIVE'
         ORDER BY cc.mcc_code, c.start_date DESC
        """
    )
    out: dict[str, dict] = {}
    try:
        async with db_engine.connect() as conn:
            rows = (await conn.execute(sql, {"mccs": mccs})).mappings().all()
            for r in rows:
                out[str(r["mcc_code"]).strip()] = dict(r)
    except Exception as exc:  # noqa: BLE001
        log.warning("campaign_lookup_failed", error=str(exc))
    return out


# ---------------------------------------------------------------------------
@router.get("/{user_id}", response_model=RecommendationResponse)
async def get_recommendations(
    request: Request,
    user_id: str,
    top_k: int = Query(default=5, ge=1, le=20),
    transaction_amount: float | None = Query(default=None, ge=0),
    channel: str | None = Query(default=None,
                                pattern="^(ONLINE|POS|ATM|MOBILE)$"),
) -> RecommendationResponse:
    state = request.app.state
    settings = state.settings

    # ---- 0. Feature lookup ------------------------------------------
    features = await state.feature_store.get(user_id)
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
        )

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
    campaigns = await _fetch_campaigns_for_mccs(state.db_engine, candidates)
    bre: BusinessRulesEngine = state.bre

    accepted: list[RecommendationItem] = []
    considered = 0
    for idx in order:
        considered += 1
        mcc = candidates[idx]
        campaign = campaigns.get(mcc)

        ctx = RuleContext(
            user_id=user_id,
            candidate_mcc=mcc,
            campaign_id=campaign["campaign_id"] if campaign else None,
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
                campaign_id=campaign["campaign_id"] if campaign else None,
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

    # ---- Side-effects: persist + emit Kafka events --------------------
    await _persist_and_emit(
        request=request,
        user_id=user_id,
        items=accepted,
        campaigns_by_mcc=campaigns,
        model_version=loaded.version,
    )
    return response


# ---------------------------------------------------------------------------
async def _persist_and_emit(
    request: Request,
    user_id: str,
    items: list[RecommendationItem],
    campaigns_by_mcc: dict[str, dict],
    model_version: str | None,
) -> None:
    """Persist each accepted recommendation in Postgres and broadcast
    a ``recommendations.created`` event so the Notification Pipeline
    (transaction_listener) can dispatch user-facing channels.
    """
    if not items:
        return

    state = request.app.state
    settings = state.settings
    db_engine = state.db_engine
    producer = getattr(state, "kafka_producer", None)

    now = datetime.now(UTC)
    expires_at = now + timedelta(days=7)

    # 1) Persist each recommendation row in Postgres.
    insert_sql = text(
        """
        INSERT INTO recommendations (
            recommendation_id, user_id, campaign_id, mcc_code,
            model_score, generated_at, response_status, expires_at,
            model_version
        )
        VALUES (
            :rid, :uid, :cid, :mcc, :score, :gen_at, 'PENDING', :exp_at,
            :model_version
        )
        """
    )
    rows = []
    for item in items:
        if item.campaign_id is None:
            continue
        rows.append({
            "rid": uuid.uuid4(),
            "uid": uuid.UUID(str(user_id)),
            "cid": uuid.UUID(item.campaign_id),
            "mcc": item.mcc_code,
            "score": float(item.score),
            "gen_at": now,
            "exp_at": expires_at,
            # Миграция 005: версия модели атрибуцирует онлайн-CTR
            # (gauge ml_online_ctr в campaign_manager).
            "model_version": model_version,
        })
    if rows:
        try:
            async with db_engine.begin() as conn:
                for row in rows:
                    await conn.execute(insert_sql, row)
        except Exception as exc:  # noqa: BLE001
            log.warning("persist_recommendations_failed", error=str(exc))

    # 2) Publish recommendations.created events.
    if producer is None:
        return
    for item in items:
        campaign = campaigns_by_mcc.get(item.mcc_code) or {}
        payload = {
            "user_id": str(user_id),
            "mcc_code": item.mcc_code,
            "campaign_id": item.campaign_id,
            "score": float(item.score),
            "cashback_rate": float(campaign.get("cashback_rate") or 0),
            "campaign_name": campaign.get("name", "Cashback offer"),
            "model_version": model_version,
            "generated_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "preferences": {
                # Default channel mix — production would look this up by user.
                "allowed_channels": ["push", "in_app", "email"],
            },
        }
        try:
            import json as _json
            await producer.send(
                settings.recommendations_topic,
                key=str(user_id).encode("utf-8"),
                value=_json.dumps(payload).encode("utf-8"),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("recommendations_publish_failed", error=str(exc))
