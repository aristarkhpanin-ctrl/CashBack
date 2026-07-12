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
from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.bre.engine import BusinessRulesEngine
from app.bre.models import RuleContext, RuleOutcome

log = structlog.get_logger("api.recommendations")

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


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
    loaded = await state.model_watcher.get()
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

    response = RecommendationResponse(
        user_id=user_id,
        recommendations=accepted,
        model_version=loaded.version,
        candidates_considered=considered,
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
