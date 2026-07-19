"""ML-объяснения (фаза 25): список клиентов для левой панели.

``GET /ml/customers`` — последняя рекомендация на пользователя (её
``model_score`` = вероятность принятия) + витринный сегмент. Само SHAP-
объяснение фронт запрашивает напрямую у recommendation_api
(``GET /recommendations/{user_id}``, admin-токен) — здесь только ростер.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session_dep
from app.reference_data import SEGMENT_NAMES
from app.schemas import MlCustomer
from app.security import get_current_user
from app.segments import bucket_of

router = APIRouter(
    prefix="/ml", tags=["ml"],
    dependencies=[Depends(get_current_user)],
)

_CUSTOMERS_SQL = text(
    """
    SELECT customer_id, external_id, segment_id, prediction FROM (
        SELECT DISTINCT ON (r.user_id)
               r.user_id::text AS customer_id,
               u.external_id   AS external_id,
               u.segment_id    AS segment_id,
               r.model_score   AS prediction
          FROM recommendations r
          JOIN users u USING (user_id)
         ORDER BY r.user_id, r.generated_at DESC
    ) t
    ORDER BY prediction DESC NULLS LAST
    LIMIT :limit
    """
)


@router.get("/customers", response_model=list[MlCustomer])
async def ml_customers(
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session_dep),
) -> list[MlCustomer]:
    rows = (await session.execute(_CUSTOMERS_SQL, {"limit": limit})).mappings().all()
    out: list[MlCustomer] = []
    for r in rows:
        bucket = bucket_of(r["segment_id"])
        cid = str(r["customer_id"])
        out.append(MlCustomer(
            customer_id=cid,
            name=r["external_id"] or f"Клиент {cid[:8]}",
            segment=SEGMENT_NAMES.get(bucket, "—") if bucket else "—",
            prediction=round(float(r["prediction"] or 0.0), 4),
        ))
    return out
